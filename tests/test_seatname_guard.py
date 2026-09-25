#!/usr/bin/env python3
"""The seat-name pre-commit rung, script-driven and END TO END.

THE DEBT THIS RUNG HOLDS (measured 2026-08-09): tests/ carried 1203 string
literals that ARE a seat identity across 63 files, plus 747 prose occurrences,
every one written in good faith by copying the local convention. `tests/` is
public-bound, so each is publication debt payable at export.

EVERY REFUSING ARM IS PAIRED WITH A PASSING ONE, and that is not symmetry for
its own sake. This rung FAILS CLOSED on a broken MEASUREMENT, so a crash or a
TypeError produces rc=1 — indistinguishable from a real finding. On the first
run of this suite's controls, eight refusals "passed" while the rung was in
fact dying on a bytes-vs-str path; only the must-PASS half exposed it. A
fail-closed guard whose tests only assert refusals is measuring nothing.

A MISSING SIBLING SEAM IS THE OPPOSITE CASE and yields rc=0 with a loud
warning: that is a broken INSTALL, and the v3 rung law keeps one rung's
absence from becoming another rung's refusal. Both halves are pinned in
SeatNameInstallFaultIsNotAFindingTest, whose two arms drive the identical
staged tree and differ only in whether the seams are reachable.

FIXTURE DISCIPLINE: every name here is SYNTHETIC and the authority is injected
via $HELM_SEAT_NAMES. Once installed the rung scans this very file on commit,
and a guard's tests must not trip the guard they prove.

The refuse-arms below are the measured bypasses of the first implementation;
the pass-arms are its two measured false positives plus the deliberate
narrowings.
"""
import ast
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import seatname_guard, work                          # noqa: E402
from helm.work import _guard                                   # noqa: E402

RUNG = os.path.abspath(seatname_guard.__file__)
A, B = "zz-synthetic-seat", "zz-synthetic-seat-2"
ENV_KEYS = ("HELM_SEAT_NAMES", "MELD_SEAT_NAMES", "HELM_SEATNAME_SKIP",
            "HELM_SEATNAME_REPO", "HELM_HOME", "HELM_ADOPTED_DIR",
            "HELM_CHAT_DIR", "HELM_PRIVATE_NEEDLES", "HOME",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")


def _read_authority_sets(path, **kw):
    """The old (armed, held, why) shape derived from the typed Authority.

    These arms assert on SETS, which is still the right question for a parser
    test; production asks the Authority its two questions instead. The sets
    are CASEFOLDED canonical keys now — that is the cure, not an artifact of
    the migration, so an arm that expected a shouted spelling back should be
    changed rather than this shim.
    """
    a, why = seatname_guard.read_authority(path, **kw)
    return (a.names(seatname_guard.ARMED), a.names(seatname_guard.HELD), why)


class RungBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seatname-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        # The first cleanup runs last, after every cleanup a test registers,
        # so no later snapshot restore can leave these keys popped.
        self.addCleanup(self._restore_env)
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HOME"] = os.path.join(self.tmp, "home")
        os.makedirs(os.environ["HOME"])
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        self.names_file = os.path.join(self.tmp, "seat-names.txt")
        with open(self.names_file, "w") as f:
            f.write("# synthetic\n%s\n%s\n" % (A, B))
        needles = os.path.join(self.tmp, "needles.txt")
        with open(needles, "w") as f:
            f.write("zz-synthetic-seatname-rung\n")
        os.environ["HELM_PRIVATE_NEEDLES"] = needles
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "tests"))
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t"),
                    # THE FIXTURE DECLARES ITS LAW. It simulates
                    # helm-the-shared-checkout; an undeclared PROJECT repo
                    # now defaults to the leak legs (task/2441).
                    ("git", "config", "--local",
                     "helm.guard.profile", "rail")):
            self.assertEqual(self.sh(*cmd).returncode, 0)

    def _restore_env(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, *args, env=None):
        merged = dict(os.environ, **(env or {}))
        return subprocess.run(list(args), cwd=self.root, capture_output=True,
                              text=True, timeout=90, env=merged)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.assertEqual(self.sh("git", "add", "--", rel).returncode, 0)

    def rung(self, cwd=None, **env):
        """Driven by CWD, never by an env override — there is no longer one to
        pass, because an inherited value naming a different VALID repo made the
        rung scan a clean tree and pass."""
        base = {"HELM_SEAT_NAMES": self.names_file}
        base.update(env)
        merged = dict(os.environ, **base)
        return subprocess.run([sys.executable, RUNG, "--staged"],
                              cwd=cwd or self.root, capture_output=True,
                              text=True, timeout=90, env=merged)

    def assertRefused(self, r, needle=None):
        self.assertEqual(r.returncode, 1, "expected REFUSED:\n" + r.stderr)
        self.assertIn("REFUSED", r.stderr)
        if needle:
            self.assertIn(needle, r.stderr)

    def assertAdvisory(self, r, needle=None):
        """REPORTED, NOT BLOCKING. The narrowed rung proves a LEXICAL claim;
        a value a program might ASSEMBLE at run time is signal it cannot
        prove, so it must print and must NOT gate. rc==0 is half the
        assertion — the advisory text is the other half, because a rung that
        silently stopped measuring these would also return 0."""
        self.assertEqual(r.returncode, 0, "expected rc=0 (advisory):\n" + r.stderr)
        self.assertIn("ADVISORY", r.stderr)
        if needle:
            self.assertIn(needle, r.stderr)

    def assertAllowed(self, r):
        self.assertEqual(r.returncode, 0, "expected rc=0:\n" + r.stderr)


