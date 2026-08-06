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
                    ("git", "config", "user.name", "t")):
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


if __name__ == "__main__":
    unittest.main()
