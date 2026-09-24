#!/usr/bin/env python3
"""The conflict-marker pre-commit rung, script-driven and END TO END.

THE GAP THIS PROVES CLOSED (2026-08-01, measured): a committed
tests/test_vcs.py carried a whole conflict block through commit + push +
review, caught only by a later SyntaxError — and only because the markers sat
outside a string. The same block inside a DOCSTRING parses and greens every
suite (the first test here pins that with ast.parse), so the add->commit seam
is the only instrument that can see the shape. helm/conflict_marker.py is
that instrument; this file drives it the way the estate runs it — as a plain
script against scratch fixture repos — and then through the real composed
hook that `helm work install-guard --apply` installs.

FIXTURE DISCIPLINE: every marker is CONSTRUCTED (``"<" * 7``), never written
at column 0 of this source — once the rung is installed it scans this very
file on commit, and a guard's tests must not trip the guard they prove.

Hermetic: git global/system config nulled, HELM_HOME sandboxed, synthetic
needles planted so the composed hook's never-track leg has a real (quiet)
configuration.
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

from helm import wiring, work  # noqa: E402
from helm.work import _guard  # noqa: E402

MODULE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "helm", "conflict_marker.py")

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_LOG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
            "HELM_PRIVATE_NEEDLES", "HELM_NEVER_TRACK_SKIP",
            "HELM_CONFLICT_MARKER_SKIP", "HELM_WORK_CLAIM",
            "HELM_WORK_INTEGRATOR", "HELM_LANDLOCK")

OURS = "<" * 7 + " HEAD"
BASE7 = "|" * 7 + " base"
SEP = "=" * 7
THEIRS = ">" * 7 + " lane/x"

# The measured incident's shape: a full conflict block INSIDE a docstring —
# valid Python, importable, invisible to every syntax-level check.
PYFIX = "\n".join(['"""A docstring the parser accepts.',
                   OURS,            # line 2
                   "ours line",
                   SEP,             # line 4 — bracketed, so the arm fires
                   "theirs line",
                   THEIRS,          # line 6
                   '"""',
                   "X = 1", ""])

# The must-pass pair the seven-equals decision is pinned against. "Section"
# and "Heading" are SEVEN characters on purpose: an exactly-seven restriction
# would NOT save these underlines, which is why the arm is neighbor-gated
# instead (helm/conflict_marker.py docstring owns the reasoning).
RSTFIX = "\n".join([SEP, "Section", SEP, "", "Body text.", ""])
MDFIX = "\n".join(["Heading", SEP, "", "Setext prose.", ""])


class RungBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-conflict-rung-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_LANDLOCK"] = "0"
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        needles = os.path.join(self.tmp, "needles.txt")
        with open(needles, "w") as f:
            f.write("zz-synthetic-conflict-rung\n")
        os.environ["HELM_PRIVATE_NEEDLES"] = needles
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t"),
                    # THE FIXTURE DECLARES ITS LAW. It simulates
                    # helm-the-shared-checkout; an undeclared PROJECT repo
                    # now defaults to the leak legs (task/2441).
                    ("git", "config", "--local",
                     "helm.guard.profile", "rail")):
            self.assertEqual(self.sh(self.root, *cmd).returncode, 0)
        with open(os.path.join(self.root, "README"), "w") as f:
            f.write("seed\n")
        self.sh(self.root, "git", "add", "-A")
        r = self.sh(self.root, "git", "commit", "-q", "-m", "seed")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, cwd, *args, env=None):
        merged = dict(os.environ, **(env or {}))
        return subprocess.run(list(args), cwd=cwd, capture_output=True,
                              text=True, timeout=60, env=merged)

    def head(self):
        return self.sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()

    def stage(self, rel, content, cwd=None):
        self.stage_bytes(rel, content.encode(), cwd)

    def stage_bytes(self, rel, blob, cwd=None):
        cwd = cwd or self.root
        full = os.path.join(cwd, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(blob)
        # :(literal) — `git add` itself glob-expands pathspecs, and these
        # fixtures include files NAMED '*.txt' and '[a]b.txt' on purpose
        r = self.sh(cwd, "git", "add", "--", ":(literal)" + rel)
        self.assertEqual(r.returncode, 0, r.stderr)

    def rung(self, cwd=None):
        """The rung EXACTLY as the hook runs it: a plain script, cwd in the
        fixture repo, no helm package anywhere near the interpreter."""
        return self.sh(cwd or self.root, sys.executable, MODULE, "--staged")

    def commit(self, msg="c", env=None):
        return self.sh(self.root, "git", "commit", "-q", "-m", msg, env=env)


class ScriptScanTest(RungBase):
    def test_docstring_conflict_block_is_refused_naming_file_and_lines(self):
        """THE MUST-CATCH — and first, the premise: this fixture PARSES, so
        every syntax-level check on the estate greens it."""
        ast.parse(PYFIX)
        self.stage("pkg/mod.py", PYFIX)
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("[helm conflict-marker] REFUSED", r.stderr)
        self.assertIn("pkg/mod.py:2 — merge-conflict ours-side opener", r.stderr)
        self.assertIn("pkg/mod.py:4 — merge-conflict separator", r.stderr)
        self.assertIn("pkg/mod.py:6 — merge-conflict theirs-side closer", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertIn("HELM_CONFLICT_MARKER_SKIP=1", r.stderr)

    def test_rst_and_markdown_setext_underlines_pass(self):
        """THE MUST-PASS: RST over/underlines and a Markdown setext H1, all
        exactly seven equals under seven-char titles. The second phase seeds
        a MUST-HIT so a green first phase cannot be a dead probe."""
        self.stage("docs/section.rst", RSTFIX)
        self.stage("docs/head.md", MDFIX)
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage("pkg/mod.py", PYFIX)
        r = self.rung()
        self.assertEqual(r.returncode, 1, "the probe must be alive")
        self.assertIn("pkg/mod.py", r.stderr)
        self.assertNotIn("section.rst", r.stderr,
                         "a heading underline must not ride the refusal")
        self.assertNotIn("head.md", r.stderr)

    def test_diff3_base_marker_is_refused(self):
        self.stage("notes.txt", "\n".join(
            [OURS, "ours", BASE7, "base", SEP, "theirs", THEIRS, ""]))
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("notes.txt:3 — merge-conflict diff3 base marker", r.stderr)

    def test_bare_seven_glyph_lines_with_no_label_still_refuse(self):
        """A hand-mangled leftover drops the label; the arm takes EOL too."""
        self.stage("a.txt", "\n".join(["<" * 7, "x", ">" * 7, ""]))
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("a.txt:1", r.stderr)
        self.assertIn("a.txt:3", r.stderr)

    def test_eight_glyphs_and_indented_lookalikes_pass(self):  # noqa: VACUOUS_ASSERTION — phase two stages c.txt and asserts the refusal NAMES it — a live positive control in the same scan
        """THE DEFAULT-7 MUST-PASS CONTROL for the attribute law. RESCOPED
        (attribute-drift fix): the old premise 'N is read from the INDEX' was
        false — git WRITES markers under working-tree attributes — so N now
        resolves from BOTH sources and this fixture pins the case where both
        are silent: no .gitattributes anywhere, both sources default to 7, an
        8-glyph run is not a marker git would write there. The drift tests
        own every attributed case. Phase two seeds the MUST-HIT again — an
        empty result needs a live probe behind it."""
        self.stage("b.txt", "\n".join(
            ["<" * 8 + " not a marker", " " + OURS, "\t" + THEIRS,
             "=" * 8, " " + SEP, ""]))
        r = self.rung()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage("c.txt", "\n".join([OURS, SEP, THEIRS, ""]))
        r = self.rung()
        self.assertEqual(r.returncode, 1, "the probe must be alive")
        self.assertIn("c.txt", r.stderr)
        self.assertNotIn("b.txt", r.stderr)

    def test_lonely_separator_without_bracket_passes(self):
        """The documented trade-off: an unbracketed seven-equals line is
        byte-identical to a heading underline and must not refuse. The
        bracketed twin in the same scan proves the arm itself is live."""
        self.stage("lone.txt", "prose\n%s\nmore prose\n" % SEP)
        self.stage("pair.txt", "\n".join([OURS, SEP, THEIRS, ""]))
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("pair.txt:2 — merge-conflict separator", r.stderr)
        self.assertNotIn("lone.txt", r.stderr,
                         "an unbracketed separator is a heading underline")

    def test_crlf_conflict_block_is_still_caught(self):
        self.stage_bytes("win.txt",
                         ("\r\n".join([OURS, "a", SEP, "b", THEIRS]) + "\r\n")
                         .encode())
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("win.txt:1", r.stderr)
        self.assertIn("win.txt:3 — merge-conflict separator", r.stderr)

    def test_nul_carrying_blob_is_skipped_but_the_text_twin_is_not(self):
        payload = ("\n".join([OURS, SEP, THEIRS]) + "\n").encode()
        self.stage_bytes("blob.bin", b"\x00" + payload)
        r = self.rung()
        self.assertEqual(r.returncode, 0,
                         "markers are a line construct; NUL blobs are not "
                         "line-text: %s" % r.stderr)
        self.stage_bytes("blob.txt", payload)   # the MUST-HIT twin
        r = self.rung()
        self.assertEqual(r.returncode, 1)
        self.assertIn("blob.txt", r.stderr)
        self.assertNotIn("blob.bin", r.stderr)

    def test_preexisting_markers_note_and_never_block_added_markers_do(self):
        """nevertrack's measured lesson, applied: blocking a commit cannot
        remove what HEAD already carries (`guard-refuses-what-it-cannot-fix`).
        No hook is installed in this scratch repo, so planting is one plain
        commit — the same way the incident file entered history."""
        planted = "\n".join([OURS, "old", SEP, "older", THEIRS, "tail", ""])
        self.stage("legacy.txt", planted)
        r = self.commit("plant")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage("legacy.txt", planted + "clean appended line\n")
        r = self.rung()
        self.assertEqual(r.returncode, 0,
                         "a commit cannot be blamed for HEAD's markers: %s"
                         % r.stderr)
        self.assertIn("legacy.txt:1", r.stderr,
                      "the pre-existing block must still be NAMED, every run")
        self.assertIn("ALREADY IN HEAD", r.stderr)
        self.assertIn("cleanup commit", r.stderr)
        self.assertNotIn("REFUSED", r.stderr)
        # ...and a NEW block added to that same dirty file still refuses
        self.stage("legacy.txt",
                   planted + "\n".join([OURS, "new", SEP, "newer", THEIRS, ""]))
        r = self.rung()
        self.assertEqual(r.returncode, 1, "the ADDED block is this commit's")
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertIn("legacy.txt:7", r.stderr)

    def test_conflict_marker_size_attribute_sets_the_marker_length(self):
        """Real git honors the per-path conflict-marker-size ATTRIBUTE: with
        size=8 git WRITES 8-glyph markers, and a rung pinned to seven would
        bless every one of them. The attribute resolves from BOTH sources —
        index and working tree agree at 8 here; the drift tests below own
        each disagreement direction."""
        self.stage(".gitattributes", "*.c8 conflict-marker-size=8\n")
        self.stage("w.c8", "\n".join(
            ["<" * 8 + " HEAD", "a", "=" * 8, "b", ">" * 8 + " lane", ""]))
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("w.c8:1 — merge-conflict ours-side opener", r.stderr)
        self.assertIn("w.c8:3 — merge-conflict separator", r.stderr)
        self.assertIn("w.c8:5 — merge-conflict theirs-side closer", r.stderr)
        self.assertIn("'" + "<" * 8 + "'", r.stderr,
                      "the refusal must show the length it matched")

    def test_attr_drift_untracked_gitattributes_size_must_refuse(self):
        """ATTRIBUTE-DRIFT arm A: git writes markers at merge time under
        WORKING-TREE attributes, so an untracked .gitattributes size=8
        shapes REAL 8-glyph markers while `check-attr --cached` still
        answers 'unspecified'. The rung bound to the index alone blessed
        exactly those markers (measured rc=0); either resolved length must
        refuse."""
        with open(os.path.join(self.root, ".gitattributes"), "w") as f:
            f.write("*.c8 conflict-marker-size=8\n")   # worktree ONLY, never added
        self.stage("f.c8", "\n".join(
            ["<" * 8 + " HEAD", "a", "=" * 8, "b", ">" * 8 + " lane", ""]))
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "working-tree attrs shape the markers git actually "
                         "writes: %s" % r.stderr)
        self.assertIn("f.c8:1 — merge-conflict ours-side opener", r.stderr)
        self.assertIn("f.c8:3 — merge-conflict separator", r.stderr)
        self.assertIn("f.c8:5 — merge-conflict theirs-side closer", r.stderr)
        self.assertIn("'" + "<" * 8 + "'", r.stderr,
                      "the refusal must show the drift-resolved length")

    def test_attr_drift_staged_size_does_not_bless_the_real_seven_block(self):
        """ATTRIBUTE-DRIFT arm B, the reverse: the merge ran under default 7
        — the staged block is REAL 7-glyph markers — and only later did a
        size=8 .gitattributes reach the index while the worktree copy moved
        on. The rung bound to the index alone hunted 8-glyph runs and passed
        the real block (measured rc=0); either resolved length must refuse."""
        self.stage(".gitattributes", "*.t7 conflict-marker-size=8\n")
        with open(os.path.join(self.root, ".gitattributes"), "w") as f:
            f.write("")            # worktree copy drifts: attr unspecified
        self.stage("f.t7", "\n".join([OURS, "a", SEP, "b", THEIRS, ""]))
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "the real 7-glyph block must refuse under index "
                         "drift: %s" % r.stderr)
        self.assertIn("f.t7:1 — merge-conflict ours-side opener", r.stderr)
        self.assertIn("f.t7:3 — merge-conflict separator", r.stderr)
        self.assertIn("f.t7:5 — merge-conflict theirs-side closer", r.stderr)
        self.assertIn("'" + "<" * 7 + "'", r.stderr,
                      "the refusal must show the drift-resolved length")

    def test_lf_in_a_staged_pathname_cannot_forge_a_diagnostic_line(self):  # noqa: VACUOUS_ASSERTION — rc==1 plus the ESCAPED name asserted PRESENT and every stderr line prefix-checked are the live positive controls; the assertNotIn is their negative twin on the same observable
        """A pathname is attacker-shaped data: one embedded LF used to split
        a refusal row into two stderr lines, the second an unprefixed forged
        diagnostic. The display edge escapes control characters, so each row
        stays ONE line carrying the escaped name — never two lines."""
        bad = "inject\nforged.txt"
        self.stage_bytes(bad, ("\n".join([OURS, SEP, THEIRS]) + "\n").encode())
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        lines = [ln for ln in r.stderr.splitlines() if ln]
        self.assertTrue(all(ln.startswith("[helm conflict-marker]")
                            for ln in lines),
                        "an unprefixed stderr line is a forged diagnostic: "
                        "%r" % lines)
        self.assertIn("inject\\nforged.txt:1", r.stderr,
                      "the row must carry the ESCAPED name")
        self.assertNotIn("\nforged.txt:1", r.stderr,
                         "a raw LF before the tail is the two-line forgery")  # noqa: VACUOUS_ASSERTION — rc==0 is not the claim; the note '*.txt:1 ... ALREADY IN HEAD' is asserted present in the same run
        """A file NAMED '*.txt', carrying an INHERITED block, staged next to
        a neighbor whose ADDED range covers those line numbers. Pathspec
        expansion hands this file the neighbor's hunks — the parser has no
        per-file attribution — and the inherited note is misblamed ADDED."""
        planted = "\n".join([OURS, "a", SEP, "b", THEIRS, ""])
        self.stage("*.txt", planted)
        r = self.commit("plant")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage("*.txt", planted + "appended clean line\n")
        filler = "\n".join("filler %d" % i for i in range(1, 30)) + "\n"
        self.stage("bulk.txt", filler)
        r = self.rung()
        self.assertEqual(r.returncode, 0,
                         "an inherited block must stay a NOTE even beside a "
                         "big added range in a glob-matching neighbor: %s"
                         % r.stderr)
        self.assertIn("*.txt:1", r.stderr)
        self.assertIn("ALREADY IN HEAD", r.stderr)

    def test_a_bracket_named_file_is_scanned_literally_not_expanded(self):  # noqa: VACUOUS_ASSERTION — phase one asserts the note NAMES [a]b.txt:1; phase two is the rc==1 live-probe control
        """'[a]b.txt' glob-matches 'ab.txt' — measured: git's matcher takes
        the literal name AND the expansion, so the hazard is POLLUTION: the
        neighbor's added range rides in and misblames this file's inherited
        block as ADDED. Literal pathspecs keep the note a note."""
        planted = "\n".join([OURS, "a", SEP, "b", THEIRS, ""])
        self.stage("[a]b.txt", planted)
        self.stage("ab.txt", "decoy content\n")
        r = self.commit("plant")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage("[a]b.txt", planted + "appended clean line\n")
        filler = "\n".join("filler %d" % i for i in range(1, 30)) + "\n"
        self.stage("ab.txt", filler)
        r = self.rung()
        self.assertEqual(r.returncode, 0,
                         "the neighbor's added range must not be blamed on "
                         "the bracket-named file's inherited block: %s"
                         % r.stderr)
        self.assertIn("[a]b.txt:1", r.stderr)
        self.assertIn("ALREADY IN HEAD", r.stderr)
        # the live-probe control: a block this commit ADDS there still refuses
        self.stage("[a]b.txt",
                   planted + "\n".join([OURS, SEP, THEIRS, ""]))
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "the ADDED block in the bracket-named file must "
                         "refuse: %s" % r.stderr)
        self.assertIn("[a]b.txt:6", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)

    def test_a_textconv_filter_cannot_hide_added_markers(self):  # noqa: VACUOUS_ASSERTION — asserts the refusal NAMES f.sec:1 ADDED — a positive effect, not an absence
        """textconv applies to `git diff --cached` BY DEFAULT: a filter that
        strips marker lines erases exactly the added ranges that must
        refuse, so the added block would read inherited. The rung diffs with
        --no-textconv and reads blobs by OID."""
        strip = "sed -e '/^%s/d' -e '/^%s$/d' -e '/^%s/d'" % (
            "<" * 7, "=" * 7, ">" * 7)
        r = self.sh(self.root, "git", "config", "diff.hider.textconv", strip)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage(".gitattributes", "*.sec diff=hider\n")
        self.stage("f.sec", "plain line\n")
        r = self.commit("plant")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage("f.sec",
                   "\n".join([OURS, SEP, THEIRS]) + "\nplain line\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "a textconv view must not hide the added block: %s"
                         % r.stderr)
        self.assertIn("f.sec:1", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)

    def test_pure_rename_of_inherited_markers_stays_note_only(self):  # noqa: VACUOUS_ASSERTION — phase one asserts the note NAMES new-name.txt:1; phase two is the rc==1 live-probe control
        """The diff-aware law survives a rename: R100 adds no lines, so the
        markers HEAD already carries stay a NOTE — refusing the one operation
        that cannot even edit the content would only teach the skip flag."""
        filler = "\n".join("line %d" % i for i in range(1, 21))
        planted = "\n".join([OURS, "a", SEP, "b", THEIRS, filler, ""])
        self.stage("old-name.txt", planted)
        r = self.commit("plant")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.sh(self.root, "git", "mv", "old-name.txt", "new-name.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.rung()
        self.assertEqual(r.returncode, 0,
                         "a pure rename adds nothing and must not refuse: %s"
                         % r.stderr)
        self.assertIn("new-name.txt:1", r.stderr,
                      "the inherited block is still NAMED at its new path")
        self.assertIn("ALREADY IN HEAD", r.stderr)
        # the live-probe control: rename PLUS an added block still refuses
        with open(os.path.join(self.root, "new-name.txt"), "a") as f:
            f.write("\n".join([OURS, "new", SEP, "newer", THEIRS, ""]))
        r = self.sh(self.root, "git", "add", "--", "new-name.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.rung()
        self.assertEqual(r.returncode, 1, "the ADDED block is this commit's")
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)

    def test_unreadable_staged_content_refuses_as_UNKNOWN_never_skips(self):
        """The tri-state law: a staged blob that cannot be READ is not a
        clean one. Silent-skip would make the commit that most needs the
        scan the one that passes; the rung fails CLOSED (exit 2)."""
        self.stage("ghost.txt", "content the object store will lose\n")
        out = self.sh(self.root, "git", "ls-files", "--stage",
                      "--", "ghost.txt").stdout
        oid = out.split()[1]
        obj = os.path.join(self.root, ".git", "objects", oid[:2], oid[2:])
        os.unlink(obj)
        r = self.rung()
        self.assertEqual(r.returncode, 2,
                         "unreadable staged content is UNKNOWN, not clean: "
                         "rc=%d %s" % (r.returncode, r.stderr))
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("UNKNOWN", r.stderr)

    def test_invalid_utf8_path_is_scanned_not_crashed(self):  # noqa: VACUOUS_ASSERTION — asserts the refusal NAMES the replaced filename at :1; no-Traceback rides a positive refusal
        """Bytes-safe path handling: a filename that is not UTF-8 is data.
        The marker inside it must still refuse — with the name shown via
        replacement — and a traceback is a crash, not a refusal."""
        bad = b"bad-\xff-name.txt"
        with open(os.path.join(os.fsencode(self.root), bad), "wb") as f:
            f.write(("\n".join([OURS, SEP, THEIRS]) + "\n").encode())
        r = self.sh(self.root, "git", "add", "--", bad)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("bad-�-name.txt:1", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_the_index_is_scanned_never_the_worktree(self):  # noqa: VACUOUS_ASSERTION — direction two asserts the refusal NAMES ix-only.txt:1 — the live control for direction one's absence
        """Staged-vs-worktree ownership, BOTH directions: the commit takes
        the index, so a marker only in the worktree must not refuse, and a
        marker only in the index must — a scan quietly flipped to worktree
        content would pass every fixture that stages what it writes."""
        block = "\n".join([OURS, SEP, THEIRS, ""])
        self.stage("wt-only.txt", "clean staged line\n")
        with open(os.path.join(self.root, "wt-only.txt"), "w") as f:
            f.write(block)          # dirty worktree, NOT added
        r = self.rung()
        self.assertEqual(r.returncode, 0,
                         "an unstaged worktree marker is not this commit's: "
                         "%s" % r.stderr)
        self.assertNotIn("wt-only.txt", r.stderr)
        # the reverse: index dirty, worktree scrubbed clean
        self.stage("ix-only.txt", block)
        with open(os.path.join(self.root, "ix-only.txt"), "w") as f:
            f.write("scrubbed worktree\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "the staged marker is what commits: %s" % r.stderr)
        self.assertIn("ix-only.txt:1", r.stderr)
        self.assertNotIn("wt-only.txt", r.stderr)

    def test_usage_error_and_non_repo_fail_closed(self):
        r = self.sh(self.root, sys.executable, MODULE)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)
        outside = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(outside)
        r = self.rung(cwd=outside)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not inside a git work tree", r.stderr)


class MergeInheritsTheOtherSidesMarkersTest(RungBase):
    """ADDED MEANS ABSENT FROM EVERY PARENT.

    This rung attributed lines with `git diff --cached`, whose base is HEAD —
    during a merge, the FIRST parent only. So a blob a merge took unchanged
    from its other side read as lines this commit staged, and `status` itself
    is first-parent talk: 'A' for a path the other side has carried for months.
    MEASURED on the shipped rung before the cure, on the fixture below: exit 1,
    `m.txt:2 ... ADDED BY THIS COMMIT`, for a file the lane never touched.

    Same law and same measurement as helm/nevertrack.py's, which is why
    `commit_parents` lives in one module and this rung asks it.
    """

    def trunk_carrying(self, rel, content):
        """A real two-parent index: the trunk commits `content`, a lane
        branched before it commits elsewhere, and the merge is left staged."""
        trunk = self.sh(self.root, "git", "rev-parse", "--abbrev-ref",
                        "HEAD").stdout.strip()
        self.assertEqual(self.sh(self.root, "git", "checkout", "-qb", "lane"
                                 ).returncode, 0)
        self.stage("lane/own.txt", "the lane's own work\n")
        self.assertEqual(self.commit("lane work").returncode, 0)
        self.assertEqual(self.sh(self.root, "git", "checkout", "-q", trunk
                                 ).returncode, 0)
        self.stage(rel, content)
        self.assertEqual(self.commit("trunk work").returncode, 0)
        self.assertEqual(self.sh(self.root, "git", "checkout", "-q", "lane"
                                 ).returncode, 0)
        r = self.sh(self.root, "git", "merge", "--no-commit", "--no-ff", trunk)
        gitdir = self.sh(self.root, "git", "rev-parse", "--absolute-git-dir"
                         ).stdout.strip()
        with open(os.path.join(gitdir, "MERGE_HEAD")) as f:
            self.assertEqual(len(f.read().split()), 1,
                             "no merge is in progress, so this arm is about an "
                             "ordinary commit: %s" % (r.stdout + r.stderr))

    def test_a_merge_is_not_blamed_for_a_marker_its_other_parent_committed(self):
        self.trunk_carrying("pkg/mod.py", PYFIX)
        r = self.rung()
        self.assertEqual(r.returncode, 0,
                         "the merge was refused for marker lines its other "
                         "parent already carried: %s" % r.stderr)
        self.assertIn("pkg/mod.py:2", r.stderr,
                      "the inherited block is not named at all, so the pass is "
                      "a mute rung rather than a scoped one: %s" % r.stderr)
        self.assertIn("ALREADY IN A PARENT OF THIS MERGE", r.stderr)
        self.assertNotIn("ADDED BY THIS COMMIT", r.stderr)

    def test_a_merge_that_RESOLVES_with_a_marker_is_still_refused(self):
        """THE MUST-HIT on an otherwise-valid merge: identical two parents, and
        the only difference is that the resolution writes a marker block. A
        rung that had simply stopped looking at merges would pass the arm above
        and fail this one."""
        self.trunk_carrying("pkg/other.py", "X = 1\n")
        self.stage("pkg/mod.py", PYFIX)
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "a marker THIS merge staged was admitted: %s" % r.stderr)
        self.assertIn("pkg/mod.py:2", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertNotIn("pkg/other.py", r.stderr,
                         "the refusal names a file it has no finding about")

    def test_the_single_parent_base_did_not_move(self):
        """THE CONTROL: one parent, a staged marker block, still refused with
        the unchanged HEAD wording."""
        self.stage("pkg/mod.py", PYFIX)
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertNotIn("PARENT OF THIS MERGE", r.stderr,
                         "the merge wording reached a single-parent commit")


class TheStagedRungScansUnderANonUTF8GitdirTest(RungBase):
    """THE SUPPORTED `--staged` RUNG MUST NOT DIE ON A PATH IT NEVER PRINTS.

    This rung asks nevertrack.commit_parents who the parents are, and that
    helper asks git for the MERGE_HEAD PATHNAME. For a linked worktree that
    answer is absolute and lives under the COMMON git directory, so one
    ancestor of the common gitdir holding a raw 0xff byte makes it non-UTF8 —
    while the lane root, the staged filename and the staged bytes are all
    ASCII. Decoded as text it raised UnicodeDecodeError, which is a ValueError
    and NOT one of the RuntimeError/TimeoutExpired/OSError this rung's `main`
    catches, so a clean ASCII staged file produced a traceback and a refused
    commit where the pre-merge scanner had scanned the same file byte for byte.
    tests/test_conflict_marker.py's existing bytes arms vary a staged FILENAME;
    this one varies only the Git-directory pathname the rung never displays.
    """

    def lane_under(self, ancestor_suffix, tag):
        """A repo whose COMMON git directory sits under `ancestor_suffix`, with
        an ASCII-named linked worktree. The two calls differ in that suffix and
        in nothing else."""
        anc = os.fsdecode(os.fsencode(self.tmp) + b"/anc-" + ancestor_suffix)
        os.makedirs(anc)
        main = os.path.join(anc, "main")
        os.makedirs(main)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t"),
                    ("git", "config", "--local", "helm.guard.profile",
                     "rail")):
            self.assertEqual(self.sh(main, *cmd).returncode, 0)
        self.stage("README", "seed\n", cwd=main)
        r = self.sh(main, "git", "commit", "-q", "-m", "seed")
        self.assertEqual(r.returncode, 0, r.stderr)
        # THE LANE IS NAMED FROM THE ASCII `tag`, NEVER from the ancestor
        # bytes: os.fsdecode of a raw 0xff yields a surrogate, which would put
        # non-ASCII in the WORKTREE path and destroy the one-variable design.
        lane = os.path.join(self.tmp, "lane-" + tag)
        r = self.sh(main, "git", "worktree", "add", "-q", "-b", "lane", lane,
                    env={"HELM_WORK_INTEGRATOR": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(lane.isascii(),
                        "fixture: the worktree stays ASCII, so the gitdir "
                        "pathname is the only non-ASCII thing in play")
        return lane

    def git_path_bytes(self, lane):
        r = subprocess.run(("git", "rev-parse", "--git-path", "MERGE_HEAD"),
                           cwd=lane, capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_a_clean_ASCII_staged_file_scans_under_either_ancestor(self):  # noqa: VACUOUS_ASSERTION — the absence-shaped readings are the ASCII twin's pathname holding no 0xff and the two stderrs holding no traceback; the unconditional positives on the same producers come first and are the 0xff BYTE in the pathname this rung's parent query resolves and the pre-cure text-mode raise, and both rc-0 readings below are unconditional equalities on the shipped rung
        """The rung runs EXACTLY as the hook runs it — a plain script with cwd
        in the lane — and the ASCII-ancestor twin is the control."""
        ascii_lane = self.lane_under(b"ascii", "ascii")
        raw_lane = self.lane_under(b"\xff", "raw")
        raw = self.git_path_bytes(raw_lane)
        self.assertIn(b"\xff", raw,
                      "git did not put the non-UTF8 byte in the pathname this "
                      "rung's parent query resolves: %r" % raw)
        self.assertNotIn(b"\xff", self.git_path_bytes(ascii_lane))
        # MUST-HIT CONTROL, THROUGH THE SHIPPED PRODUCER IN THE PRE-CURE
        # CURRENCY: the same query decoded as text raises here and not in the
        # ASCII twin, so the exit 0 below would have been a traceback and an
        # exit 2 before the cure. Blast radius: one read-only `git rev-parse`
        # in this arm's own scratch lane; it changes no state.
        self.assertRaises(UnicodeDecodeError, subprocess.run,
                          ("git", "rev-parse", "--git-path", "MERGE_HEAD"),
                          cwd=raw_lane, capture_output=True, text=True,
                          timeout=60)
        # BOTH LANES INLINE, no loop: each reading is unconditional.
        self.stage("pkg/clean.py", "X = 1\n", cwd=ascii_lane)
        a = self.rung(cwd=ascii_lane)
        self.assertEqual(a.returncode, 0,
                         "the ASCII control lane did not scan: %s" % a.stderr)
        self.assertNotIn("Traceback", a.stderr)
        self.stage("pkg/clean.py", "X = 1\n", cwd=raw_lane)
        r = self.rung(cwd=raw_lane)
        self.assertEqual(r.returncode, 0,
                         "a clean ASCII staged file was not scanned under a "
                         "non-UTF8 common gitdir: %s" % r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_marker_staged_in_that_lane_is_STILL_refused(self):
        """THE MUST-HIT on an otherwise-identical lane: the only difference
        from the arm above is the staged content. A rung that had gone quiet
        under a non-UTF8 gitdir would pass that arm and fail this one."""
        lane = self.lane_under(b"\xff", "raw")
        self.assertIn(b"\xff", self.git_path_bytes(lane))
        self.stage("pkg/mod.py", PYFIX, cwd=lane)
        r = self.rung(cwd=lane)
        self.assertEqual(r.returncode, 1,
                         "a marker block this commit staged was admitted: %s"
                         % r.stderr)
        self.assertIn("pkg/mod.py:2", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)


class HookWiringTest(RungBase):
    def cli(self, *args):
        """The real mechanism: the `helm work` dispatcher, not install_guard
        called directly — an install path nobody can reach is not wiring."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def install(self):
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        return out

    def test_install_snapshots_the_conflict_scanner_beside_the_hook(self):
        out = self.install()
        self.assertIn("HELM_CONFLICT_MARKER_SKIP=1", out,
                      "the install summary must teach the rung's own skip")
        assets = _guard._scanner_assets(self.root)
        installed = next(p for p in assets if p.endswith("conflict_marker.py"))
        with open(installed, "rb") as got, open(MODULE, "rb") as want:
            self.assertEqual(got.read(), want.read(),
                             "installed snapshot must be the source, byte-for-byte")
        hook = work.hook_path(self.root, "pre-commit")
        with open(hook) as f:
            body = f.read()
        self.assertIn(installed, body)
        self.assertEqual(body.count('python3 "$conflict" --staged'), 1,
                         "duplicate hook invocations repeat every refusal")
        self.assertNotIn(MODULE, body,
                         "a source lane path is editable and disposable")

    def test_commit_staging_a_docstring_block_is_refused_end_to_end(self):
        self.install()
        before = self.head()
        self.stage("tests/test_planted.py", PYFIX)
        r = self.commit()
        self.assertNotEqual(r.returncode, 0, "the commit MUST be refused")
        self.assertIn("[helm conflict-marker] REFUSED", r.stderr)
        self.assertIn("tests/test_planted.py:2", r.stderr)
        self.assertEqual(self.head(), before, "history did not move")
        status = self.sh(self.root, "git", "status", "--porcelain").stdout
        self.assertIn("A  tests/test_planted.py", status,
                      "refusal leaves the stage intact for the fix-up")

    def test_rst_heading_lands_through_the_installed_hook(self):
        self.install()
        before = self.head()
        self.stage("docs/section.rst", RSTFIX)
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self.head(), before, "history DID move")

    def test_skip_env_is_a_one_commit_owner_override(self):
        self.install()
        self.stage("docs/markers.md", "\n".join(
            ["how git writes markers:", OURS, SEP, THEIRS, ""]))
        r = self.commit(env={"HELM_CONFLICT_MARKER_SKIP": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        # and the very next commit is guarded again
        self.stage("docs/markers2.md", "\n".join([OURS, SEP, THEIRS, ""]))
        r = self.commit()
        self.assertNotEqual(r.returncode, 0, "no standing exemption")
        self.assertIn("[helm conflict-marker] REFUSED", r.stderr)

    def test_never_track_skip_does_not_disarm_the_conflict_rung(self):
        """Separate laws, separate skips: the documented never-track bypass
        must not silently switch this rung off — that is exactly how the
        incident file would ride a bypass into history again."""
        self.install()
        before = self.head()
        self.stage("pkg/mod.py", PYFIX)
        r = self.commit(env={"HELM_NEVER_TRACK_SKIP": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[helm conflict-marker] REFUSED", r.stderr)
        self.assertEqual(self.head(), before)

    def test_never_track_skip_and_absence_do_not_disarm_the_hardcode_rung(self):
        """Each rung's skip AND each rung's absence isolate to itself. The v2
        hook's never-track skip line `exit 0`ed the whole hook from ABOVE the
        hardcode rung, and a missing never-track snapshot exited the same way
        — one law's bypass silently disarmed an unrelated law."""
        self.install()
        self.stage("pkg/wired.py", 'CFG_DIR = "/home/zz-fixture-user/cfg/"\n')
        r = self.commit(env={"HELM_NEVER_TRACK_SKIP": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm hardcode]", r.stderr,
                      "the hardcode rung must still fire under never-track's "
                      "one-commit skip")
        # absence leg: a deleted never-track snapshot warns, and the
        # hardcode rung STILL fires on the same commit
        scanner = next(p for p in _guard._scanner_assets(self.root)
                       if p.endswith("/nevertrack.py"))
        os.unlink(scanner)
        self.stage("pkg/wired2.py", 'LOG_DIR = "/home/zz-fixture-user/logs/"\n')
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm hardcode]", r.stderr,
                      "a missing never-track snapshot must not disarm the "
                      "hardcode rung")
        self.assertIn("[helm never-track] WARNING: scanner missing", r.stderr)

    def test_wiring_census_proves_the_installed_rung_and_names_an_absent_one(self):
        """The ALLOWED exemption is a claim; the actuator census is its proof.
        Both directions, or the obligation is prose: the hook this repo's
        installer writes must satisfy conflict-marker-pre-commit, and a hook
        estate WITHOUT the invocation line must report it MISSING."""
        self.install()
        hooks = os.path.dirname(work.hook_path(self.root, "pre-commit"))
        empty_units = os.path.join(self.tmp, "no-units")
        os.makedirs(empty_units)
        got = wiring.actuator_census(hook_dir=hooks, unit_dir=empty_units,
                                     crontab="", settings_paths=[])
        self.assertIn("conflict-marker-pre-commit", got["wired"])
        self.assertNotIn("conflict-marker-pre-commit", got["missing"])
        bare = os.path.join(self.tmp, "bare-hooks")
        os.makedirs(bare)
        with open(os.path.join(bare, "pre-commit"), "w") as f:
            f.write('#!/bin/sh\nscanner=/opt/helm/helm/nevertrack.py\n'
                    'exec python3 "$scanner" --staged\n')
        os.chmod(os.path.join(bare, "pre-commit"), 0o755)
        got = wiring.actuator_census(hook_dir=bare, unit_dir=empty_units,
                                     crontab="", settings_paths=[])
        self.assertIn("conflict-marker-pre-commit", got["missing"])

    def test_missing_snapshot_fails_open_but_loud(self):
        """A deleted installed snapshot must not brick every commit — but the
        skip has to announce itself, or the estate believes it is guarded."""
        self.install()
        installed = next(p for p in _guard._scanner_assets(self.root)
                         if p.endswith("conflict_marker.py"))
        os.unlink(installed)
        self.stage("docs/clean.md", "clean\n")
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm conflict-marker] WARNING: scanner missing", r.stderr)
        self.assertIn("install-guard --apply", r.stderr)


class AnAddedLineIsOneTheCommitActuallyWroteTest(RungBase):
    """Every guard that judges only what a commit ADDS asks git which lines
    those are, and for a long time none of them said WHICH DIFF.

    THE MEASUREMENT. The fixture's second blob is a byte-exact SUBSEQUENCE of
    its first: two whole classes are cut out and not one surviving line is
    touched, so the truthful answer is that this commit adds NOTHING. Under
    git's default myers algorithm the answer came back as 162 added lines --
    myers minimises the edit COUNT, and on a deletion that large it is cheaper
    to delete a little more here and RE-ADD the identical text there, so
    unchanged lines arrive with a `+`. That is not a cosmetic difference: a
    seat-name rung refused a correct commit over a literal on a line the commit
    had not written, three files and eight thousand deleted lines away.

    THREE READERS, ONE QUESTION, ASKED TOGETHER ON PURPOSE. conflict_marker,
    silent_cap and vacuous_assertion each spawn their own `git diff --cached
    -U0`, and a cure applied to one leaves the other two lying. Enumerating
    them here means a future reader added without the pin fails this arm rather
    than being discovered by a refusal nobody can explain.

    AND THE AMBIENT HALF: `diff.algorithm` is a user config. Without the pin a
    guard's verdict depends on the operator's ~/.gitconfig, which is the class
    of hostile state helm/gitfacts.py already neutralises at its own door. The
    hostile-config leg proves the pin beats the config rather than merely
    agreeing with the default.
    """

    COMMON = ('        self.assertTrue(ok)', '        self.assertIsNone(err)',
              '', '        ok, msg = run(x)',
              '        chat.post("noise", who="bob")',
              '        seats.join(session="s", seat="a", cwd="/tmp/p")')

    #: (name, arity) per block. Sizes are load-bearing: the artifact needs
    #: deletions big enough, and separated by enough surviving text, that
    #: re-adding is the cheaper script. Smaller shapes do not reproduce it.
    BLOCKS = (("Head", 30), ("Gone1", 200), ("Mid", 80), ("Gone2", 250),
              ("Tail", 90))

    def block(self, name, n):
        """One class whose bodies REPEAT shared lines without being equal —
        the real shape, and the one that misleads myers. A fixture of
        identical blocks does not reproduce the defect, so it would prove
        nothing."""
        out = ["class %s(Base):" % name, '    """%s."""' % name, ""]
        for i in range(n):
            out.append("    def test_%s_%d(self):" % (name.lower(), i))
            for k in range(3 + (i * 7 + len(name)) % 7):
                out.append(self.COMMON[(i * 5 + k * 3 + len(name))
                                       % len(self.COMMON)])
            out.append("        self.assertEqual(%s_%d, %d)"
                       % (name.lower(), i, i))
            out.append("")
        return out

    def whole(self):
        out = []
        for name, n in self.BLOCKS:
            if out:
                out.append("")
            out += self.block(name, n)
        return "\n".join(out) + "\n"

    def without(self, names):
        lines = self.whole().split("\n")
        for name in names:
            i = lines.index("class %s(Base):" % name)
            j = i + 1
            while not lines[j].startswith("class "):
                j += 1
            lines = lines[:i - 1] + lines[j:]
        return "\n".join(lines)

    def readers(self):
        """(name, call, lines a PURE DELETION may attribute) for each reader.

        THE THIRD FIELD IS NOT A FUDGE, it is the one place the three readers
        legitimately differ and it is written down so a drift is a failure
        rather than a shrug. conflict_marker and silent_cap drop a hunk whose
        new-side count is zero, so a pure deletion attributes nothing.
        vacuous_assertion keeps that hunk's new-side ANCHOR on purpose and its
        own docstring says why -- removing the sole positive assertion is
        exactly the change it must see. One line per deleted block is a
        bounded, deliberate over-report; a RUN of surviving body lines, which
        is what the unpinned algorithm produced, is not.
        """
        from helm import conflict_marker as cm
        from helm import silent_cap, vacuous_assertion
        return (
            ("conflict_marker",
             lambda root: cm._added_ranges(root, "M", None, b"f.py"), 0),
            ("silent_cap",
             lambda root: silent_cap._added_ranges(root, "f.py"), 0),
            ("vacuous_assertion",
             lambda root: vacuous_assertion._added_ranges(root, "f.py"),
             len(self.CUT)),
        )

    #: The blocks cut from the seed. One deletion hunk each.
    CUT = ("Gone1", "Gone2")

    def seed_then_cut(self):
        """Commit the whole file, stage the version with two classes gone."""
        self.stage("f.py", self.whole())
        self.assertEqual(self.commit("seed").returncode, 0)
        self.stage("f.py", self.without(self.CUT))

    def test_the_fixture_deletes_only(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the length assertion; the subsequence loop is the premise it guards
        """THE PREMISE, asserted rather than assumed: if the cut blob were not
        a byte-exact subsequence of the seed, a nonzero added-range count would
        be correct and the rest of this class would prove nothing."""
        whole, cut = self.whole().split("\n"), self.without(self.CUT).split("\n")
        # UNCONDITIONAL: a `without` that returned its input unchanged would
        # satisfy the subsequence loop below while cutting nothing at all.
        self.assertLess(len(cut), len(whole) - 400)
        old = iter(whole)
        for line in cut:
            self.assertTrue(any(line == o for o in old),
                            "cut blob is not a subsequence: %r" % line)

    #: Every module in the tree that reads staged ADDED line numbers. The
    #: count is asserted, not looped over blindly: a readers() that returned
    #: nothing would make every arm below pass while measuring no reader.
    READERS = 3

    def check_bounded(self, leg):
        """No reader may attribute a RUN of surviving lines, and each may
        attribute only the anchors its own contract allows."""
        self.assertEqual(self.READERS, len(self.readers()))
        for name, read, allowed in self.readers():
            with self.subTest(reader=name):
                got = list(read(self.root) or [])
                wide = [r for r in got if r[1] > r[0]]
                self.assertEqual([], wide,
                                 "%s claimed a RUN of unchanged lines (%s): %r"
                                 % (name, leg, got))
                self.assertEqual(allowed, len(got),
                                 "%s attributed %d line(s), %d allowed (%s)"
                                 % (name, len(got), allowed, leg))

    def test_a_pure_deletion_adds_nothing_in_every_reader(self):
        self.seed_then_cut()
        self.check_bounded("default config")

    def test_the_pin_beats_a_hostile_ambient_algorithm(self):
        """myers set in config is exactly what shipped as the default, so this
        leg is the one that fails if the pin is dropped and the machine's
        config happens to agree with the code."""
        self.seed_then_cut()
        cfg = os.path.join(self.tmp, "hostile.gitconfig")
        with open(cfg, "w") as f:
            f.write("[diff]\n\talgorithm = myers\n")
        os.environ["GIT_CONFIG_GLOBAL"] = cfg
        self.sh(self.root, "git", "config", "--local",
                "diff.algorithm", "myers")
        self.check_bounded("hostile ambient algorithm")

    def test_a_real_addition_is_still_reported(self):  # noqa: VACUOUS_ASSERTION — this IS the class's positive control, and its reader count is asserted unconditionally first
        """THE POSITIVE CONTROL. A reader that answered "nothing added" to
        everything would pass every arm above and see no defect ever."""
        self.seed_then_cut()
        cut = self.without(self.CUT).split("\n")
        cut.insert(1, '    """zz-synthetic-added-line."""')
        self.stage("f.py", "\n".join(cut))
        self.assertEqual(self.READERS, len(self.readers()))
        for name, read, _allowed in self.readers():
            with self.subTest(reader=name):
                got = list(read(self.root) or [])
                self.assertTrue(any(lo <= 2 <= hi for lo, hi in got),
                                "%s missed the one added line: %r"
                                % (name, got))


if __name__ == "__main__":
    unittest.main()