class SeatNameBypassesAreClosedTest(RungBase):
    """The eight measured false negatives of the first implementation."""

    def test_an_exact_fixture_value_is_refused(self):
        self.write("tests/test_x.py", 'ROW = {"seat": "%s"}\n' % A)
        self.assertRefused(self.rung(), repr(A))

    def test_a_delimited_string_of_two_real_seats_is_ADVISORY(self):
        # Yields seat-name VALUES via .split() while never being one. The
        # rung still MEASURES it and still prints it; it does not gate,
        # because no lexical claim covers what .split() will produce.
        self.write("tests/test_x.py", 'S = "%s,%s".split(",")\n' % (A, B))
        self.assertAdvisory(self.rung(), "constructed")

    def test_every_split_spelling_that_yields_seats_is_ADVISORY(self):  # noqa: VACUOUS_ASSERTION — each assertRefused pins rc==1 and the REFUSED text inside a helper; the maxsplit-0 arm directly below is the paired NON-refusal over the same machinery
        # READ THE OPERATION, NOT THE SHAPES I HAPPENED TO THINK OF. The first
        # version modelled only a positional separator, so sep= keyword,
        # split(None), a bare split() and rsplit all walked past.
        for body in ('S = "%s,%s".split(",")\n' % (A, B),
                     'S = "%s,%s".split(sep=",")\n' % (A, B),
                     'S = "%s %s".split(None)\n' % (A, B),
                     'S = "%s %s".split()\n' % (A, B),
                     'S = "%s,%s".rsplit(",")\n' % (A, B),
                     'S = "%s,%s".rsplit(",", maxsplit=1)\n' % (A, B)):
            self.write("tests/test_x.py", body)
            self.assertAdvisory(self.rung(), needle="constructed")

    def test_a_split_that_does_NOT_split_is_not_a_collection(self):  # noqa: VACUOUS_ASSERTION — the paired refusing arm directly above drives the identical receiver through the same machinery; only maxsplit differs, which is the whole contract
        # MAXSPLIT IS PART OF THE OPERATION. `"a,b".split(",", 0)` yields ONE
        # element, so ignoring the argument invented a collection the code
        # never produces and refused an honest line.
        self.write("tests/test_x.py", 'S = "%s,%s".split(",", 0)\n' % (A, B))
        self.assertAllowed(self.rung())
        # A non-constant maxsplit is not deterministic; decline rather than guess.
        self.write("tests/test_x.py",
                   'import os\nS = "%s,%s".split(",", os.cpu_count())\n' % (A, B))
        self.assertAllowed(self.rung())

    def test_PLACEHOLDER_SHAPED_TEXT_THAT_IS_NEVER_FORMATTED_passes(self):
        """A literal that merely LOOKS like a template is not an identity.

        The broad claim refused `"zz-synthetic-{tail}"` on the theory that
        something might format it into a real name. Nothing in the file does,
        the value that reaches any reader is the braces themselves, and
        refusing it made the rung wrong about an honest line — the exact
        false-positive class that gets a guard switched off. Under the
        narrowed lexical claim the question is only what the literal IS.
        """
        # AT A SINK, which is where the false positive actually lived —
        # an assignment never reaches the template machinery, so a
        # fixture that does not call one proves nothing about it.
        self.write("tests/test_x.py",
                   'send(seat="%s-{tail}")\n' % A.rsplit("-", 1)[0])
        self.assertAllowed(self.rung())

    def test_deterministic_concatenation_is_refused(self):
        self.write("tests/test_x.py", 'S = "zz-synthetic" + "-seat"\n')
        self.assertRefused(self.rung())

    def test_a_non_python_fixture_under_tests_is_refused(self):
        # The boundary the docstring ADVERTISES is tests/, not tests/**.py.
        self.write("tests/fixture.json", '{"seat": "%s"}\n' % A)
        self.assertRefused(self.rung(), "fixture.json")

    def test_a_non_ascii_path_is_refused(self):
        # git C-quotes such paths; the old parser never unquoted them.
        self.write("tests/test-é.py", 'ROW = "%s"\n' % A)
        self.assertRefused(self.rung())

    def test_two_paths_that_LOSSY_DECODE_TO_ONE_do_not_mask_each_other(self):  # noqa: VACUOUS_ASSERTION — assertRefused pins rc==1 AND that the refusal NAMES the identity, inside a helper the analyzer cannot see into; the clean sibling path staged alongside is the in-arm control
        # REPLACEMENT DECODING IS A BYPASS, not merely a false refusal. An
        # invalid-byte path carrying a real identity and a literal U+FFFD path
        # that is clean both decode to the SAME str under errors="replace", so
        # the guard scanned the clean blob twice and found nothing. fsdecode's
        # surrogateescape is injective, which is what makes the two stay two.
        dirty = os.path.join(self.root.encode(), b"tests", b"x-\xff.json")
        with open(dirty, "wb") as f:
            f.write(b'{"seat": "%s"}\n' % A.encode())
        self.write("tests/x-\ufffd.json", '{"seat": "seat-a"}\n')
        self.assertEqual(self.sh("git", "add", "-A").returncode, 0)
        r = self.rung()
        self.assertRefused(r, A)

    def test_noqa_shaped_STRING_DATA_does_not_disarm_the_line(self):
        self.write("tests/test_x.py",
                   'MSG = "# noqa: SEAT_NAME - not a comment"\n'
                   'ROW = "%s"\n' % A)
        self.assertRefused(self.rung())

    def test_a_diff_suppressed_by_gitattributes_is_still_seen(self):
        with open(os.path.join(self.root, ".gitattributes"), "w") as f:
            f.write("*.py -diff\n")
        self.write(".gitattributes", "*.py -diff\n")
        self.sh("git", "commit", "-q", "--no-verify", "-m", "attrs")
        self.write("tests/test_x.py", 'ROW = "%s"\n' % A)
        self.assertRefused(self.rung())

    def test_outside_a_git_worktree_it_REFUSES_rather_than_skipping(self):
        # The worst of the eight: the first version returned rc=0 SKIPPED.
        #
        # GIT_CEILING_DIRECTORIES is load-bearing and this arm taught me why:
        # `git rev-parse --show-toplevel` WALKS UP. A scratch dir is only
        # "outside a worktree" if no ANCESTOR is a repo, which is a property of
        # the machine, not of the rung — green here, red on the fab node whose
        # temp root sits under one. The ceiling makes the condition the arm
        # claims to create actually hold everywhere.
        outside = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(outside)
        r = self.rung(cwd=outside, GIT_CEILING_DIRECTORIES=self.tmp)
        self.assertRefused(r)
        self.assertIn("not inside a git work tree", r.stderr)

    def test_there_is_NO_env_override_to_re_point_the_rung(self):  # noqa: VACUOUS_ASSERTION — assertRefused pins rc==1 AND the REFUSED text; the analyzer cannot see assertions inside a helper method (its documented nested-def blindness), and the paired pass-arms in the sibling class are what prove these refusals are detections rather than crashes
        # A guard re-pointable by ambient environment has an undocumented off
        # switch: aiming the old override at a second VALID clean repo made
        # the rung pass. Fail-closed on an INVALID path proved nothing.
        clean = os.path.join(self.tmp, "clean")
        os.makedirs(clean)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t")):
            # A failed init leaves `clean` a plain directory, so the override
            # would be inert for the WRONG reason and the arm would pass
            # without ever testing a valid-but-wrong repo.
            self.assertEqual(subprocess.run(list(cmd), cwd=clean,
                                            capture_output=True).returncode, 0)
        self.assertTrue(os.path.isdir(os.path.join(clean, ".git")))
        self.write("tests/test_x.py", 'ROW = "%s"\n' % A)
        r = self.rung(HELM_SEATNAME_REPO=clean)
        self.assertRefused(r)          # the override is INERT; cwd still rules

    def test_a_PURE_RENAME_into_tests_is_refused(self):  # noqa: VACUOUS_ASSERTION — assertRefused pins rc==1 AND the REFUSED text inside a helper; its positive control is test_an_INSIDE_to_inside_rename_stays_quiet, which drives the SAME rename machinery and must NOT refuse
        # No line changes and the venue does: `git mv private/x.py
        # tests/test_x.py` publishes an unchanged blob (measured).
        self.write("private/x.py", 'ROW = "%s"\n' % A)
        # ASSERT THE SEED. If the commit fails the later `git mv` stages an
        # ADD, not a RENAME, and this arm silently stops testing renames.
        self.assertEqual(
            self.sh("git", "commit", "-q", "--no-verify", "-m", "private").returncode,
            0)
        self.assertEqual(
            self.sh("git", "mv", "private/x.py", "tests/test_x.py").returncode, 0)
        status = self.sh("git", "diff", "--cached", "--name-status", "-M").stdout
        self.assertTrue(status.startswith("R"), "not staged as a rename: " + status)
        self.assertRefused(self.rung())

    def test_an_INSIDE_to_inside_rename_stays_quiet(self):  # noqa: VACUOUS_ASSERTION — the contract IS a non-refusal, and its control is the outside->inside arm directly above, which the analyzer correctly reads as a separate observable
        # It published already; re-refusing it would make every tidy-up commit
        # a refusal and the rung would be switched off within a week.
        self.write("tests/test_old.py", 'ROW = "%s"\n' % A)
        self.sh("git", "commit", "-q", "--no-verify", "-m", "seed")
        self.sh("git", "mv", "tests/test_old.py", "tests/test_new.py")
        self.assertAllowed(self.rung())


class SeatNameStructuredBoundaryTest(RungBase):
    """THE NARROWED PROMISE, PINNED IN BOTH DIRECTIONS.

    The contract is Python source plus .json, and that boundary is MEASURED
    rather than imagined: an inventory of this repo's non-Python tests/ assets
    found .js 4 files / .txt 2 / .json 1 / .md 1, and every real-seat
    occurrence in ALL of them is prose, narration or a captured transcript —
    not one is a fixture value. .js carries the most names but has no honest
    stdlib parser, so it is out of scope and belongs to the export sweep; a
    regex pretending to parse it would be the fragile pseudo-parser the review
    ruled against. Promising coverage the implementation cannot honour is
    worse than a narrow promise kept.
    """

    def test_AN_IDENTITY_ADDED_BELOW_LINE_ONE_OF_A_JSON_FIXTURE_is_refused(self):
        """The staged-range filter used to pass a hardcoded line 1, so it
        never looked at the hit at all: a whole file's findings stood or fell
        on whether its FIRST line was in the diff. Adding an identity to an
        existing fixture returned rc=0, and touching line 1 blamed values the
        commit never moved. Both halves are one defect — no real span."""
        self.write("tests/fixture.json",
                   '{\n  "a": "zz-clean-one",\n  "b": "zz-clean-two"\n}\n')
        self.assertEqual(self.sh("git", "commit", "-qm", "base",
                                 "--no-verify").returncode, 0)
        # line 3 now carries a REAL identity; line 1 is untouched.
        self.write("tests/fixture.json",
                   '{\n  "a": "zz-clean-one",\n  "b": "%s"\n}\n' % A)
        self.assertRefused(self.rung(), A)

    def test_a_structured_json_fixture_value_is_refused(self):  # noqa: VACUOUS_ASSERTION — assertRefused/assertAllowed live in a helper the analyzer cannot see into; every arm in this class is PAIRED by construction (a refusing case and its passing twin over the same format and the same machinery), which is what makes each half mean something
        self.write("tests/fixture.json", '{"seat": "%s"}\n' % A)
        self.assertRefused(self.rung(), "fixture.json")

    def test_a_json_collection_of_real_seats_is_refused(self):  # noqa: VACUOUS_ASSERTION — assertRefused/assertAllowed live in a helper the analyzer cannot see into; every arm in this class is PAIRED by construction (a refusing case and its passing twin over the same format and the same machinery), which is what makes each half mean something
        self.write("tests/fixture.json", '{"seats": ["%s", "%s"]}\n' % (A, B))
        self.assertRefused(self.rung())

    def test_a_seat_name_inside_JSON_PROSE_passes(self):  # noqa: VACUOUS_ASSERTION — assertRefused/assertAllowed live in a helper the analyzer cannot see into; every arm in this class is PAIRED by construction (a refusing case and its passing twin over the same format and the same machinery), which is what makes each half mean something
        # The corpus case measured in this repo: a captured sentence stored in
        # a "text" field. Structural matching is EXACT-WHOLE-SCALAR, so a
        # sentence containing a name is not a value.
        self.write("tests/fixture.json",
                   '{"text": "you mistook %s for the resumed seat"}\n' % A)
        self.assertAllowed(self.rung())

    def test_quoted_prose_in_a_markdown_note_passes(self):  # noqa: VACUOUS_ASSERTION — assertRefused/assertAllowed live in a helper the analyzer cannot see into; every arm in this class is PAIRED by construction (a refusing case and its passing twin over the same format and the same machinery), which is what makes each half mean something
        self.write("tests/notes.md", 'The "%s" pane died overnight.\n' % A)
        self.assertAllowed(self.rung())

    def test_a_txt_transcript_naming_seats_passes(self):  # noqa: VACUOUS_ASSERTION — assertRefused/assertAllowed live in a helper the analyzer cannot see into; every arm in this class is PAIRED by construction (a refusing case and its passing twin over the same format and the same machinery), which is what makes each half mean something
        self.write("tests/fixtures/tail.txt",
                   'Monitor event: "%s Helm inbox"\n' % A)
        self.assertAllowed(self.rung())

    def test_an_INSIDE_tests_json_rename_stays_quiet(self):  # noqa: VACUOUS_ASSERTION — assertRefused/assertAllowed live in a helper the analyzer cannot see into; every arm in this class is PAIRED by construction (a refusing case and its passing twin over the same format and the same machinery), which is what makes each half mean something
        self.write("tests/old.json", '{"seat": "%s"}\n' % A)
        self.assertEqual(self.sh("git", "commit", "-q", "--no-verify",
                                 "-m", "seed").returncode, 0)
        self.assertEqual(self.sh("git", "mv", "tests/old.json",
                                 "tests/new.json").returncode, 0)
        self.assertAllowed(self.rung())

    def test_an_OUTSIDE_to_inside_json_rename_is_refused(self):  # noqa: VACUOUS_ASSERTION — assertRefused/assertAllowed live in a helper the analyzer cannot see into; every arm in this class is PAIRED by construction (a refusing case and its passing twin over the same format and the same machinery), which is what makes each half mean something
        self.write("private/data.json", '{"seat": "%s"}\n' % A)
        self.assertEqual(self.sh("git", "commit", "-q", "--no-verify",
                                 "-m", "private").returncode, 0)
        self.assertEqual(self.sh("git", "mv", "private/data.json",
                                 "tests/data.json").returncode, 0)
        self.assertRefused(self.rung())


