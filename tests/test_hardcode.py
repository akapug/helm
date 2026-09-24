"""Tests for helm/hardcode.py — the staged-diff hardcode warner.

MUTATION BINDING (owner's requirement, 2026-07-31): every test here must turn
RED when its fix is reverted, and this docstring says so in the commit. The
mutations named per test were run by hand against the lane; the two-leg shape
(each shape has a FIRE test and a NO-FIRE control) is what makes a revert
visible — a deleted shape kills its FIRE test, and a widened shape kills its
control.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from helm import hardcode as h


CV_LINE = b'let root = dirs::home_dir().map(|h| h.join(".claude").join("projects"));'


class ShapeTest(unittest.TestCase):
    def test_the_cv_construction_shape_FIRES(self):
        """THE MUST-HIT CONTROL as a test: the canonical example from the
        owner's brief. A rung that cannot see this shape must never report a
        clean tree. Mutation: delete the construction arm -> this dies."""
        hits = h._hits(CV_LINE)
        self.assertIn("constructed-home", [c for c, _, _ in hits])

    def test_a_python_expanduser_join_FIRES(self):
        """The same shape in Python dress. The first draft's join regex could
        not cross the nested paren in expanduser("~") and this test was RED
        until it could — which is why it exists as a separate case from the
        Rust original."""
        hits = h._hits(b'root = os.path.join(os.path.expanduser("~"), '
                       b'".claude", "projects")')
        self.assertIn("constructed-home", [c for c, _, _ in hits])

    def test_a_home_source_WITHOUT_a_hidden_join_is_innocent(self):
        """NO-FIRE control, leg one: expanduser alone is everywhere and fine.
        Without this control, widening the home-source arm passes unmeasured."""
        self.assertEqual(h._hits(b'home = os.path.expanduser("~")'), [])

    def test_a_hidden_join_WITHOUT_a_home_source_is_innocent(self):
        """NO-FIRE control, leg two: joining a hidden dir off a non-home root
        (a tempdir fixture, a cache root) is not the cv shape."""
        self.assertEqual(h._hits(b'x = root.join(".claude")'), [])

    def test_an_absolute_home_path_FIRES(self):
        # a GENERIC home path, not the box's real one — the never-track
        # scanner (correctly) refuses the real path as a private needle,
        # which is the same guard-became-the-leak class hostpath had.
        hits = h._hits(b'LOG = "/home/operator/projects/x/y.log"')
        self.assertIn("home-path", [c for c, _, _ in hits])

    def test_an_identity_env_var_FIRES_but_a_config_env_var_does_not(self):
        """The whitelist is the class: session/tenant/account reads are
        baked-in identity; configured endpoints (DATABASE_HOST) and bare
        USER/HOST are not — DATABASE_HOST must NOT fire (blocker 4)."""
        self.assertIn("env-identity",
                      [c for c, _, _ in h._hits(b'u = os.environ.get("TENANT_ID")')])
        self.assertIn("env-identity",
                      [c for c, _, _ in h._hits(b'u = os.environ["CLAUDE_CODE_SESSION_ID"]')])
        self.assertEqual(h._hits(b'k = os.environ.get("HELM_SCAN_ROOTS")'), [])
        self.assertEqual(h._hits(b'h = os.environ["DATABASE_HOST"]'), [])

    def test_a_seat_name_FIRES_in_logic(self):
        hits = h._hits(b'target = "ds4pro"')
        self.assertIn("seat-name", [c for c, _, _ in hits])

    def test_a_loopback_port_FIRES(self):
        hits = h._hits(b'url = "http://127.0.0.1:8318/v1"')
        self.assertIn("host-port", [c for c, _, _ in hits])

    def test_an_ordinary_join_is_not_a_hit(self):
        self.assertEqual(h._hits(b'x = os.path.join(a, "results", "out.json")'),
                         [])


class ScanWiringTest(unittest.TestCase):
    def test_the_must_hit_control_is_wired_into_scan(self):
        """The control is not a comment: scan() must REPORT when the rung
        cannot see its own canonical example, and the control must traverse
        the REAL extraction path (plant a staged artifact, read it through
        _added_blocks). Mutation: make _control_ok return True unconditionally
        -> scan reports green on a blind extraction layer -> this dies."""
        import inspect
        src = inspect.getsource(h.scan)
        self.assertIn("_control_ok", src)
        # the control traverses the SHARED raw-diff parser, not an in-memory
        # probe of the constant — the blocker-3 requirement
        csrc = inspect.getsource(h._control_ok)
        self.assertIn("_parse_diff_blocks", csrc)
        self.assertIn("_CONTROL_DIFF", csrc)
        # and the matcher still fires on the bare constant (the control's target)
        self.assertTrue(h._hits(h._MUST_HIT))

    def test_tests_dir_is_exempt_by_default_and_opt_in(self):
        """tests/ plants identity-shaped fixtures legitimately; scanning it by
        default would make the rung cry wolf daily. The opt-in env must exist
        because a rung that CANNOT be aimed at tests is blind by design."""
        self.assertIn("tests/", h._EXEMPT_DIRS)
        self.assertFalse(h._SCAN_TESTS)  # default off in this process

    def test_scan_failure_warns_and_never_blocks(self):
        """A git failure must exit 0 with a WARN, never refuse a commit —
        the rung's one law. Mutation: return 1 on the failure path -> this
        dies (it asserts the zero)."""
        import subprocess
        script = os.path.join(os.path.dirname(__file__), "..", "helm", "hardcode.py")
        r = subprocess.run([sys.executable, script, "--staged"],
                           cwd="/tmp", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[:200])
        self.assertIn("scan FAILED", r.stderr)


if __name__ == "__main__":
    unittest.main()


class _StagedRepoMixin(unittest.TestCase):
    """A real git repo with staged content, so tests exercise the REAL
    `_staged_paths -> _added_blocks -> _hits` extraction path rather than
    feeding `_hits` synthetic `+`-prefixed payloads production never emits.

    The review verdict named exactly this: the committed tests fed
    synthetic +++/+ lines, so they passed while production extraction was
    blind. Every blocker repro below drives the real pipeline."""

    def setUp(self):
        import subprocess, tempfile
        self.dir = tempfile.mkdtemp(prefix="hardcode-test-")
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        self._git("config", "commit.gpgsign", "false")
        # an initial commit so `git diff --cached` has a base
        self._write("seed.txt", "seed\n")
        self._git("add", "seed.txt")
        self._git("commit", "-q", "-m", "seed")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _git(self, *args):
        import subprocess
        r = subprocess.run(("git",) + args, cwd=self.dir,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def _write(self, rel, text):
        import os
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def _stage(self, rel, text):
        self._write(rel, text)
        self._git("add", rel)


class BlockerReproTest(_StagedRepoMixin):
    """The four truth blockers, each pinned against the REAL pipeline."""

    def test_unrelated_home_source_and_hidden_join_do_NOT_co_fire(self):  # noqa: VACUOUS_ASSERTION — absence IS the blocker; the positive one-hunk control is asserted below
        """Blocker 1 (the exact fixture): ONE tracked file, 120 baseline
        lines. Stage Path.home() at line 1 and os.path.join(root, ".claude")
        at line 100, so `git diff -U0` yields TWO @@ hunks. The two halves of
        the cv shape live in DIFFERENT hunks -> must NOT co-fire. Control:
        both halves in ONE hunk -> must fire. Proves per-hunk grouping in the
        REAL extraction, not a matcher that re-derives stripped markers."""
        # a single file whose two edits land in two separate -U0 hunks
        body = ["# line %d" % i for i in range(120)]
        self._write("helm/x.py", "\n".join(body) + "\n")
        self._git("add", "helm/x.py")
        self._git("commit", "-q", "-m", "baseline x.py")
        # edit line 1 and line 100 -> two @@ hunks under -U0
        body[0] = "root = Path.home()"
        body[99] = 'cfg = os.path.join(root, ".claude")'
        self._write("helm/x.py", "\n".join(body) + "\n")
        self._git("add", "helm/x.py")
        hits, _, _ = h.scan(self.dir)
        self.assertNotIn("constructed-home", [c for _, c, _, _ in hits])
        # sanity: the staged diff really did split into two hunks
        blocks = h._added_blocks(self.dir, "helm/x.py")
        self.assertEqual(len(blocks), 2)
        # CONTROL: both halves in ONE hunk (adjacent lines) DOES fire
        body[0] = "root = Path.home()"
        body[1] = 'cfg = os.path.join(root, ".claude")'
        body[99] = "# line 99"
        self._write("helm/x.py", "\n".join(body) + "\n")
        self._git("add", "helm/x.py")
        hits2, _, _ = h.scan(self.dir)
        self.assertIn("constructed-home", [c for _, c, _, _ in hits2])

    def test_a_docstring_example_is_NOT_a_hardcode(self):  # noqa: VACUOUS_ASSERTION — absence IS the blocker; positive arms (real assignments fire) asserted inline below
        """Blocker 2 (the exact fixture): prose is classified before ALL
        matcher families. Each prose line must NOT fire; each matching CODE
        control MUST fire. Markdown stays empty by construction."""
        # docstring / help / comment shapes across every matcher family
        self.assertEqual(h._hits(b'"""see /home/operator/projects/x"""'), [])
        self.assertEqual(h._hits(b'"""connect localhost:8318 for dev"""'), [])
        self.assertEqual(h._hits(b'# target = "ds4pro"'), [])
        # POSITIVE ARMS: the same shapes as real code DO fire
        self.assertIn("home-path",
                      [c for c, _, _ in h._hits(b'LOG = "/home/operator/x"')])
        self.assertIn("host-port",
                      [c for c, _, _ in h._hits(b'url = "localhost:8318"')])
        self.assertIn("seat-name",
                      [c for c, _, _ in h._hits(b'target = "ds4pro"')])
        # markdown is prose by construction
        self.assertEqual(h._hits(b'LOG = "/home/operator/x"',
                                 rel="docs/example.md"), [])

    def test_prose_spans_not_lines(self):
        """The EXACT repros, pinned verbatim: prose is a lexical
        SPAN (open/close independent of line breaks), so a stateless per-line
        filter leaks two ways. Both payloads must be silent; both code
        controls must fire. Mutation: revert _code_only to the per-line
        _is_prose_line filter -> both repros go RED."""
        # repro 1: an inline help= string — prose INSIDE a code line
        self.assertEqual(
            h._hits(b'parser.add_argument("--url", '
                    b'help="example http://localhost:8318/v1")'), [])
        # repro 2: a docstring interior — prose on a line with no quote prefix
        self.assertEqual(
            h._hits(b'def x():\n    """Example path:\n'
                    b'    /home/operator/projects/x\n    """'), [])
        # POSITIVE CONTROLS: the same shapes as real VALUES must fire
        self.assertIn("host-port",
                      [c for c, _, _ in h._hits(b'url = "http://127.0.0.1:8318/v1"')])
        self.assertIn("home-path",
                      [c for c, _, _ in h._hits(b'LOG = "/home/operator/projects/x"')])

    def test_a_markdown_file_is_prose_by_construction(self):  # noqa: VACUOUS_ASSERTION — absence IS the blocker; positive arm is the cv FIRE test
        md = b'Example: root = dirs::home_dir().map(|h| h.join(".claude"))'
        self.assertEqual(h._hits(md, rel="docs/example.md"), [])

    def test_the_env_whitelist_exists_and_is_the_class(self):
        """Blocker 4: DATABASE_HOST is a configured endpoint, not identity;
        TENANT/ACCOUNT/SESSION_ID are identity. Both forms (call and
        subscript) must match the identity class."""
        self.assertEqual(h._hits(b'os.environ["DATABASE_HOST"]'), [])
        self.assertEqual(h._hits(b'os.environ["KIMI_USER"]'), [])
        self.assertIn("env-identity",
                      [c for c, _, _ in h._hits(b'os.environ["TENANT_ID"]')])
        self.assertIn("env-identity",
                      [c for c, _, _ in h._hits(b'os.environ.get("AWS_ACCOUNT_ID")')])

    def test_no_override_is_measured(self):
        """The review bar: the warning asserts "with no override", so
        an override must be MEASURED — and a measured one silences the arm.
        Both real warnings tonight (wiring's injected home_dir, beacons'
        HELM_HOME env) were overridable; pinning reduced fixtures from each
        commit. The canonical no-override cv shape MUST still fire. Mutation:
        drop the `and not _OVERRIDE_FALLBACK.search(body)` guard -> the two
        FP fixtures go RED (constructed-home returns)."""
        # wiring.py d163ea5 reduced: injected parameter + or-default
        wiring = (b'def _settings_candidates(home_dir, repo):\n'
                  b'    return os.path.join(home_dir, ".claude", "s.json")\n'
                  b'def install(settings_paths=None, home_dir=None):\n'
                  b'    home_dir = home_dir or os.path.expanduser("~")\n'
                  b'    unit = os.path.join(home_dir, ".config", "systemd")')
        self.assertNotIn("constructed-home",
                         [c for c, _, _ in h._hits(wiring)])
        # beacons.py 6d8890e reduced: the joined base is DERIVED FROM env
        # reads (HELM_HOME/MELD_HOME/HOME) — no `or <home-source>` fallback,
        # but the home is a configuration value expanduser only expands.
        beacons = (b'    value = env.get("HELM_HOME") or env.get("MELD_HOME")\n'
                   b'    base = env.get("HOME")\n'
                   b'    return os.path.realpath(os.path.join(base, ".helm"))')
        self.assertNotIn("constructed-home",
                         [c for c, _, _ in h._hits(beacons)])
        # POSITIVE CONTROL: the no-override cv shape still fires
        self.assertIn("constructed-home",
                      [c for c, _, _ in h._hits(
                          b'let root = dirs::home_dir().map(|h| '
                          b'h.join(".claude").join("projects"));')])
        self.assertIn("constructed-home",
                      [c for c, _, _ in h._hits(
                          b'root = os.path.join(os.path.expanduser("~"), '
                          b'".claude", "projects")')])

    def test_override_is_relational_not_existential(self):
        """The review bar: measured EXISTENCE of an override is not
        measured RELATION. An override unrelated to the join's base must NOT
        silence a constructed-home defect in the same hunk. Both unrelated-
        control payloads (the review's exact probes) must FIRE; both real
        override fixtures must stay SILENT. Mutation: revert to a
        hunk-global `_OVERRIDE_FALLBACK.search(body)` guard -> the two FIRE
        payloads go silent (the false negative returns)."""
        # The exact probes: an unrelated override anywhere in
        # the hunk must not suppress the real defect -> FIRE
        self.assertIn("constructed-home", [c for c, _, _ in h._hits(
            b'ignored = env.get("HELM_HOME")\n'
            b'root = Path.home().join(".claude")')])
        self.assertIn("constructed-home", [c for c, _, _ in h._hits(
            b'other = other or Path.home()\n'
            b'root = dirs::home_dir().join(".claude")')])
        # and the related overrides still stay SILENT — the
        # association works both ways
        self.assertNotIn("constructed-home", [c for c, _, _ in h._hits(
            b'    home_dir = home_dir or os.path.expanduser("~")\n'
            b'    unit = os.path.join(home_dir, ".config", "systemd")')])
        self.assertNotIn("constructed-home", [c for c, _, _ in h._hits(
            b'    base = env.get("HOME")\n'
            b'    return os.path.realpath(os.path.join(base, ".helm"))')])

    def test_the_must_hit_control_traverses_real_extraction(self):
        """Blocker 3 (the exact fixture): ONE raw-diff->added-blocks
        parser shared by live extraction AND the control. parser(_CONTROL_DIFF)
        yields a block containing MUST_HIT and control_ok is True; breaking the
        parser to [] makes control_ok False (never silently green); a temp-git
        staged canonical cv line is seen by scan as constructed-home."""
        from unittest import mock
        # parser sees the synthetic control diff
        blocks = h._parse_diff_blocks(h._CONTROL_DIFF)
        self.assertTrue(any(h._MUST_HIT in b for b in blocks))
        self.assertTrue(h._control_ok(self.dir))
        # a broken parser (returns no blocks) -> control UNKNOWN, not green
        with mock.patch.object(h, "_parse_diff_blocks", return_value=[]):
            self.assertFalse(h._control_ok(self.dir))
        # the canonical cv line, staged for real, is seen end-to-end
        self._stage("helm/cv.py",
                    'let root = dirs::home_dir().map(|h| '
                    'h.join(".claude").join("projects"));\n')
        hits, control_ok, _ = h.scan(self.dir)
        self.assertIn("constructed-home", [c for _, c, _, _ in hits])
        self.assertTrue(control_ok)
