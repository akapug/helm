"""The seats-split budget asked at COMMIT instead of from a red whole suite.

EVERY ARM IS ABOUT A DISCRIMINATION. A rung that refused everything and one
that refused nothing would both look like work, so each refusing row below
sits beside a passing one differing only in the property under test.

THE DESIGN THE ARMS PIN, and it is the reason this is not just the suite arm
moved earlier: it REFUSES ONLY WHAT THE COMMIT MAKES WORSE. A module already
over budget is a standing debt somebody owns, and walling every commit in the
repository until they pay it would make this rung the thing seats route
around -- which is how a guard stops being consulted. So growth past the
budget refuses, a commit that SHRINKS an over-budget module passes, and a
standing debt in a file this commit does not touch is reported and never
refuses.
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import splitbudget                                  # noqa: E402

RUNG = os.path.abspath(splitbudget.__file__)


class BudgetRungTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="splitbudget-")
        self.addCleanup(_rmtree, self.root)
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.invalid")
        self._git("config", "user.name", "t")
        os.makedirs(os.path.join(self.root, "helm"))

    def _git(self, *args):
        return subprocess.run(("git",) + args, cwd=self.root,
                              capture_output=True, text=True)

    def _write(self, name, lines):
        with open(os.path.join(self.root, "helm", name), "w") as fh:
            fh.write("x = 1\n" * lines)
        self._git("add", "helm/" + name)

    def _run(self):
        p = subprocess.run([sys.executable, RUNG, "--staged",
                            "--repo", self.root],
                           capture_output=True, text=True)
        return p.returncode, p.stderr

    def test_growth_past_the_budget_refuses_and_names_both_numbers(self):
        """THE CASE THAT COST A FAB ROUND TRIP (task/2256): a one-line change
        wrapped in thirty lines of rationale, and the red arrived from a whole
        suite naming a budget the author had not heard of."""
        self._write("seats_x.py", 500)
        self._git("commit", "-qm", "base")
        self._write("seats_x.py", splitbudget.FINISH + 3)
        rc, said = self._run()
        self.assertEqual(rc, 1, "a commit taking a module past the budget was "
                                "not refused: %r" % said)
        self.assertIn("seats_x.py", said, "the refusal does not name the file")
        self.assertIn(str(splitbudget.FINISH + 3), said,
                      "the refusal does not say what the module BECOMES")
        self.assertIn(str(splitbudget.FINISH), said,
                      "the refusal does not say what the budget IS")

    def test_a_commit_that_SHRINKS_an_over_budget_module_passes(self):
        """PROGRESS MID-WAY IS NOT A VIOLATION. A module coming down from over
        budget is the behaviour this rung wants; refusing it would wall the
        only commits that fix the thing being guarded."""
        self._write("seats_x.py", splitbudget.FINISH + 10)
        self._git("commit", "-qm", "over")
        self._write("seats_x.py", splitbudget.FINISH + 2)
        rc, said = self._run()
        self.assertEqual(rc, 0, "a commit REDUCING an over-budget module was "
                                "refused: %r" % said)
        self.assertIn("does not grow it", said,
                      "the pass is silent about a module that is still over "
                      "budget, so the debt goes unreported: %r" % said)

    def test_a_standing_debt_in_an_untouched_module_never_refuses(self):
        """THE ROW THAT KEEPS THIS RUNG CONSULTED. Somebody else's over-budget
        module must not wall a lane about something else — that is exactly the
        experience that makes a guard something seats route around."""
        self._write("seats_big.py", splitbudget.FINISH + 50)
        self._git("commit", "-qm", "debt")
        # THE TOUCHED MODULE IS PLANTED IN THE WARNING BAND ON PURPOSE, so
        # this ONE reading carries both halves: it must SPEAK about the module
        # this commit touched and STAY SILENT about the standing debt. A
        # control drawn from a second reading could never cover this one —
        # rebinding the observable starts a new question.
        self._write("seats_small.py", splitbudget.FINISH - 1)
        rc, said = self._run()
        self.assertIn("seats_small.py", said,
                      "this reading says nothing at all, so the silence about "
                      "seats_big.py below is a mute rung rather than a scoped "
                      "one: %r" % said)
        self.assertEqual(rc, 0, "an unrelated commit was refused because a "
                                "module it does not touch is over budget: %r"
                                % said)
        self.assertNotIn("seats_big.py", said,
                         "the rung reports a module this commit never touched")
        # AND THE DEBT IS STILL REFUSABLE, shown in its own reading: the
        # moment THIS commit grows that module, the same rung refuses it. This
        # is a separate claim from the one above and is written as one.
        self._write("seats_big.py", splitbudget.FINISH + 60)
        grew_rc, grew = self._run()
        self.assertEqual(grew_rc, 1,
                         "the same module, GROWN by this commit, is not "
                         "refused — the scoping above is indistinguishable "
                         "from a rung that never refuses: %r" % grew)
        self.assertIn("seats_big.py", grew)

    def test_the_facade_has_its_own_ceiling(self):
        self._write("seats.py", splitbudget.CEILING + 1)
        rc, said = self._run()
        self.assertEqual(rc, 1, "the facade past its ratchet was not refused: "
                                "%r" % said)
        self.assertIn("seats.py", said)

    def test_a_file_outside_the_budget_is_not_measured_at_any_size(self):
        """THE MUST-MISS, and it has to be huge: a rung that counted every
        staged file would pass every arm above and refuse the repository."""
        with open(os.path.join(self.root, "unrelated.py"), "w") as fh:
            fh.write("x = 1\n" * (splitbudget.FINISH * 5))
        self._git("add", "unrelated.py")
        # BOTH FILES IN ONE READING, which is the only way the silence means
        # anything: a budgeted module in the warning band beside a
        # non-budgeted file five times the budget in size. The rung must name
        # the first and never the second.
        self._write("seats_w.py", splitbudget.FINISH - 1)
        rc, said = self._run()
        self.assertEqual(rc, 0, "a non-budgeted file was measured: %r" % said)
        self.assertIn("seats_w.py", said,
                      "this reading is empty, so its silence about a 5000-line "
                      "unrelated.py is a mute rung rather than a scoped one")
        self.assertNotIn("unrelated.py", said,
                         "the rung speaks about a file it does not govern")

    def test_the_warning_band_reports_headroom_before_it_is_spent(self):
        """task/2264's own ask: three modules sat AT the wall and nothing said
        so until one of them went over."""
        self._write("seats_x.py", splitbudget.FINISH - 1)
        rc, said = self._run()
        self.assertEqual(rc, 0, "a module inside the budget was refused")
        self.assertIn("from the %d budget" % splitbudget.FINISH, said,
                      "a module one line from the budget says nothing, which "
                      "is the state task/2264 filed: %r" % said)
        # THE OPPOSITE ROW: comfortably under, and it must stay quiet or the
        # band is not a band.
        self._write("seats_y.py", splitbudget.FINISH - splitbudget.WARN_BAND * 4)
        _rc, quiet = self._run()
        self.assertIn("seats_x.py", quiet,
                      "the in-band module stopped being reported in this same "
                      "reading, so the absence below is about a rung that has "
                      "gone silent rather than about the band: %r" % quiet)
        self.assertNotIn("seats_y.py", quiet,
                         "a module far under budget is announced, so the "
                         "warning band is not discriminating")

    def test_an_unreadable_index_says_UNMEASURED_and_lets_the_commit_through(self):
        """A RUNG THAT CANNOT MEASURE MUST NOT WALL THE FLEET on its own bad
        day — the whole-suite arm has not moved and still enforces this."""
        p = subprocess.run(
            [sys.executable, RUNG, "--staged", "--repo",
             os.path.join(self.root, "no-such-dir")],
            capture_output=True, text=True)
        self.assertEqual(p.returncode, 0,
                         "the rung refused a commit because IT could not read "
                         "the index, walling the fleet on its own bad day: %r"
                         % p.stderr)
        self.assertIn("UNMEASURED", p.stderr,
                      "the rung passed SILENTLY when it could not measure, so "
                      "a broken rung is indistinguishable from a clean tree: "
                      "%r" % p.stderr)
        # THE OPPOSITE ROW: a readable repo must NOT claim UNMEASURED, or the
        # word means nothing and every real pass reads as a failed one.
        self._write("seats_x.py", splitbudget.FINISH - 1)
        clean_rc, clean = self._run()
        self.assertIn("from the %d budget" % splitbudget.FINISH, clean,
                      "the readable reading says nothing at all, so the "
                      "absence below is about a rung producing no output "
                      "rather than about the word UNMEASURED: %r" % clean)
        self.assertEqual(clean_rc, 0)
        self.assertNotIn("UNMEASURED", clean,
                         "a readable repo is reported UNMEASURED, so the word "
                         "cannot distinguish a blind rung from a clean tree")


    def _merge_trunk_growth(self, lines):
        """A real two-parent index: trunk grows the module, the lane does not
        touch it, and the merge is left staged-but-uncommitted — exactly the
        state a pre-commit rung is asked about."""
        self._write("seats_x.py", 400)
        self._git("commit", "-qm", "base")
        # THE TRUNK BRANCH IS READ, NEVER ASSUMED: this fixture's `git init`
        # names no branch, so whether it is main or master is the host's
        # git config and a hardcoded name fails on somebody else's box.
        trunk = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertTrue(trunk, "the fixture has no branch to merge from")
        self.assertEqual(self._git("checkout", "-qb", "lane").returncode, 0)
        with open(os.path.join(self.root, "elsewhere.txt"), "w") as fh:
            fh.write("the lane's own work\n")
        self._git("add", "elsewhere.txt")
        self._git("commit", "-qm", "lane work")
        self.assertEqual(self._git("checkout", "-q", trunk).returncode, 0)
        self._write("seats_x.py", lines)
        self._git("commit", "-qm", "trunk grows it")
        self._git("checkout", "-q", "lane")
        merged = self._git("merge", "--no-commit", "--no-ff", trunk)
        with open(os.path.join(self.root, ".git", "MERGE_HEAD")) as fh:
            heads = fh.read().split()
        self.assertEqual(len(heads), 1,
                         "the fixture did not leave a merge in progress: %r"
                         % (merged.stdout + merged.stderr))

    def test_a_merge_is_not_blamed_for_growth_its_other_side_committed(self):
        """GREW IS A CLAIM ABOUT WHAT THIS COMMIT DID.

        The rung compared the staged count against HEAD, which during a merge
        is the FIRST parent only — so merging a trunk that had grown a budgeted
        module read as this lane growing it, and the merge was refused for
        lines the lane never wrote. MEASURED on the shipped rung before the
        cure, on this fixture: exit 1, `helm/seats_x.py  400 -> 1400`. Its
        sibling rungs had the same base, which is why the cure is one law
        across the guard family (helm/nevertrack.py owns `commit_parents`).
        """
        self._merge_trunk_growth(splitbudget.FINISH + 400)
        rc, said = self._run()
        self.assertEqual(rc, 0, "the merge was refused for lines the other "
                                "parent already carried: %r" % said)
        self.assertIn("seats_x.py", said,
                      "this reading is empty, so the pass above is a mute rung "
                      "rather than a scoped one: %r" % said)
        self.assertIn("does not grow it", said,
                      "the standing debt is not reported, so the rung went "
                      "quiet instead of becoming accurate: %r" % said)

    def test_a_merge_IS_refused_for_growth_no_parent_had(self):
        """THE MUST-HIT on the same fixture: the resolution itself takes the
        module past the budget, and neither parent had it there. Without this
        row the arm above is indistinguishable from a rung that stopped
        measuring merges."""
        self._merge_trunk_growth(splitbudget.FINISH - 100)
        self._write("seats_x.py", splitbudget.FINISH + 500)
        rc, said = self._run()
        self.assertEqual(rc, 1, "growth NO parent carried was admitted because "
                                "a merge was in progress: %r" % said)
        self.assertIn("seats_x.py", said)
        self.assertIn(str(splitbudget.FINISH + 500), said)

    def test_the_single_parent_base_did_not_move(self):
        """THE CONTROL. The ordinary commit is what the cure had to leave
        byte-identical: one parent, growth past the budget, still refused."""
        self._write("seats_x.py", 400)
        self._git("commit", "-qm", "base")
        self._write("seats_x.py", splitbudget.FINISH + 400)
        rc, said = self._run()
        self.assertEqual(rc, 1, "a plain single-parent commit past the budget "
                                "stopped being refused: %r" % said)
        self.assertIn("400 -> %d" % (splitbudget.FINISH + 400), said,
                      "the refusal no longer names what the module WAS: %r"
                      % said)


class TheBudgetRungMEASURESUnderANonUTF8GitdirTest(unittest.TestCase):
    """UNMEASURED IS A LAST RESORT, NOT A PLACE TO PUT A PATHNAME.

    This rung asks nevertrack.commit_parents for the parent set, and that
    helper asks git for the MERGE_HEAD PATHNAME. For a linked worktree the
    answer is absolute and lives under the COMMON git directory, so one
    ancestor of the common gitdir holding a raw 0xff byte makes it non-UTF8 —
    with the lane root, the staged path and the staged bytes all ASCII.
    Decoded as text that raised UnicodeDecodeError, `main`'s broad except
    printed UNMEASURED and returned 0, and the whole inherited-debt vs
    new-growth decision this lane added was silently dropped for every commit
    in such a checkout. The cure is in the helper: the query is bytes, so the
    rung reaches its decision here.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="splitbudget-nonutf8-")
        self.addCleanup(_rmtree, self.tmp)

    def _git(self, cwd, *args):
        return subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                              text=True, timeout=60)

    def _write(self, cwd, name, lines):
        os.makedirs(os.path.join(cwd, "helm"), exist_ok=True)
        with open(os.path.join(cwd, "helm", name), "w") as fh:
            fh.write("x = 1\n" * lines)
        self.assertEqual(self._git(cwd, "add", "helm/" + name).returncode, 0)

    def _run(self, lane):
        p = subprocess.run([sys.executable, RUNG, "--staged", "--repo", lane],
                           capture_output=True, text=True, timeout=120)
        return p.returncode, p.stderr

    def lane_under(self, ancestor_suffix, tag):
        """A repo whose COMMON git directory sits under `ancestor_suffix`, with
        an ASCII-named linked worktree. Returns (main, lane)."""
        anc = os.fsdecode(os.fsencode(self.tmp) + b"/anc-" + ancestor_suffix)
        os.makedirs(anc)
        main = os.path.join(anc, "main")
        os.makedirs(main)
        for cmd in (("init", "-q", "-b", "main"),
                    ("config", "user.email", "t@example.invalid"),
                    ("config", "user.name", "t")):
            self.assertEqual(self._git(main, *cmd).returncode, 0)
        # THE LANE IS NAMED FROM THE ASCII `tag`, NEVER from the ancestor
        # bytes: os.fsdecode of a raw 0xff yields a surrogate, which would put
        # non-ASCII in the WORKTREE path and destroy the one-variable design.
        lane = os.path.join(self.tmp, "lane-" + tag)
        return main, lane

    def merge_trunk_growth(self, ancestor_suffix, tag, lines):
        """The trunk grows a budgeted module past the budget; a lane branched
        before it merges the trunk and touches nothing budgeted. The merge is
        left staged — the state the rung is asked about."""
        main, lane = self.lane_under(ancestor_suffix, tag)
        self._write(main, "seats_x.py", 400)
        self.assertEqual(self._git(main, "commit", "-qm", "base").returncode, 0)
        r = self._git(main, "worktree", "add", "-q", "-b", "lane", lane)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(lane.isascii(), "fixture: the lane stays ASCII")
        with open(os.path.join(lane, "elsewhere.txt"), "w") as fh:
            fh.write("the lane's own work\n")
        self.assertEqual(self._git(lane, "add", "elsewhere.txt").returncode, 0)
        self.assertEqual(self._git(lane, "commit", "-qm", "lane").returncode, 0)
        self._write(main, "seats_x.py", lines)
        self.assertEqual(self._git(main, "commit", "-qm", "trunk").returncode, 0)
        merged = self._git(lane, "merge", "--no-commit", "--no-ff", "main")
        gitpath = subprocess.run(("git", "rev-parse", "--git-path",
                                  "MERGE_HEAD"), cwd=lane,
                                 capture_output=True, timeout=60).stdout
        with open(gitpath.rstrip(b"\n"), "rb") as fh:
            self.assertEqual(len(fh.read().split()), 1,
                             "the fixture left no merge in progress: %r"
                             % (merged.stdout + merged.stderr))
        return lane, gitpath

    def test_the_inherited_debt_distinction_SURVIVES_a_nonUTF8_gitdir(self):  # noqa: VACUOUS_ASSERTION — the absence-shaped readings are the ASCII twin's pathname holding no 0xff and neither stderr saying UNMEASURED; the unconditional positives on the same producers are asserted first (the 0xff byte IS in the raw lane's pathname, the pre-cure text-mode query DOES raise) and both rungs' rc-0 plus their "does not grow it" line are unconditional readings of the shipped rung
        ascii_lane, ascii_path = self.merge_trunk_growth(
            b"ascii", "ascii", splitbudget.FINISH + 400)
        raw_lane, raw_path = self.merge_trunk_growth(
            b"\xff", "raw", splitbudget.FINISH + 400)
        self.assertIn(b"\xff", raw_path,
                      "git did not put the non-UTF8 byte in the pathname the "
                      "parent query resolves: %r" % raw_path)
        self.assertNotIn(b"\xff", ascii_path)
        # MUST-HIT CONTROL IN THE PRE-CURE CURRENCY: the same query decoded as
        # text raises on this lane and not on its ASCII twin, so before the
        # cure `scan` raised, `main` caught it and printed UNMEASURED, and the
        # reading below was exit 0 with no budget decision in it. Blast radius:
        # one read-only `git rev-parse` in this arm's own scratch lane.
        self.assertRaises(UnicodeDecodeError, subprocess.run,
                          ("git", "rev-parse", "--git-path", "MERGE_HEAD"),
                          cwd=raw_lane, capture_output=True, text=True,
                          timeout=60)
        # BOTH LANES INLINE, no loop: each reading is unconditional.
        rc, said = self._run(ascii_lane)
        self.assertEqual(rc, 0, "the ASCII control lane was refused: %r" % said)
        self.assertIn("does not grow it", said)
        self.assertNotIn("UNMEASURED", said)
        rc, said = self._run(raw_lane)
        self.assertEqual(rc, 0, "the merge was refused for lines the other "
                                "parent already carried: %r" % said)
        self.assertIn("does not grow it", said,
                      "the inherited debt is not reported, so this rung went "
                      "quiet rather than becoming accurate: %r" % said)
        self.assertNotIn("UNMEASURED", said,
                         "the rung gave up instead of deciding: %r" % said)

    def test_growth_NO_parent_had_is_still_refused_in_that_lane(self):
        """THE MUST-HIT: identical fixture, and the only difference is that the
        resolution itself takes the module past the budget. A rung that had
        simply stopped measuring under this gitdir would pass the arm above."""
        lane, path = self.merge_trunk_growth(b"\xff", "raw",
                                             splitbudget.FINISH - 100)
        self.assertIn(b"\xff", path)
        self._write(lane, "seats_x.py", splitbudget.FINISH + 500)
        rc, said = self._run(lane)
        self.assertEqual(rc, 1, "growth no parent carried was admitted: %r"
                                % said)
        self.assertIn("seats_x.py", said)
        self.assertIn(str(splitbudget.FINISH + 500), said)


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)