class SeatNameInstallFaultIsNotAFindingTest(RungBase):
    """A BROKEN INSTALL AND A BROKEN MEASUREMENT GET OPPOSITE ANSWERS.

    Fail-closed governs staged state the commit CARRIES. A missing sibling
    snapshot is estate integrity, and the v3 rung law is explicit that each
    rung's absence isolates to itself — otherwise deleting one scanner blocks
    every commit in every room. Conflating them made this rung refuse whenever
    conflict_marker's snapshot was absent, which broke that rung's own
    fails-open-but-loud arm and never-track's. Measured, not theorised.
    """

    def test_a_missing_sibling_seam_WARNS_and_stands_down(self):
        # The offending file is present, so a refusal here would be the rung
        # answering a question it could not read.
        self.write("tests/test_x.py", 'ROW = "%s"\n' % A)
        empty = os.path.join(self.tmp, "no-seams")
        os.makedirs(empty)
        shutil.copy(RUNG, os.path.join(empty, "seatname_guard.py"))
        r = subprocess.run(
            [sys.executable, os.path.join(empty, "seatname_guard.py"), "--staged"],
            cwd=self.root, capture_output=True, text=True, timeout=90,
            env=dict(os.environ, HELM_SEAT_NAMES=self.names_file))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING", r.stderr)
        self.assertIn("SKIPPED", r.stderr)
        self.assertNotIn("REFUSED", r.stderr)

    def test_an_inherited_PYTHONPATH_cannot_supply_the_seams(self):  # noqa: VACUOUS_ASSERTION — rc==0 plus WARNING plus assertNotIn(REFUSED) is the contract; the sibling arm in this class proves the identical tree REFUSES when the seams are genuinely present, which is what makes this stand-down meaningful
        # PROXIMITY, NOT IMPORTABILITY. A bare __import__ searches all of
        # sys.path, so an inherited PYTHONPATH or an editable checkout
        # satisfied the sibling lookup with MUTABLE SOURCE whenever the
        # installed snapshot was missing — the estate looked guarded while the
        # tree being committed supplied its own judge.
        self.write("tests/test_x.py", 'ROW = "%s"\n' % A)
        snap = os.path.join(self.tmp, "snapshot-without-seams")
        os.makedirs(snap)
        shutil.copy(RUNG, os.path.join(snap, "seatname_guard.py"))
        source_pkg = os.path.dirname(RUNG)
        self.assertTrue(os.path.exists(
            os.path.join(source_pkg, "conflict_marker.py")),
            "control: PYTHONPATH must be ABLE to supply the sibling")
        r = subprocess.run(
            [sys.executable, os.path.join(snap, "seatname_guard.py"), "--staged"],
            cwd=self.root, capture_output=True, text=True, timeout=90,
            env=dict(os.environ, HELM_SEAT_NAMES=self.names_file,
                     PYTHONPATH=source_pkg))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING", r.stderr)
        self.assertNotIn("REFUSED", r.stderr)

    def test_the_same_tree_IS_refused_when_the_seams_are_present(self):  # noqa: VACUOUS_ASSERTION — assertRefused pins rc==1 AND the REFUSED text inside a helper the analyzer cannot see into; this arm IS the positive control for the missing-seam stand-down above, which is why it drives the identical staged tree
        # The positive control for the arm above: identical staged state,
        # seams available, so rc=0 there is the install fault and nothing else.
        self.write("tests/test_x.py", 'ROW = "%s"\n' % A)
        self.assertRefused(self.rung())


class SeatNameFalsePositivesAreGoneTest(RungBase):
    """The other half, and the half that proves the refusals above are real.

    Under fail-closed a crash also returns rc=1. These arms are what
    distinguish "the rung detected something" from "the rung fell over".
    """

    def test_the_house_convention_passes(self):
        self.write("tests/test_x.py", 'ROW = {"seat": "seat-a"}\n')
        self.assertAllowed(self.rung())

    def test_plain_prose_naming_a_seat_passes(self):
        self.write("tests/test_x.py", 'MSG = "the %s pane died overnight"\n' % A)
        self.assertAllowed(self.rung())

    def test_prose_with_a_SEPARATELY_QUOTED_name_passes(self):  # noqa: VACUOUS_ASSERTION — the contract IS a non-refusal; its control is the sibling refuse-arm on the identical name as a bare VALUE, which the analyzer correctly reads as a separate observable
        # Measured false positive #2. The real-world case is a docstring
        # about case-folding, where both spellings ARE the subject.
        self.write("tests/test_x.py",
                   'DOC = \'Committer "%s" vs roster key "%s".\'\n' % (A, A))
        self.assertAllowed(self.rung())

    def test_a_mismatched_quote_fragment_passes(self):  # noqa: VACUOUS_ASSERTION — same non-refusal contract; the matched-quote case refuses one class over, so rc=0 here isolates the backreference and not an absent rung
        # Measured false positive #1: the old matcher had no backreference.
        self.write("tests/test_x.py", 'DOC = \'a "%s\\\' fragment\'\n' % A)
        self.assertAllowed(self.rung())

    def test_a_reasoned_noqa_passes(self):
        self.write("tests/test_x.py",
                   'ROW = "%s"  # noqa: SEAT_NAME - roster fixture\n' % A)
        self.assertAllowed(self.rung())

    def test_a_bare_noqa_does_NOT_pass(self):
        self.write("tests/test_x.py",
                   'ROW = "%s"  # noqa: SEAT_NAME\n' % A)
        self.assertRefused(self.rung())

    def test_a_marker_on_a_CONTINUED_statement_is_seen(self):  # noqa: VACUOUS_ASSERTION — the contract IS a non-refusal, and its control is the sibling refuse-arms on the identical name; reverting the escape to the physical-line rule reddens exactly this arm
        """A physical line ending in a backslash cannot carry a trailing
        comment — that is a syntax error — so on a continued statement the
        prescribed remedy is unwritable at the very line the refusal points
        at. The only markable line belongs to the same STATEMENT.

        Measured on task/2098: three refused commits and two re-cuts, the
        author annotating correctly every time and the guard declining to
        see it, because the escape was bound to the physical line.
        """
        self.write("tests/test_x.py",
                   'import mock\n'
                   'def t():\n'
                   # THE NAME IS ON THE BACKSLASH LINE, which is the whole
                   # point: that line cannot carry a trailing comment, so the
                   # marker has to go on another line of the same statement.
                   '    with mock.patch.object(a, "%s"), \\\n'
                   '            mock.patch.object(b, "g"):  '
                   '# noqa: SEAT_NAME - the family key, not the seat\n'
                   '        pass\n' % A)
        self.assertAllowed(self.rung())

    def test_a_marker_on_a_DIFFERENT_statement_is_NOT_seen(self):  # noqa: VACUOUS_ASSERTION — this asserts rc=1, a positive refusal, not an absence; widening the escape to the whole file reddens it
        """And the widening stops at the statement. An escape reasoned about
        one statement may not excuse the next one, or a single noqa anywhere
        in a file would retire the rung for everything below it."""
        self.write("tests/test_x.py",
                   'FIRST = "ok"  # noqa: SEAT_NAME - about THIS line only\n'
                   'SECOND = "%s"\n' % A)
        self.assertRefused(self.rung())

    def test_a_comment_ONLY_line_still_escapes_just_its_own_line(self):  # noqa: VACUOUS_ASSERTION — this asserts rc=1, a positive refusal, not an absence; letting a standalone comment mark the next statement reddens it, and so does a file-wide escape
        """A comment on its own line never reaches a NEWLINE token, so the
        logical-line grouping must not swallow the statement after it."""
        self.write("tests/test_x.py",
                   '# noqa: SEAT_NAME - a standalone remark\n'
                   'ROW = "%s"\n' % A)
        self.assertRefused(self.rung())

    def test_a_marker_in_the_BODY_does_not_reach_its_HEADER(self):  # noqa: VACUOUS_ASSERTION - this asserts rc=1, a positive refusal, not an absence; widening the escape from the logical line to the whole compound statement reddens exactly this arm
        """THE OTHER HALF OF THE PRINTED REMEDY, AND THE ONLY ARM THAT DRIVES IT.

        The refusal tells an author to put the marker on the header's own
        final line and NOT in the indented body. Its sibling above proves the
        header line WORKS; nothing proved the body line does not, so the
        sentence that sends authors to one rather than the other rested on a
        hand probe in one agent's scratchpad. That is the gap task/2424 was
        filed on: the commit message for f8e69f969 said the text was bound to
        the rule by an arm, and the arm bound two phrases.

        The mechanism is tokenize's NEWLINE. A compound statement's header is
        one logical line and its body is another, so the escape computed at
        the body's NEWLINE covers the body and stops. Measured on task/2421:
        a header marker escapes the header's physical lines and not the body,
        a body marker escapes the body and not the header.
        """
        self.write("tests/test_x.py",
                   'import mock\n'
                   'def t():\n'
                   # SAME FIXTURE AS THE ACCEPTING SIBLING, marker moved. The
                   # name is on the backslash line of the HEADER; the only
                   # difference is which logical line carries the noqa.
                   '    with mock.patch.object(a, "%s"), \\\n'
                   '            mock.patch.object(b, "g"):\n'
                   '        pass  '
                   '# noqa: SEAT_NAME - reasoned in the BODY, one logical '
                   'line too late\n' % A)
        self.assertRefused(self.rung())

    def test_the_refusal_prescribes_a_remedy_THE_RULE_ACCEPTS(self):
        """THE MESSAGE IS BOUND TO THE RULE BY THIS ARM AND BY NOTHING ELSE.

        It drifted once: the escape moved to the LOGICAL line and the refusal
        went on telling authors to mark the literal's own line, which on a
        backslash continuation is a syntax error — so the mechanism accepted
        the remedy the message forbade, and an author following the printed
        instruction exactly still failed. Nothing asserted the text, so
        nothing noticed. Vigilance is what failed; this arm is what does not.

        It also refuses the OTHER wrong wording. "the same statement" is
        false: a compound statement spans several logical lines and NEWLINE
        resets the escape at the header, so a marker in an indented body
        cannot exempt the header above it.
        """
        self.write("tests/test_x.py", 'ROW = "%s"\n' % A)
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "control: this must REFUSE, or what follows is not a "
                         "refusal text at all")
        self.assertIn("same LOGICAL line", r.stderr)
        self.assertIn("backslash", r.stderr,
                      "the continuation case is named — it is the one that "
                      "makes the naive remedy unwritable")
        self.assertNotIn("mark it on the same line", r.stderr,
                         "that phrase prescribes the literal's own PHYSICAL "
                         "line, which a continuation cannot carry")
        self.assertNotIn("same statement", r.stderr,
                         "and that one over-promises: a compound statement is "
                         "more than one logical line")
        self.assertIn("NOT its indented body", r.stderr,
                       "and the remedy must say WHICH line of a continued "
                       "compound statement takes the marker: the header's "
                       "own final line. Dropping this sentence while keeping "
                       "'logical line' and 'backslash' leaves the author "
                       "correctly told the rule and still unable to apply it "
                       "to the case the rule was written for.")
        self.assertIn("last comment-capable line", r.stderr,
                      "the phrase that makes the instruction executable on a "
                      "backslash continuation, where the obvious line cannot "
                      "carry a comment at all")

    def test_the_same_name_outside_tests_passes(self):
        self.write("helm/thing.py", 'SEAT = "%s"\n' % A)
        self.assertAllowed(self.rung())


