#!/usr/bin/env python3
"""Files that must NEVER be tracked — the check every as-public pass missed.

WHAT HAPPENED, in this repo's private lineage. A personal, privacy-sensitive
owner skill under `agents/claudecode/skills/` was TRACKED, and had already been
pushed to the remote. Its files carry material about the owner personally,
including verbatim private quotes. That remote was private, so nothing was
publicly disclosed — but a private repo is still a REMOTE, and this repo's whole
as-public posture is that it stays ONE VISIBILITY FLIP away from public. A flip
would have published it instantly.

THIS DOCSTRING IS DELIBERATELY UNSPECIFIC, and that is itself the second
lesson. The first version of this file explained the hazard by DESCRIBING
the sensitive material — so the tracked, one-flip-from-public test written to
keep a private disclosure off the remote stated that disclosure in its own
prose. Removing a file's CONTENT while leaving a signpost that says what was in
it does not protect anything; the guard has to be quiet about the thing it
guards. A reason must be specific enough that the next person tidying up does
not delete the rule, and no more specific than that.

WHY EVERY SCRUB MISSED IT, which is the reusable lesson: the as-public passes
searched for leaky CONTENT — owner names, incident markers, session UUIDs,
internal paths, secret patterns. They never asked the different question "is
there a FILE here that must not be tracked at all, whatever its content?" A
content scan cannot answer that, because the file is not a leak of a pattern; the
file is the leak. Three independent review passes (a from-clone certification,
a re-certification that produced 7 named classes, and a third full pass) all
passed the tree while these sat in it. Adding one more content pattern would not have caught it;
only enumerating the never-track set does.

WHY A GITIGNORE ALONE IS NOT ENOUGH: `.gitignore` does nothing for a file that is
ALREADY tracked — git keeps honouring the index — which is exactly how these
survived. So the pin below asserts the INDEX, not the ignore file.

THE HISTORY IS NOT FIXED BY THIS. A blob that already reached a remote stays in
that remote's history, and removing it is a rewrite of a shared remote — an
owner decision, surfaced, not something a test can undo. This file only
guarantees such files stop entering NEW trees.

THE THIRD LESSON: A SUITE-RUN GUARD CANNOT CLOSE A TIMING GAP. A
real operator path entered history in a new fixture with this suite GREEN: the
file was completely untracked when pytest ran (correctly invisible to every
index read), then `git add` + `git commit` followed with no re-run. The first
proposed fix unioned `git ls-files` with `git diff --cached` here — measured
and DISPROVED (both read the same index; see the premise pin below). The real
fix is the composed pre-commit guard from `helm work install-guard --apply`,
which runs the SAME law (now shared via helm/nevertrack.py) against the staged
set inside `git commit` itself — the one moment that matters. This file keeps
the index PIN (already-tracked violations) and the law's tests; the hook is
the enforcement-timing leg. tests/test_never_track_hook.py proves the hook
end-to-end.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from helm import nevertrack  # noqa: E402
from helm.nevertrack import NEVER_TRACK, load_private_needles  # noqa: E402


def _tracked(pathspec):
    p = subprocess.run(["git", "ls-files", "-z", "--", pathspec],
                       capture_output=True, text=True, cwd=ROOT, timeout=60)
    if p.returncode != 0:
        raise RuntimeError("git ls-files failed: %s" % p.stderr.strip())
    return [f for f in p.stdout.split("\0") if f]


# THE NEEDLES ARE LOCAL USER DATA AND DO NOT BELONG IN THIS REPO.
#
# They used to be a tracked literal tuple, split pairwise so the file never
# held a whole one. That kept the guard working while making the repo carry the
# owner's account names, email domains and home path — in the very file whose
# job is keeping personal data off the remote. The split was a tell: a value you
# must obfuscate to commit is a value you should not be committing.
#
# It also does not generalise. A guard that hardcodes WHOSE data to look for
# protects exactly one person and silently protects nobody else, so a fresh
# consumer of this repo inherits a check that cannot fire for them.
#
# So the identifiers live OUTSIDE the tree, one per line, comments with '#':
#     $HELM_PRIVATE_NEEDLES, else <real home>/.helm/_global/private-needles.txt
# and the tracked file ships ZERO of them (loader now lives in
# helm/nevertrack.py so the pre-commit guard reads the SAME set). An estate
# with no file configured gets an empty needle set and the scan is a
# documented NO-OP rather than a silent pass — see
# test_needles_absent_is_reported_not_silently_passed.
PRIVATE_FIXTURE_NEEDLES, PRIVATE_NEEDLES_PATH = load_private_needles()


_WORD_BYTES = frozenset(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _standalone_hit(hay, needle):
    """True iff `needle` occurs in `hay` NOT buried inside a longer word.

    WHY THIS IS NOT A PLAIN SUBSTRING TEST, measured on a real estate's
    configuration: 13 needles, and THREE UNDER 8 CHARACTERS (min 6). A 6-byte string
    raw-substring-matched across the FULL BYTES of every tracked test file will
    eventually collide with something innocent, and when it does main goes red
    for a reason no author can see and no diff explains — the config that
    caused it lives OUTSIDE the tree by design.

    THE TWO SCANS DISAGREED AND ONLY ONE SAID WHY. The account guard already
    narrows to needles containing "." or "@" (2 of the 13) with a comment
    stating the reason: a rule broad enough to fire on innocuous text "would
    get switched off, and a guard people switch off protects nothing". The
    fixture-label scan applied NO narrowing whatever. Same corpus, same
    needles, two different breadths, one of them undocumented.

    THE TRADEOFF IS REAL AND POINTS BOTH WAYS, so it is stated rather than
    hidden: a boundary rule can MISS a leak where a private identifier is
    glued to other word characters (needle `abcdef` inside `abcdefgh`). For a
    PUBLICATION guard a false negative is worse than a false positive, which
    argues for staying strict. It is admitted here because `abcdefgh` is a
    DIFFERENT token — it is not the identifier, and treating every superstring
    as a leak is what makes a short needle unusable in the first place. A
    reviewer who thinks the asymmetry should win instead should say so: the
    alternative is to keep substring matching for long needles and apply this
    rule only below a length threshold, which is strictly safer and one magic
    number more complex.
    """
    i = hay.find(needle)
    while i != -1:
        before, after = hay[i - 1:i], hay[i + len(needle):i + len(needle) + 1]
        if (not (i and before[0] in _WORD_BYTES)
                and not (after and after[0] in _WORD_BYTES)):
            return True
        i = hay.find(needle, i + 1)
    return False


def _private_fixture_hits(root=ROOT, tracked=None):
    files = _tracked("tests") if tracked is None else tracked
    if tracked is None and (not files or "tests/test_never_track.py" not in files):
        raise RuntimeError("tracked test enumeration is empty or missing its guard")
    hits = []
    for rel in files:
        full = os.path.join(root, rel)
        if os.path.islink(full):
            payload = os.fsencode(os.readlink(full))
        else:
            with open(full, "rb") as f:
                payload = f.read()
        for needle in PRIVATE_FIXTURE_NEEDLES:
            nb = needle.encode()
            if _standalone_hit(os.fsencode(rel), nb) or _standalone_hit(payload, nb):
                hits.append((rel, needle))
    return hits


class NeverTrackTest(unittest.TestCase):

    def test_no_never_track_path_is_in_the_index(self):
        """The load-bearing assertion. It reads the INDEX rather than .gitignore,
        because an already-tracked file is tracked no matter what .gitignore
        says — which is precisely how this survived three reviews."""
        leaked = {}
        for path, why in NEVER_TRACK.items():
            found = _tracked(path)
            if found:
                leaked[path] = (found, why)
        self.assertEqual(
            leaked, {},
            "these paths must NEVER be tracked — untrack with `git rm --cached "
            "<file>` (the files STAY on disk and keep working locally): %s"
            % (leaked,))

    def test_untracking_did_not_EMPTY_a_path_that_is_present(self):
        """Untracked is NOT deleted — but that is only checkable where the files
        exist, and CONDITIONING it is the whole point.

        FIRST VERSION OF THIS TEST WAS WRONG, caught by a cross-family review
        on a fresh clone: it
        asserted the untracked paths exist on disk, UNCONDITIONALLY. A clone
        contains only TRACKED files, so an untracked path can never exist in one
        by construction — meaning the test failed on a correct artifact, and
        would have failed the very `git archive`/fresh-clone certification this
        commit exists to protect, plus the CI job on every runner.

        That is the exact class this repo keeps hitting from the other side: a
        check that passes on the developer's worktree and fails on the shipped
        artifact. Writing an artifact-hygiene guard that itself could not survive
        the artifact is the joke, so the reason is recorded here rather than
        quietly patched.

        The INDEX assertion above is the real guarantee and needs no such
        conditioning. This one now says: where a never-track path IS present, it
        must not have been hollowed out."""
        for path, _why in NEVER_TRACK.items():
            full = os.path.join(ROOT, path)
            if not os.path.isdir(full):
                continue          # absent BY DESIGN in a clone or an archive
            self.assertTrue(
                os.listdir(full),
                "%s exists but is EMPTY — untracking must never delete the "
                "contents; the fleet still uses these locally" % path)

    def test_a_needle_buried_in_a_longer_word_is_not_a_leak(self):
        """BOTH CONTROLS, because a scan over bytes cannot be eyeballed.

        The MUST-NOTs are the point of the change: three configured needles are
        under 8 characters, and raw substring matching over every tracked test
        file's full bytes turns any innocent collision into a red main that no
        diff explains. The MUST-HITs are what stops the cure from gutting the
        guard — delimiter-adjacent occurrences are still leaks.

        The last case is the one a naive implementation fails: a BURIED
        occurrence followed by a STANDALONE one. A single find() that rejects
        the first match and stops would miss the real leak sitting after it.
        """
        for hay, needle, want, why in (
                (b"the user abcdef here", b"abcdef", True, "standalone"),
                (b"abcdef", b"abcdef", True, "whole string"),
                (b"path/abcdef/file", b"abcdef", True, "slash-delimited"),
                (b"a-abcdef-b", b"abcdef", True, "hyphen-delimited is a leak"),
                (b"x user@ex.com y", b"user@ex.com", True, "email standalone"),
                (b"abcdefgh and abcdef", b"abcdef", True,
                 "buried FIRST, standalone LATER — the scan must not stop early"),
                (b"xxabcdefgh", b"abcdef", False, "buried in a longer word"),
                (b"zabcdef", b"abcdef", False, "word char before"),
                (b"abcdefz", b"abcdef", False, "word char after"),
                (b"myuser@ex.com", b"user@ex.com", False, "a DIFFERENT address"),
                (b"nope", b"abcdef", False, "absent")):
            self.assertEqual(_standalone_hit(hay, needle), want,
                             "%s: %r in %r" % (why, needle, hay))

    def test_the_scan_ITSELF_uses_the_boundary_rule_not_just_the_helper(self):
        """BINDS THE CALL SITE, because the helper test does not.

        Caught by mutation: reverting _private_fixture_hits to raw substring
        left the whole suite GREEN, because the case above exercises
        _standalone_hit DIRECTLY. It proved the helper works and said nothing
        about whether anything CALLS it — the same shape as a fixture that
        substitutes its own correct code for the code under test.

        This drives the real scan over a planted tracked file: a needle buried
        in a longer word must produce NO hit, and a standalone one in the same
        corpus must still produce one. Skips honestly when no needle is
        configured, like every other guard in this file.
        """
        if not PRIVATE_FIXTURE_NEEDLES:
            self.skipTest("no private needles configured — nothing to bury")
        needle = min(PRIVATE_FIXTURE_NEEDLES, key=len)
        tmp = tempfile.mkdtemp(prefix="helm-test-needle-scan-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        buried, standalone = "buried.txt", "standalone.txt"
        with open(os.path.join(tmp, buried), "w") as f:
            f.write("prefix%ssuffix\n" % needle)      # glued to word chars
        with open(os.path.join(tmp, standalone), "w") as f:
            f.write("a %s b\n" % needle)              # space-delimited
        hits = _private_fixture_hits(root=tmp, tracked=[buried, standalone])
        files = {rel for rel, _n in hits}
        self.assertNotIn(buried, files,
                         "a needle glued inside a longer word is not a leak, "
                         "and the SCAN must apply that rule, not just the helper")
        self.assertIn(standalone, files,
                      "the cure must not gut the guard — a standalone "
                      "occurrence is still a leak")

    def test_every_never_track_entry_states_a_reason(self):
        """A rule whose reason is lost is a rule someone deletes while tidying.
        Same discipline as the VCS allowlist reasons having to be non-trivial."""
        thin = [p for p, why in NEVER_TRACK.items() if len(why.strip()) < 40]
        self.assertEqual(thin, [], "each never-track entry needs a real reason, "
                                   "not a label: %r" % thin)

    def test_no_tracked_file_carries_a_real_owner_account(self):
        """Real owner accounts stay absent without restating them in this guard.

        Deliberately NARROW: real account domains only. A broad "no email
        anywhere" rule would fire on example.com and on the CI identity and get
        switched off, and a guard people switch off protects nothing.

        THE ACCOUNTS COME FROM LOCAL CONFIG, same as the fixture needles and for
        the same reason: they were split literals here, which still shipped the
        owner's real domains inside the guard meant to keep them off the remote.
        No configured needles => nothing to search for => this is a documented
        no-op, not a silent pass."""
        needles = tuple(n for n in PRIVATE_FIXTURE_NEEDLES if "." in n or "@" in n)
        if not needles:
            self.skipTest("no private needles configured (%s) — this guard is a "
                          "NO-OP on this estate, not a pass"
                          % PRIVATE_NEEDLES_PATH)
        args = []
        for n in needles:
            args += ["-e", n]
        p = subprocess.run(["git", "grep", "-l", "-I", "-F"] + args + ["--", "."],
                           capture_output=True, text=True, cwd=ROOT, timeout=120)
        # git grep exits 1 with no output when nothing matches — that is success
        hits = [f for f in p.stdout.splitlines() if f.strip()]
        self.assertEqual(hits, [], "tracked files carrying a real owner account "
                                   "— use example.com in illustrations: %r" % hits)

    def test_tests_carry_no_private_fixture_labels(self):
        """Fixtures preserve behavior with generic labels, never owner data.

        Needles are assembled so this guard scans itself too — no exclusion can
        become the next hiding place for a copied private fixture."""
        hits = _private_fixture_hits()
        self.assertEqual(hits, [], "private fixture labels in tracked tests/: %r"
                                   % hits)

    def test_private_fixture_guard_fails_loud_when_index_is_unreadable(self):
        failed = mock.Mock(returncode=128, stdout="", stderr="fatal: not a repo")
        with mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "git ls-files failed"):
                _tracked("tests")

    def test_private_fixture_guard_rejects_empty_or_wrong_enumeration(self):
        for files in ([], ["tests/test_other.py"]):
            with self.subTest(files=files), \
                    mock.patch.object(sys.modules[__name__], "_tracked",
                                      return_value=files):
                with self.assertRaisesRegex(RuntimeError, "missing its guard"):
                    _private_fixture_hits()

    def test_private_fixture_guard_scans_all_tracked_file_shapes(self):
        """Extensions, pathnames and symlink targets are all part of the tree.

        SYNTHETIC needles, deliberately. This exercises the SCAN, not any
        particular identifier — using real ones made a mechanism test depend on
        the owner's personal data, which is the same flaw the needle list itself
        had. A guard's tests must not re-import what the guard exists to keep
        out."""
        fake = ("zz-needle-alpha", "zz-needle-beta", "zz-needle-gamma")
        with tempfile.TemporaryDirectory(prefix="helm-test-private-labels-") as tmp, \
                mock.patch.object(sys.modules[__name__],
                                  "PRIVATE_FIXTURE_NEEDLES", fake):
            os.makedirs(os.path.join(tmp, "tests", "nested"))
            content_rel = os.path.join("tests", "nested", "fixture.json")
            with open(os.path.join(tmp, content_rel), "w") as f:
                f.write(fake[0])
            path_rel = os.path.join("tests", fake[-1] + ".txt")
            with open(os.path.join(tmp, path_rel), "w") as f:
                f.write("generic")
            link_rel = os.path.join("tests", "fixture-link")
            os.symlink(fake[1], os.path.join(tmp, link_rel))
            hits = _private_fixture_hits(tmp, [content_rel, path_rel, link_rel])
        self.assertEqual(set(hits), {(content_rel, fake[0]),
                                     (path_rel, fake[-1]),
                                     (link_rel, fake[1])})

    def test_the_needle_list_ships_empty_and_loads_from_outside_the_tree(self):
        """THE ARCHITECTURAL FIX. The identifiers are LOCAL USER DATA: an
        estate's own account names, email domains and home path. A generic repo
        must not carry them, and a guard that hardcodes whose data to look for
        protects one person while silently protecting nobody else."""
        # Assembled, not written literally: spelling it out would put the very
        # pattern into the file that the assertion forbids — this test failed
        # itself on the first run for exactly that reason, which is the same
        # self-reference trap the original split-needle tuple was working
        # around. A guard must not contain what it searches for. The loader
        # moved to helm/nevertrack.py, so BOTH sources are checked: a literal
        # reappearing in either file is the same regression.
        banned = "PRIVATE_FIXTURE_NEEDLES" + " = tuple(a" + " + b"
        for source in (__file__, nevertrack.__file__):
            with open(source, encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn(banned, src,
                             "needles must not be a tracked literal (%s)" % source)
        with tempfile.TemporaryDirectory(prefix="helm-needles-") as tmp:
            p = os.path.join(tmp, "n.txt")
            with open(p, "w") as f:
                f.write("# a comment\n\nzz-alpha\n  zz-beta  \n")
            with mock.patch.dict(os.environ, {"HELM_PRIVATE_NEEDLES": p}):
                needles, path = load_private_needles()
        self.assertEqual(needles, ("zz-alpha", "zz-beta"),
                         "comments and blank lines are skipped, values stripped")
        self.assertEqual(path, p)

    def test_needles_absent_is_reported_not_silently_passed(self):
        """A scan with nothing to look for PASSES, and that pass means 'I could
        not look' — the exact shape of a vacuous guard. An estate with no needle
        file configured must be able to see that it is a no-op."""
        with tempfile.TemporaryDirectory(prefix="helm-needles-none-") as tmp:
            with mock.patch.dict(os.environ,
                                 {"HELM_PRIVATE_NEEDLES": os.path.join(tmp, "absent")}):
                needles, path = load_private_needles()
        self.assertEqual(needles, (), "a missing file yields NO needles")
        self.assertTrue(path, "and still names where it looked, so the no-op is visible")

    def test_census_silent_on_zero_needles_the_callers_responsibility(self):
        """Zero needles => caller owns the note (the existing no-needles
        message). The census returns empty string — it must never duplicate
        or conflict."""
        from helm import nevertrack
        note, classes = nevertrack._needle_census(())
        self.assertEqual(note, "")
        self.assertEqual(classes, {})

    def test_thin_needles_missing_address_shape_triggers_census_note(self):
        """A needle set with paths but nothing containing @ — armed but blind
        to email/account-id shapes. The census must say what class is absent."""
        from helm import nevertrack
        note, classes = nevertrack._needle_census(
            ("/home/user/proj", "pipeline-token", "example-org-name"))
        self.assertIn("no address-shaped (@)", note)
        self.assertIn("3 private needles loaded", note)
        self.assertTrue(classes["path"])
        self.assertFalse(classes["address"])

    def test_thin_needles_missing_path_shape_triggers_census_note(self):
        """Needles with addresses but no paths — armed against email but blind
        to filesystem PII."""
        from helm import nevertrack
        note, classes = nevertrack._needle_census(
            ("user@example.invalid", "other@example.invalid"))
        self.assertIn("no path-shaped", note)
        self.assertIn("2 private needles loaded", note)
        self.assertTrue(classes["address"])
        self.assertFalse(classes["path"])

    def test_complete_needle_set_reports_coverage_quietly(self):
        """Both shapes present — the guard is not OBVIOUSLY thin (a census
        cannot guarantee completeness, only flag obvious gaps)."""
        from helm import nevertrack
        note, classes = nevertrack._needle_census(
            ("user@example.invalid", "/home/user/estate"))
        self.assertIn("not obviously thin", note)
        self.assertTrue(classes["address"])
        self.assertTrue(classes["path"])

    def test_added_bytes_RAISES_on_git_failure_and_never_returns_empty(self):
        """FAIL CLOSED. An empty answer from the added-side reader means "this
        commit adds nothing" — the one sentence a broken git must not be able
        to say, because it makes every needle look pre-existing and every
        commit pass."""
        with mock.patch.object(nevertrack, "_git",
                               return_value=(128, b"", b"fatal: not a repo")):
            with self.assertRaisesRegex(RuntimeError, "git diff --cached failed"):
                nevertrack._added_bytes("/nowhere", "some/file")

    def test_added_bytes_reports_binary_as_None_so_the_caller_over_blocks(self):
        """Binary has no line structure, so there is no added side to read.
        None is the signal to scan the WHOLE blob instead — over-blocking is
        the only safe direction when the diff is unreadable."""
        out = (b"diff --git a/x.bin b/x.bin\nindex e69de29..1f0c1a2 100644\n"
               b"Binary files a/x.bin and b/x.bin differ\n")
        with mock.patch.object(nevertrack, "_git", return_value=(0, out, b"")):
            self.assertIsNone(nevertrack._added_bytes("/r", "x.bin"))

    def test_added_bytes_keeps_an_added_line_that_itself_begins_with_plus(self):
        """The obvious shortcut — drop every line starting `+++` as a header —
        eats a real added line whose own text begins `++`, which is a
        needle-shaped hole in the branch that blocks. Hunk membership is
        tracked instead, so headers go by position, never by spelling."""
        out = (b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1,2 @@\n"
               b"++leading plus survives\n+ordinary\n")
        with mock.patch.object(nevertrack, "_git", return_value=(0, out, b"")):
            self.assertEqual(nevertrack._added_bytes("/r", "x"),
                             b"+leading plus survives\nordinary")

    def test_added_bytes_ignores_removed_lines_and_the_file_headers(self):
        """A needle being DELETED is a scrub, not a leak — and `--- a/path`
        must never be read as removed content."""
        out = (b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n"
               b"-zz-gone\n+zz-kept\n")
        with mock.patch.object(nevertrack, "_git", return_value=(0, out, b"")):
            self.assertEqual(nevertrack._added_bytes("/r", "x"), b"zz-kept")

    def test_ls_files_already_sees_the_staged_set_so_a_union_adds_nothing(self):
        """THE PREMISE PIN for the staged-file timing incident, so the disproved fix
        does not get re-proposed. The proposal: union `git ls-files` with
        `git diff --cached --name-only` in _tracked(), because a staged-but-
        uncommitted file was supposedly invisible to ls-files alone. Measured:
        FALSE. Plain `git ls-files` reads the INDEX — a file is listed the
        instant it is `git add`ed, before any commit, byte-identical to what
        diff --cached reports; and a file with NO `git add` at all is invisible
        to BOTH. So the union changes nothing, and the incident (untracked
        fixture -> green suite -> add+commit with no re-run) was never a
        detection gap. It is an enforcement-TIMING gap, closed by the
        pre-commit guard (`helm work install-guard --apply`), which runs the
        shared law in helm/nevertrack.py inside `git commit` itself."""
        with tempfile.TemporaryDirectory(prefix="helm-staged-premise-") as tmp:
            env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
                       GIT_CONFIG_SYSTEM="/dev/null")

            def git(*args):
                p = subprocess.run(("git",) + args, cwd=tmp, env=env,
                                   capture_output=True, text=True, timeout=30)
                self.assertEqual(p.returncode, 0, p.stderr)
                return p.stdout

            git("init", "-q", "-b", "main")
            with open(os.path.join(tmp, "newfile.txt"), "w") as f:
                f.write("staged, never committed\n")
            self.assertEqual(git("ls-files", "--", "newfile.txt"), "",
                             "an UNTRACKED file is invisible — this is the "
                             "incident's suite-was-green state, and correct")
            git("add", "newfile.txt")
            ls = git("ls-files", "--", "newfile.txt")
            cached = git("diff", "--cached", "--name-only", "--", "newfile.txt")
            self.assertEqual(ls, "newfile.txt\n",
                             "ls-files reads the index: staged => listed, "
                             "no commit required")
            self.assertEqual(ls, cached, "the proposed union is a no-op")

    def test_gitignore_also_covers_them_so_a_re_add_is_hard(self):
        """Belt AND braces: the index pin is the guarantee, the ignore entry is
        what stops a careless `git add -A` from re-adding them in the first
        place. Both, because the index pin only fires once a suite runs."""
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            ignored = f.read()
        for path in NEVER_TRACK:
            self.assertIn(path, ignored,
                          "%s should also be in .gitignore" % path)


if __name__ == "__main__":
    unittest.main()