class SeatNameAuthorityTest(RungBase):
    def test_an_unconfigured_authority_is_a_no_op_it_says_OUT_LOUD(self):
        self.write("tests/test_x.py", 'ROW = "%s"\n' % A)
        r = self.rung(HELM_SEAT_NAMES=os.path.join(self.tmp, "absent.txt"))
        self.assertAllowed(r)
        self.assertIn("NO-OP", r.stderr)
        self.assertIn("checked nothing", r.stderr)

    def test_a_MALFORMED_authority_refuses_rather_than_guarding_a_partial_list(self):
        bad = os.path.join(self.tmp, "bad.txt")
        with open(bad, "wb") as f:
            f.write(b"%s\n\xff\xfe not utf-8\n" % A.encode())
        self.write("tests/test_x.py", 'ROW = "seat-a"\n')
        r = self.rung(HELM_SEAT_NAMES=bad)
        self.assertRefused(r)
        self.assertIn("not valid UTF-8", r.stderr)

    def test_a_RELATIVE_authority_override_is_REFUSED_not_resolved(self):
        """A relative override names a different file from every working
        directory. The writer resolved it against its own cwd and the commit
        hook against the repository it was invoked from, so both believed they
        shared one authority while using two. Refusing is the only answer that
        cannot silently disagree with itself."""
        prior = os.environ.get("HELM_SEAT_NAMES")
        self.addCleanup(lambda: os.environ.__setitem__("HELM_SEAT_NAMES", prior)
                        if prior is not None
                        else os.environ.pop("HELM_SEAT_NAMES", None))
        os.environ["HELM_SEAT_NAMES"] = os.path.join("relative", "names.txt")
        path, invalid = seatname_guard.authority_path()
        self.assertIsNone(path)
        self.assertIn("relative", invalid)
        # AND IT FAILS CLOSED THROUGH THE READER, which is the property that
        # matters: an unresolvable authority must not read as an empty one.
        authority, _path, why = seatname_guard.load_seat_names()
        self.assertTrue(why, "an unresolvable authority reported no reason")
        self.assertEqual(len(authority), 0)

        # POSITIVE CONTROL on the same observable: an ABSOLUTE override
        # resolves cleanly, so the refusal above is about relativeness and not
        # about the variable merely being set.
        target = os.path.join(self.tmp, "abs-names.txt")
        os.environ["HELM_SEAT_NAMES"] = target
        path, invalid = seatname_guard.authority_path()
        self.assertIsNone(invalid)
        self.assertEqual(path, target)

    def test_authority_precedence_is_one_four_case_decision(self):  # noqa: VACUOUS_ASSERTION — every case positively writes and reads the selected authority; absent non-selected/default paths prove the same write did not escape there
        from helm import seats_estate, seats_roster
        default = os.path.join(os.environ["HOME"], ".helm", "_global",
                               "seat-names.txt")
        shapes = (
            ("legacy-only", None, "legacy", "legacy"),
            ("preferred-only", "preferred", None, "preferred"),
            ("preferred-wins", "preferred", "legacy", "preferred"),
            ("empty-preferred-falls-through", "", "legacy", "legacy"),
        )
        for label, helm_leaf, meld_leaf, expected_leaf in shapes:
            paths = {leaf: os.path.join(self.tmp, label + "-" + leaf + ".txt")
                     for leaf in ("preferred", "legacy")}
            helm_value = paths.get(helm_leaf, helm_leaf)
            meld_value = paths.get(meld_leaf, meld_leaf)
            expected = paths[expected_leaf]
            for key, value in (("HELM_SEAT_NAMES", helm_value),
                               ("MELD_SEAT_NAMES", meld_value)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            path, invalid, targeted = seatname_guard.authority_target()
            estate = seats_estate.estate()
            self.assertIsNone(invalid, label)
            self.assertTrue(targeted, label)
            self.assertEqual(path, expected, label)
            self.assertEqual(estate.projection, seats_estate.TARGETED, label)
            self.assertEqual(estate.authority_path, expected, label)
            self.assertTrue(seats_roster._project_seat_authority({A: {}}), label)
            authority, why = seatname_guard.read_authority(expected)
            self.assertIsNone(why, label)
            self.assertTrue(authority.covers(A), label)
            self.assertFalse(os.path.exists(default), label)
            for candidate in paths.values():
                if candidate != expected:
                    self.assertFalse(os.path.exists(candidate), label)

    def test_a_relative_legacy_override_names_the_variable_that_is_wrong(self):
        os.environ["MELD_SEAT_NAMES"] = os.path.join("relative", "legacy.txt")
        path, invalid = seatname_guard.authority_path()
        self.assertIsNone(path)
        self.assertIn("MELD_SEAT_NAMES is relative", invalid)

    def test_read_authority_is_TOTAL_on_malformed_bytes(self):
        # It runs AFTER install has written hooks and snapshots, outside the
        # rollback, so it may not raise. Absence and malformation differ.
        bad = os.path.join(self.tmp, "bad2.txt")
        with open(bad, "wb") as f:
            f.write(b"\xff\xfe\n")
        armed, held, why = _read_authority_sets(bad)
        self.assertEqual((armed, held), (frozenset(), frozenset()))
        self.assertIsNotNone(why)
        gone = _read_authority_sets(os.path.join(self.tmp, "nope"))
        self.assertIsNone(gone[2], "absent is proven-empty, not malformed")


class SeatNameAuthorityFreshnessTest(RungBase):
    """The roster PUSHES. Install-time notes cannot keep a file current: after
    install, adding a seat left every byte-comparing staleness check green
    while the new identity went unguarded."""

    def test_a_CRASH_BETWEEN_THE_TWO_WRITES_LEAVES_THE_SAFE_RESIDUE(self):
        """Order is not a preference here, because the two failures are not
        symmetric. Arming first means a crash before the roster write leaves an
        EXTRA arm — a name armed for a seat the roster does not list, which
        refuses slightly too much and heals on the next write. The old order
        left the opposite residue: a live seat in the roster that the authority
        never learned, so the rung passed commits carrying that identity and
        nothing ever noticed it was missing.
        """
        from helm import seats_roster
        auth = os.path.join(self.tmp, "ordered-auth.txt")
        os.environ["HELM_SEAT_NAMES"] = auth

        boom = []
        real_write = seats_roster.pk.write_json

        def exploding(path, data, *a, **kw):
            if path.endswith(".roster.json"):
                boom.append(path)
                raise OSError("simulated crash between the two writes")
            return real_write(path, data, *a, **kw)

        seats_roster.pk.write_json = exploding
        try:
            with contextlib.suppress(OSError):
                seats_roster.write_roster(A, session="s" * 12)
        finally:
            seats_roster.pk.write_json = real_write
        self.assertTrue(boom, "the roster write never ran, so nothing was proven")

        armed, _held, why = _read_authority_sets(auth)
        self.assertIsNone(why)
        self.assertIn(A, armed,
                      "the authority was NOT armed first — a crash here would "
                      "leave a seat the authority never learned")
        self.assertNotIn(A, seats_roster.roster(),
                         "the roster write was supposed to have failed")

        # POSITIVE CONTROL: with no crash, both land and the roster carries it.
        seats_roster.write_roster(A, session="s" * 12)
        self.assertIn(A, seats_roster.roster())

    def test_a_METADATA_VERB_CANNOT_MINT_AN_IDENTITY(self):
        """A mute for a seat that does not exist must refuse, not create it.

        `r.get(seat) or {}` followed by `r[seat] = row` admitted ANY token,
        and because projection is ADDITIVE a mistyped --seat then armed that
        typo in the machine-global authority permanently — nothing removes an
        armed name. A metadata verb states something ABOUT a seat and
        presupposes one; admission belongs to the join and rename doors.
        """
        from helm import seats_roster
        auth = os.path.join(self.tmp, "targeted-auth.txt")
        os.environ["HELM_SEAT_NAMES"] = auth
        seats_roster.write_roster(A, session="s" * 12)
        self.assertIn(A, seats_roster.roster(), "the control seat never joined")

        typo = "zz-synthetic-typo-not-a-seat"
        from helm import seats_mute
        ok, msg = seats_mute.set_mute(typo, "some-room")
        self.assertFalse(ok, "a metadata verb admitted an unknown identity")
        self.assertIn("no seat", msg)
        self.assertNotIn(typo, seats_roster.roster(),
                         "the refused mute still minted a roster row")
        armed, held, _why = _read_authority_sets(auth)
        self.assertNotIn(typo, armed | held,
                         "a typo reached the authority and nothing removes it")

        # POSITIVE CONTROL on the same observable: mute WORKS for a seat that
        # exists, so the refusal above is about admission and not about mute
        # being broken.
        ok, _msg = seats_mute.set_mute(A, "some-room")
        self.assertTrue(ok, "mute stopped working for a seat that exists")
        self.assertIn("some-room", seats_mute.mutes(A))
        self.assertIn(A, armed, "the control seat never reached the authority")

    def test_a_new_roster_seat_is_armed_immediately(self):
        path = os.path.join(self.tmp, "auth.txt")
        with open(path, "w") as f:
            f.write("%s\n" % A)
        added = seatname_guard.refresh_authority({A: {}, B: {}}, path=path)
        self.assertEqual(added, (B,))
        armed, _held, why = _read_authority_sets(path)
        self.assertIsNone(why)
        self.assertIn(B, armed)

    def test_a_roster_write_never_re_arms_a_HELD_BACK_name(self):
        # The curated exclusions are judgement (a name that is also a provider
        # value would refuse honest tests); a roster write must not undo them.
        path = os.path.join(self.tmp, "auth2.txt")
        with open(path, "w") as f:
            f.write("%s\n!%s\n" % (A, B))
        self.assertEqual(seatname_guard.refresh_authority({A: {}, B: {}},
                                                          path=path), ())
        armed, held, _ = _read_authority_sets(path)
        self.assertNotIn(B, armed)
        self.assertIn(B, held)

    def test_a_FAILED_projection_is_durable_and_the_rung_refuses(self):  # noqa: VACUOUS_ASSERTION — asserts a NON-EMPTY why, a REFUSED rc, marker-cleared, and a subsequent rc=0 — four unconditional positives; the empty-looking one is assertFalse(exists(marker)), which is the whole point of the clearing half
        # A best-effort push that fails silently recreates the stale authority
        # it exists to prevent, so the failure gets a marker and the marker is
        # a second freshness owner: do not fail the roster write, but do not
        # let the failure vanish either.
        path = os.path.join(self.tmp, "auth4.txt")
        with open(path, "w") as f:
            f.write("%s\n" % A)
        seatname_guard._mark_stale(path, "simulated projection failure")
        _armed, _held, why = _read_authority_sets(path)
        self.assertIsNotNone(why)
        self.assertIn("STALE", why)
        self.write("tests/test_x.py", 'ROW = "seat-a"\n')   # otherwise clean
        r = self.rung(HELM_SEAT_NAMES=path)
        self.assertRefused(r)
        # A successful refresh CLEARS it, or the marker would be a one-way trap.
        seatname_guard.refresh_authority({A: {}, B: {}}, path=path)
        self.assertFalse(os.path.exists(seatname_guard.stale_marker(path)))
        self.assertAllowed(self.rung(HELM_SEAT_NAMES=path))

    def test_a_FRESH_estate_initialises_its_authority_parent(self):
        # The lock was opened BEFORE the parent existed, so a fresh estate
        # left neither an authority nor a stale marker — silent in both
        # directions, which is the one state a guard may never reach.
        fresh = os.path.join(self.tmp, "brand", "new", "_global", "names.txt")
        added = seatname_guard.refresh_authority({A: {}}, path=fresh)
        self.assertEqual(added, (A,))
        self.assertTrue(os.path.exists(fresh))
        armed, _held, why = _read_authority_sets(fresh)
        self.assertIsNone(why)
        self.assertIn(A, armed)
        self.assertFalse(os.path.exists(seatname_guard.stale_marker(fresh)))

    def test_a_case_variant_never_RE_ARMS_a_held_back_name(self):
        # THE WORST OUTCOME THIS FILE CAN PRODUCE, and it was reachable: the
        # membership test compared RAW spellings, so a deliberately held
        # `!Seat-A` did not match a live `seat-a`, the name was appended bare,
        # and the operator's exclusion was silently reversed. Seat identity
        # folds case everywhere else in helm; it must fold here too.
        path = os.path.join(self.tmp, "case.txt")
        with open(path, "w") as f:
            f.write("# a\n%s\n!Seat-A\n" % A)
        added = seatname_guard.refresh_authority(
            {"seat-a": {}, A.upper(): {}, "zz-genuinely-new": {}}, path=path)
        self.assertEqual(added, ("zz-genuinely-new",))
        armed, held, why = _read_authority_sets(path)
        self.assertIsNone(why)
        self.assertFalse([n for n in armed if n.casefold() == "seat-a"],
                         "a held-back name was re-armed by a case variant")
        # THE CANONICAL KEY IS FOLDED, so the held entry comes back as
        # `seat-a` however the file spelled it. That is the cure this arm is
        # about: the authority now has ONE key per identity, which is why a
        # case variant can no longer walk past it.
        self.assertIn("seat-a", held)
        self.assertEqual([n for n in armed if n.casefold() == A.casefold()],
                         [A.casefold()])
        self.assertIn("zz-genuinely-new", armed)

    def test_an_EMPTIED_roster_cannot_erase_the_authority(self):  # noqa: VACUOUS_ASSERTION — the empty-looking assertion is refresh_authority(...)==() , i.e. NO ADDITIONS, which is the contract; the same read is pinned positively on the line below by armed=={A} and held=={B}, and a wipe would fail those two
        # CONTAINMENT AGAINST A ROSTER-EMPTYING WRITE PATH. The projection is
        # additive by construction — it never removes a name — so a roster
        # that goes empty upstream degrades coverage to "no new names", never
        # to an erased authority. A departed seat's identity is still
        # publication debt; over-guarding is safe, under-guarding is the bug.
        path = os.path.join(self.tmp, "contain.txt")
        with open(path, "w") as f:
            f.write("# a\n%s\n!%s\n" % (A, B))
        with open(path, encoding="utf-8") as f:
            before = f.read()
        self.assertEqual(seatname_guard.refresh_authority({}, path=path), ())
        self.assertEqual(
            seatname_guard.refresh_authority({"": {}, "   ": {}}, path=path), ())
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)
        armed, held, why = _read_authority_sets(path)
        self.assertIsNone(why)
        self.assertEqual(armed, frozenset({A}))
        self.assertEqual(held, frozenset({B}))
        self.assertFalse(os.path.exists(seatname_guard.stale_marker(path)))

    def test_the_refresh_is_idempotent(self):  # noqa: VACUOUS_ASSERTION — the empty-looking assertion is held==frozenset(); the SAME read is pinned positively by armed=={A,B} on the line above, which is the control that a truncating refresh would fail
        path = os.path.join(self.tmp, "auth3.txt")
        with open(path, "w") as f:
            f.write("%s\n" % A)
        seatname_guard.refresh_authority({A: {}, B: {}}, path=path)
        seatname_guard.refresh_authority({A: {}, B: {}}, path=path)
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines()
                     if l.strip() and not l.startswith("#")]
        self.assertEqual(len(lines), len(set(lines)), lines)
        # PIN THE COMPLETE CONTENT, not merely the absence of duplicates: a
        # refresh that TRUNCATED the authority to empty satisfied "no
        # duplicate lines" perfectly.
        armed, held, why = _read_authority_sets(path)
        self.assertIsNone(why)
        self.assertEqual(armed, frozenset({A, B}))
        self.assertEqual(held, frozenset())

    def test_only_the_doors_that_ADMIT_an_identity_project(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertNotIn('def _save_roster'), a must-hit guarding the two equalities below it; both are unconditional positives over lists the scanner must produce non-empty, so a scan that read nothing fails loudly
        # THE SEAM MOVED, AND WHY IS THE POINT. An earlier version hoisted all
        # five writes into one _save_roster helper — tidier, and it made the
        # roster-write guard's arm BLIND. Transactional rename now publishes
        # through a callback, so this scanner follows the AST rather than one
        # indentation shape: a lambda is still owned by rename_seat.
        # TWO MODULES, ONE SCAN. The wake filter left the roster's module for
        # its own, so scanning one file would let a roster publication leave
        # this scanner's view by the simple act of MOVING — which is exactly
        # the blindness the arm exists to catch, arriving as a refactor
        # instead of as a helper.
        fns = []
        for name in ("seats_roster.py", "seats_mute.py",
                     "seats_incarnation.py"):
            with open(os.path.join(os.path.dirname(RUNG), name),
                      encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("def _save_roster", src,
                             "the hoisted seam is back in %s" % name)
            fns.extend(node for node in ast.parse(src).body
                       if isinstance(node, ast.FunctionDef))
        writes, projects = [], set()
        for fn in fns:
            for call in (node for node in ast.walk(fn)
                         if isinstance(node, ast.Call)):
                if isinstance(call.func, ast.Attribute) \
                        and isinstance(call.func.value, ast.Name) \
                        and (call.func.value.id, call.func.attr) == ("pk", "write_json") \
                        and call.args and isinstance(call.args[0], ast.Call) \
                        and isinstance(call.args[0].func, ast.Name) \
                        and call.args[0].func.id == "roster_path":
                    writes.append(fn.name)
                if isinstance(call.func, ast.Name) \
                        and call.func.id == "_project_seat_authority":
                    projects.add(fn.name)
        admitting = {"write_roster", "rename_seat"}
        # METADATA, NOT ADMITTING: `migrate_incarnations` stamps the identity
        # generation onto rows that ALREADY EXIST. Every name it writes was
        # already a key, so it can mint no seat and resolve no name — it
        # publishes the roster and must never project authority.
        metadata = {"set_mute", "rehome_seat", "disown_session",
                    "migrate_incarnations"}
        self.assertEqual(sorted(writes),
                         sorted(["write_roster", "disown_session",
                                 "rename_seat", "set_mute", "rehome_seat",
                                 "migrate_incarnations"]),
                         "a roster publication left the scanner's view")
        self.assertEqual(projects, admitting,
                         "projection must live at exactly the doors that ADMIT "
                         "an identity; found %s" % sorted(projects))
        self.assertFalse(projects & metadata,
                         "a metadata verb projects, so it pays the global lock "
                         "for an identity it cannot introduce")


class SeatNameSandboxNeverTouchesTheHostTest(RungBase):
    """A SANDBOX ROSTER MAY NOT WRITE THE OPERATOR'S GLOBAL AUTHORITY.

    This is the arm the estate actually needed: the projection ignored estate
    isolation, so every suite run that built a scratch roster under a
    temporary HELM_HOME persisted its synthetic keys into the real
    ~/.helm/_global/seat-names.txt and left them after teardown. Four such
    names were found in the live file by review. The pre-existing cross-file
    arm could not see it because it COUNTED SOURCE STRINGS — it asserted the
    call sites existed, never that they behaved.
    """

    def real_authority_digest(self):
        import hashlib
        path = os.path.join(os.path.expanduser("~"), ".helm", "_global",
                            "seat-names.txt")
        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except OSError:
            return None

    def test_projection_ownership_AGREES_WITH_THE_ROSTER_ACTUALLY_WRITTEN(self):
        """THE PROPERTY, NOT AN ENUMERATION OF ENV SHAPES.

        A process may keep the machine-global authority current exactly when
        the roster it writes IS the production roster. Every predicate this
        replaced tried to reconstruct that from the environment and each cured
        case taught the next one, so this arm asserts the invariant directly
        over the shapes that broke it — including the two the old predicate
        got WRONG IN THE DANGEROUS DIRECTION, writing a live seat into the
        production roster and then declining to project it, so the authority
        never learned about a seat the roster had.
        """
        from helm import chat, home, seats_common, seats_roster
        production = os.path.join(home.default_surface(chat.DEFAULT_DIR),
                                  ".roster.json")
        shapes = [
            ("no overrides", {}),
            ("redirected home", {"HELM_HOME": os.path.join(self.tmp, "iso")}),
            ("explicit production chat dir beside an isolated home",
             {"HELM_CHAT_DIR": home.default_surface(chat.DEFAULT_DIR),
              "HELM_HOME": os.path.join(self.tmp, "iso")}),
            ("empty HELM_HOME beside a relative legacy root",
             {"HELM_HOME": "", "MELD_HOME": "rel-sandbox"}),
            ("legacy chat override", {"MELD_CHAT_DIR": os.path.join(self.tmp, "bus")}),
        ]
        seen_both = set()
        for label, env in shapes:
            for key in ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR",
                        "MELD_CHAT_DIR", "HELM_SEAT_NAMES"):
                os.environ.pop(key, None)
            os.environ.update(env)
            writes_production = (os.path.realpath(seats_common.roster_path())
                                 == os.path.realpath(production))
            projects = seats_roster.estate().projects()
            self.assertEqual(
                projects, writes_production,
                "%s: projects=%s but writes_production=%s — projection must "
                "follow the FILE, not the environment" % (label, projects,
                                                          writes_production))
            seen_both.add(writes_production)
        # THE CONTROL: the shapes must actually exercise BOTH answers. An
        # invariant that only ever saw sandboxes would pass while proving
        # nothing about the production case, which is the half that writes.
        self.assertEqual(seen_both, {True, False},
                         "the shapes did not cover both production and sandbox")

    def test_a_sandbox_roster_leaves_the_real_authority_BYTE_IDENTICAL(self):  # noqa: VACUOUS_ASSERTION — UNCHANGED IS THE CONTRACT: the digest equality is the finding, and assertFalse(_owns_the_global_authority) is its unconditional positive control — mutation-proven, forcing the predicate to always-true reddens this arm
        from helm import seats_roster
        before = self.real_authority_digest()
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "sandbox-estate")
        os.environ.pop("HELM_SEAT_NAMES", None)
        self.assertFalse(seats_roster._owns_the_global_authority(),
                         "a redirected estate must not own the global file")
        seats_roster._project_seat_authority({"zz-sandbox-only": {}})
        self.assertEqual(self.real_authority_digest(), before,
                         "a sandbox roster mutated the operator's authority")

    def test_a_HELM_CHAT_DIR_sandbox_also_cannot_write_the_host(self):  # noqa: VACUOUS_ASSERTION — unchanged-is-the-contract; assertFalse(_owns_the_global_authority) is the unconditional positive control, and the sibling arm proves the projection still writes when it legitimately owns a target
        # THE FIRST CURE WAS HALF A CURE. It gated on HELM_HOME and I reported
        # the boundary closed; HELM_CHAT_DIR is an INDEPENDENT documented
        # override, so a sandbox using only it still wrote the operator's
        # machine-global authority. The property is "this estate is the
        # production one" — every override that redirects the roster's surface
        # falsifies it, not just the one I happened to look at.
        from helm import seats_roster
        before = self.real_authority_digest()
        os.environ.pop("HELM_HOME", None)
        os.environ.pop("HELM_SEAT_NAMES", None)
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "sandbox-chat")
        self.assertFalse(seats_roster._owns_the_global_authority())
        seats_roster._project_seat_authority({"zz-chatdir-sandbox": {}})
        self.assertEqual(self.real_authority_digest(), before)

    def test_the_LEGACY_MELD_override_also_cannot_write_the_host(self):  # noqa: VACUOUS_ASSERTION — unchanged-is-the-contract; assertFalse(_owns_the_global_authority) is the unconditional positive control and the production case in the same class proves the predicate still says True when it should
        # THE ARM THAT EXISTS BECAUSE I EXECUTED MY OWN DOCSTRING. The comment
        # claimed the predicate tested the PROPERTY so that ANY surface
        # redirect falsified it; the code compared chat_dir() against the very
        # function chat_dir() delegates to — a value against itself, never
        # False — leaving one enumerated env string doing all the work.
        # MEASURED before the fix: MELD_CHAT_DIR redirected the surface, the
        # predicate returned True, and a sandbox name reached the real
        # authority. Reading the claim would never have found it.
        from helm import seats_roster
        before = self.real_authority_digest()
        for k in ("HELM_HOME", "HELM_SEAT_NAMES", "HELM_CHAT_DIR"):
            os.environ.pop(k, None)
        os.environ["MELD_CHAT_DIR"] = os.path.join(self.tmp, "legacy-sandbox")
        try:
            self.assertFalse(seats_roster._owns_the_global_authority())
            seats_roster._project_seat_authority({"zz-legacy-sandbox": {}})
            self.assertEqual(self.real_authority_digest(), before)
        finally:
            os.environ.pop("MELD_CHAT_DIR", None)

    def test_an_EXPLICIT_sandbox_authority_still_receives_its_names(self):  # noqa: VACUOUS_ASSERTION — assertIn on the sandboxed authority is the unconditional positive; the digest equality beside it proves the projection wrote THERE and not to the host
        # The control: the predicate must not simply disable the projection,
        # or the freshness owner would be dead and this suite would not notice.
        from helm import seats_roster
        target = os.path.join(self.tmp, "sandboxed-authority.txt")
        with open(target, "w") as f:
            f.write("# sandbox\n")
        os.environ["HELM_SEAT_NAMES"] = target
        before = self.real_authority_digest()
        self.assertTrue(seats_roster._owns_the_global_authority())
        seats_roster._project_seat_authority({"zz-sandbox-only": {}})
        armed, _held, why = _read_authority_sets(target)
        self.assertIsNone(why)
        self.assertIn("zz-sandbox-only", armed)
        self.assertEqual(self.real_authority_digest(), before)

    def test_a_MELD_only_target_receives_projection_and_the_rung_reads_it(self):  # noqa: VACUOUS_ASSERTION — the unchanged default digest is controlled by the name appearing in the legacy target and by the real subprocess refusing that same name
        from helm import seats_roster
        target = os.path.join(self.tmp, "legacy-authority.txt")
        os.environ["MELD_SEAT_NAMES"] = target
        before = self.real_authority_digest()

        self.assertTrue(seats_roster._owns_the_global_authority())
        self.assertTrue(seats_roster._project_seat_authority({A: {}}))
        armed, _held, why = _read_authority_sets(target)
        self.assertIsNone(why)
        self.assertIn(A, armed)
        self.assertEqual(self.real_authority_digest(), before)

        # Exercise the installed-script shape, not only the imported module.
        self.write("tests/test_legacy_target.py", 'ROW = {"seat": "%s"}\n' % A)
        env = dict(os.environ)
        env.pop("HELM_SEAT_NAMES", None)
        env["MELD_SEAT_NAMES"] = target
        r = subprocess.run([sys.executable, RUNG, "--staged"], cwd=self.root,
                           capture_output=True, text=True, timeout=90, env=env)
        self.assertRefused(r, repr(A))

class SeatNameHookWiringTest(RungBase):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def test_install_snapshots_the_rung_beside_the_shared_hook(self):
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("HELM_SEATNAME_SKIP=1", out)
        assets = _guard._scanner_assets(self.root)
        installed = next(p for p in assets if p.endswith("/seatname_guard.py"))
        with open(installed, "rb") as got, open(RUNG, "rb") as want:
            snap, source = got.read(), want.read()
        self.assertIn(b"[helm seat-name] REFUSED", snap)   # two empty files
        self.assertEqual(snap, source)                     # also compare equal
        with open(work.hook_path(self.root, "pre-commit")) as f:
            body = f.read()
        self.assertEqual(body.count('python3 "$seatname" --staged'), 1)
        self.assertNotIn(RUNG, body, "a source lane path is disposable")

    def test_a_commit_carrying_a_real_seat_name_is_REFUSED_end_to_end(self):
        rc, _out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.write("tests/test_planted.py", 'ROW = "%s"\n' % A)
        r = self.sh("git", "commit", "-m", "planted",
                    env={"HELM_SEAT_NAMES": self.names_file})
        self.assertNotEqual(r.returncode, 0, "the commit MUST be refused")
        self.assertIn("[helm seat-name] REFUSED", r.stderr)

    def test_the_convention_commits_through_the_same_hook(self):
        rc, _out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.write("tests/test_planted.py", 'ROW = "seat-a"\n')
        r = self.sh("git", "commit", "-m", "clean",
                    env={"HELM_SEAT_NAMES": self.names_file})
        self.assertEqual(r.returncode, 0, r.stderr)


class SeatNameFamilyKeysAreNotIdentitiesTest(RungBase):
    """The harness names a seat after its family when it knows nothing more
    specific, so the authority carries family words that tests use as
    provider and catalog VALUES. A family key is HELD by construction; a
    seat name that only CONTAINS one is still armed, and a file that names a
    key both ways is still a CONFLICT. Every family word below comes from
    FAMILY_KEYS at run time."""

    def family(self):
        return sorted(seatname_guard.FAMILY_KEYS)[0]

    def arm(self, *names):
        with open(self.names_file, "w", encoding="utf-8") as f:
            f.write("# synthetic\n%s\n" % "\n".join(names))

    def test_every_family_key_the_file_arms_is_held(self):
        keys = sorted(seatname_guard.FAMILY_KEYS)
        authority = seatname_guard.parse_authority("\n".join(keys + [A]))
        self.assertIn(A, authority, "must-hit: an ordinary name is armed")
        for key in keys:
            self.assertEqual(authority.state(key), seatname_guard.HELD, key)
            self.assertNotIn(key, authority, key)
            self.assertTrue(authority.covers(key), key)
            self.assertEqual(authority.state(key.upper()),
                             seatname_guard.HELD, key)

    def test_a_name_containing_a_family_is_still_armed(self):
        fam = self.family()
        authority = seatname_guard.parse_authority(
            "\n".join((fam, fam + "-2", "zz-synthetic-" + fam)))
        self.assertEqual(authority.state(fam), seatname_guard.HELD)
        self.assertIn(fam + "-2", authority)
        self.assertIn("zz-synthetic-" + fam, authority)

    def test_a_family_key_named_both_ways_is_still_a_conflict(self):
        fam = self.family()
        authority = seatname_guard.parse_authority("%s\n!%s\n" % (fam, fam))
        self.assertEqual(authority.state(fam), seatname_guard.CONFLICT)
        self.assertIn(fam, authority)

    def test_the_rung_passes_a_family_value_and_refuses_a_seat_name(self):
        fam = self.family()
        self.arm(fam, fam + "-2")
        self.write("tests/test_x.py", 'ROW = {"provider": "%s"}\n' % fam)
        self.assertAllowed(self.rung())
        self.write("tests/test_x.py", 'ROW = {"seat": "%s-2"}\n' % fam)
        self.assertRefused(self.rung(), fam + "-2")

    def test_family_keys_cover_every_family_the_code_knows(self):
        from helm import homes, seat, seat_catalog, seats_identity  # noqa: F401 — the facade before its impl module
        known = (set(seat_catalog.FAMILIES) | set(homes.ROOTS)
                 | set(seats_identity._FAMILIES))
        self.assertTrue(known)
        missing = sorted(known - seatname_guard.FAMILY_KEYS)
        self.assertEqual(missing, [], "add these family keys to "
                                      "seatname_guard.FAMILY_KEYS: %s" % missing)


if __name__ == "__main__":
    unittest.main()


class SeatNameReviewedBypassesTest(RungBase):
    """The three exact bypasses a cross-family reviewer found by READING a
    tree whose gate was green on 10,914 tests. Each is pinned in the polarity
    the reviewer named, because the existing arms covered the other one."""

    def test_a_PERCENT_FORMATTED_constant_is_a_DOCUMENTED_gap_not_a_silent_one(self):
        """`"zz-synthetic-%s" % "seat"` IS decided in the source and DOES
        produce an armed identity, and this rung does NOT refuse it.

        That is a gap, and this arm exists so it stays a STATED one. The
        docstring once promised refusal for "operands ALL constant" while
        _folded recognised only Constant and BinOp(Add) — a promise wider
        than the code, which is the exact failure the narrowed claim exists
        to stop making. So the arm asserts BOTH halves: the line passes, AND
        the module says in its own words that this form is not covered. If
        someone implements %-folding later, this arm fails and makes them
        update the promise in the same commit.
        """
        head, tail = A.rsplit("-", 1)
        self.write("tests/test_x.py",
                   'ROW = "%s-%%s" %% "%s"\n' % (head, tail))
        self.assertAllowed(self.rung())
        with open(RUNG, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("%-FORMATTING AND CONSTANT F-STRINGS ARE NOT IN IT", src,
                      "the gap stopped being documented")

    def test_an_ESCAPED_json_scalar_cannot_launder_through_an_OLD_direct_one(self):
        """An ADDED escaped spelling whose decoded value also appears on an
        OLD unchanged line used to pass.

        scan_structured located raw spellings only, so the escaped occurrence
        matched nothing; the value was deduped to one entry, the OLD line was
        located, and the added-range filter then dropped it. The line-0
        fallback could not fire because it only triggered when NOTHING
        matched. Absence-of-any is not absence-of-this-one.
        """
        tail = B[-1]
        head = B[:-1]
        self.write("tests/fixture.json",
                   '{\n  "old": "%s",\n  "pad": "x"\n}\n' % B)
        self.assertEqual(self.sh("git", "commit", "-qm", "base",
                                 "--no-verify").returncode, 0)
        # line 3 replaced: the SAME identity, written with its last character
        # as a \\u escape, so no raw spelling of it exists on the added line.
        self.write("tests/fixture.json",
                   '{\n  "old": "%s",\n  "new": "%s\\u%04x"\n}\n'
                   % (B, head, ord(tail)))
        self.assertRefused(self.rung())

    def test_a_LOCK_that_cannot_be_ACQUIRED_marks_the_authority_STALE(self):
        """A projection that could not RUN must leave the marker that makes
        the rung refuse.

        refresh_authority returned bare when parent creation, os.open or
        flock raised, and only LATER write failures marked stale. So a lock
        setup failure published a new identity to the roster while the old
        authority stayed readable and apparently fresh, and every later commit
        carrying that identity passed. Same class as marking an obligation
        delivered before attempting delivery.
        """
        auth = os.path.join(self.tmp, "locked", "auth.txt")
        os.makedirs(os.path.dirname(auth))
        with open(auth, "w") as f:
            f.write("%s\n" % A)
        # a DIRECTORY where the lock file belongs: os.open raises EISDIR
        os.makedirs(auth + ".lock")
        added = seatname_guard.refresh_authority({A: {}, B: {}}, path=auth)
        self.assertEqual(added, (), "it claimed to have armed something")
        marker = seatname_guard.stale_marker(auth)
        self.assertTrue(os.path.exists(marker),
                        "a projection that could not run left no stale marker, "
                        "so the rung goes on passing commits")
        with open(marker, encoding="utf-8") as f:
            self.assertIn("lock", f.read())

        # POSITIVE CONTROL on the same observable: with the lock path usable,
        # the refresh ARMS and leaves NO marker.
        os.rmdir(auth + ".lock")
        self.assertEqual(seatname_guard.refresh_authority({A: {}, B: {}},
                                                          path=auth), (B,))
        self.assertFalse(os.path.exists(marker), "a clean refresh left a marker")


class SeatNameUnguardedAdmitTest(RungBase):
    """The marker shares a fault domain with what it reports.

    An unwritable authority directory defeats os.open(<auth>.lock) AND the
    <auth>.stale write identically, so "mark stale on every pre-lock failure"
    is impossible to honour there. The reviewer's earlier arm covered only
    EISDIR-at-lock WHILE THE PARENT STAYED WRITABLE, which proves _mark_stale
    was CALLED and not that durable refusal state EXISTS.
    """

    def _readonly_authority(self):
        d = os.path.join(self.tmp, "ro")
        os.makedirs(d)
        auth = os.path.join(d, "auth.txt")
        with open(auth, "w") as f:
            f.write("%s\n" % A)
        os.chmod(d, 0o500)
        self.addCleanup(self._reopen_if_present, d)
        return d, auth

    def _reopen_if_present(self, d):
        """Re-open the directory for teardown IF IT IS STILL THERE.

        unittest runs tearDown BEFORE doCleanups, and RungBase.tearDown
        rmtree's self.tmp. So an arm that restores 0700 mid-test (both
        positive controls here do) lets the tree be removed completely, and an
        unconditional chmod then fires on a path that no longer exists —
        FileNotFoundError, reported as an ERROR with the arm's own assertions
        already green.

        The arm that leaves the directory 0500 survived only because
        rmtree(ignore_errors=True) cannot unlink inside it, so the directory
        was still standing when the cleanup ran. That is the tell: a cleanup
        whose success depends on whether the test restored a permission is
        not a cleanup, it is a coin flip that happened to land twice.
        """
        if os.path.isdir(d):
            os.chmod(d, 0o700)

    def test_an_UNWRITABLE_authority_dir_reports_UNGUARDED_not_success(self):
        d, auth = self._readonly_authority()
        if os.access(d, os.W_OK):      # root ignores the mode; the arm cannot run
            self.skipTest("this uid can write a 0500 directory")
        got = seatname_guard.refresh_authority({A: {}, B: {}}, path=auth)
        self.assertIsNone(got, "an unrecordable failure reported as recorded")
        self.assertFalse(os.path.exists(seatname_guard.stale_marker(auth)),
                         "the marker landed, so this arm is not testing the "
                         "fault domain it claims to")

        # POSITIVE CONTROL on the same observable: with the directory writable,
        # the same call ARMS and answers with the added name.
        os.chmod(d, 0o700)
        self.assertEqual(
            seatname_guard.refresh_authority({A: {}, B: {}}, path=auth), (B,))

    def test_an_ADMITTING_write_REFUSES_when_it_cannot_guard_the_identity(self):
        """A seat published while nothing can refuse it is a seat that reaches
        a public-bound commit unchallenged. The join door declines instead."""
        from helm import seats_roster
        d, auth = self._readonly_authority()
        if os.access(d, os.W_OK):
            self.skipTest("this uid can write a 0500 directory")
        os.environ["HELM_SEAT_NAMES"] = auth
        with self.assertRaises(OSError) as caught:
            seats_roster.write_roster(B, session="s" * 12)
        self.assertIn("could not be armed", str(caught.exception))
        self.assertNotIn(B, seats_roster.roster(),
                         "the refused identity was published anyway")

        # POSITIVE CONTROL: writable again, the identical call SUCCEEDS and
        # the seat lands — so the refusal is about the fault, not about B.
        os.chmod(d, 0o700)
        seats_roster.write_roster(B, session="s" * 12)
        self.assertIn(B, seats_roster.roster())

    def test_an_EXISTING_seat_keeps_updating_when_the_authority_is_unwritable(self):
        """THE REFUSAL BINDS THE ADMISSION, NOT THE FUNCTION.

        write_roster is the SessionStart join, the delivery registration, the
        spawn mirror and every presence refresh. An earlier cut raised on ANY
        authority fault, so an unwritable directory broke existing seats'
        presence and delivery even though their identity was already armed and
        their metadata introduces no unguarded name — an outage bought for
        nothing. The absent-seat arm above could not see it, because a
        previously-absent seat is exactly the case that SHOULD refuse.
        """
        from helm import seats_roster
        d, auth = self._readonly_authority()
        os.environ["HELM_SEAT_NAMES"] = auth

        # A joins while everything is writable, so it is an EXISTING seat.
        # THE HELPER HANDS BACK A 0500 DIRECTORY, so this arm has to open it
        # first: without the chmod the "existing" seat is refused AS NEW at
        # this very line, and the arm errors before reaching the claim it
        # exists to test. The prose said "while everything is writable" and
        # the fixture did not produce that state.
        os.chmod(d, 0o700)
        seats_roster.write_roster(A, session="s" * 12)
        self.assertIn(A, seats_roster.roster(), "the control seat never joined")

        os.chmod(d, 0o500)
        if os.access(d, os.W_OK):
            self.skipTest("this uid can write a 0500 directory")

        # PIN THE PROJECTION CALL, not only the persistence. Without this the
        # arm passes just as well if projection is SHORT-CIRCUITED for
        # non-admitting writes — which the cure comment explicitly forbids,
        # because then the authority stops converging on names added by other
        # paths. An arm that protects one of two required halves reads as
        # protecting both.
        calls = []
        real_proj = seats_roster._project_seat_authority
        self.addCleanup(setattr, seats_roster, "_project_seat_authority",
                        real_proj)
        seats_roster._project_seat_authority = (
            lambda r: (calls.append(1), real_proj(r))[1])

        # A presence/metadata refresh on that SAME seat must still persist.
        seats_roster.write_roster(A, session="t" * 12)
        self.assertTrue(calls, "projection was short-circuited on a "
                               "non-admitting write")
        row = seats_roster.roster().get(A) or {}
        self.assertIn("t" * 12,
                      [row.get("session")] + list(row.get("sessions") or []),
                      "an already-armed seat could not record a new session "
                      "while the authority was unwritable")

        # PAIRED POLARITY on the identical fault: a seat the roster has never
        # seen still REFUSES, so this arm cannot pass by disarming the guard.
        with self.assertRaises(OSError):
            seats_roster.write_roster(B, session="u" * 12)
        self.assertNotIn(B, seats_roster.roster())


class SeatNameCanonicalRosterKeyTest(RungBase):
    """ONE CANONICAL IDENTITY, ONE ROSTER KEY.

    The casefold-existence predicate that narrowed the admission refusal
    exposed a split: existence was decided case-insensitively while the row
    was LOADED AND WRITTEN by exact spelling, so a case-variant call wrote a
    SECOND key. Session history, home, runtime and presence split in half
    while recipient matching and the authority still saw one seat.
    """

    def _sessions(self, row):
        return [row.get("session")] + list(row.get("sessions") or [])

    def test_an_EXACT_spelling_update_keeps_one_key_and_its_history(self):
        from helm import seats_roster
        os.environ["HELM_SEAT_NAMES"] = self.names_file
        seats_roster.write_roster(A, session="s" * 12)
        seats_roster.write_roster(A, session="t" * 12)
        keys = [k for k in seats_roster.roster()
                if k.casefold() == A.casefold()]
        self.assertEqual(keys, [A])
        got = self._sessions(seats_roster.roster()[A])
        self.assertIn("s" * 12, got, "prior session history was lost")
        self.assertIn("t" * 12, got)

    def test_a_CASE_VARIANT_update_does_not_split_the_identity(self):
        """The paired pole, and the one that was broken: the SAME identity
        spelled differently must land on the SAME row, not beside it."""
        from helm import seats_roster
        os.environ["HELM_SEAT_NAMES"] = self.names_file
        seats_roster.write_roster(A, session="s" * 12)
        seats_roster.write_roster(A.upper(), session="t" * 12)
        keys = [k for k in seats_roster.roster()
                if k.casefold() == A.casefold()]
        self.assertEqual(keys, [A],
                         "a case variant created a SECOND row for one "
                         "canonical identity: %s" % keys)
        got = self._sessions(seats_roster.roster()[A])
        self.assertIn("s" * 12, got,
                      "the case-variant write started from an empty row and "
                      "discarded the identity's history")
        self.assertIn("t" * 12, got)

    def test_AMBIGUOUS_legacy_case_variants_REFUSE_rather_than_guess(self):
        """Two rows already casefolding together is unrepairable by guessing:
        either choice silently discards one seat's history."""
        from helm import seats_roster
        os.environ["HELM_SEAT_NAMES"] = self.names_file
        seats_roster.write_roster(A, session="s" * 12)
        r = seats_roster.roster_for_write()
        r[A.upper()] = dict(r[A])           # plant the legacy split directly
        pk_mod = seats_roster.pk
        pk_mod.write_json(seats_roster.roster_path(), r)
        with self.assertRaises(OSError) as caught:
            seats_roster.write_roster(A, session="t" * 12)
        self.assertIn("case-variant", str(caught.exception))
