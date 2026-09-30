#!/usr/bin/env python3
"""The recursive-grep rung (task/3384): a Bash or Monitor command never runs a
grep that walks a tree with no bound.

WHY. Eight orphaned ugrep processes were found on the hub, 24 minutes to 9.75
days old, two of them near 1h49m of CPU each. In a seat's shell `grep` is a
shell FUNCTION that runs the harness's ugrep, and a recursive grep with no
path walks the whole working directory; this box has crashed from one before.
The standing rule (the /dev search law) is `git grep` inside a repository and
`/usr/bin/rg` with an explicit path outside one, and nothing refused the act.

THE MATRIX IS THE TASK'S OWN: every surface-by-state cell task/3384 set is a
test below, and each allow cell is the refusal's must-miss — a rung that
refused every grep would satisfy every refusal arm forever. The hook arms
drive the SHIPPED entry, `cmd_argv_guard`, for Bash and Monitor, and one arm
runs the real entry script to prove the rung adds no import to the hook path.

THE RIG is a temp HOME holding `dev/org/repo` (a checkout), `dev/solo` (a
checkout directly under dev) and `dev/org` itself (an org directory, which is
not a checkout), so every path arm resolves against a tree this file made.

The probe strings spell the program as `GREP` and `c()` fills it in, so this
file, read by a person or searched by a seat, never holds a recursive grep a
shell could run whole.
"""
import contextlib
import io
import json
import os as _os
import shutil
import subprocess
import sys as _sys
import tempfile
import unittest
from unittest import mock

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-rgrep-", var="HELM_HOME")

from helm import chat  # noqa: E402

G = "gr" + "ep"
RIG = {}


def c(text):
    """A probe command: `GREP` spelled out as the program's name."""
    return text.replace("GREP", G)


def setUpModule():
    tmp = _os.path.realpath(tempfile.mkdtemp(prefix="helm-test-rgrep-"))
    home = _os.path.join(tmp, "h")
    for d in ("dev/org/repo/.git", "dev/org/repo/src", "dev/solo/.git",
              "dev/org/repo-wt/lane/src", "dev/org/repo-wt/compose/t1",
              "dev/org/repo/src/cache-wt"):
        _os.makedirs(_os.path.join(home, d))
    for f in ("dev/org/repo-wt/lane/.git", "dev/org/repo-wt/compose/t1/.git"):
        with open(_os.path.join(home, f), "w") as fh:  # a linked worktree
            fh.write("gitdir: elsewhere\n")
    RIG.update(tmp=tmp, home=home, dev=_os.path.join(home, "dev"),
               org=_os.path.join(home, "dev", "org"),
               repo=_os.path.join(home, "dev", "org", "repo"))
    RIG["env"] = mock.patch.dict(_os.environ, {"HOME": home})
    RIG["env"].start()


def tearDownModule():
    RIG["env"].stop()
    shutil.rmtree(RIG["tmp"], ignore_errors=True)


def refusal(command, cwd=None):
    return chat.recursive_grep_refusal(c(command), cwd or RIG["repo"])


class Matrix(unittest.TestCase):
    def refused(self, commands, cwd=None, root=None):
        """Each command is refused; `root` None asserts the no-path kind (no
        operand and no root), a path asserts the broad root it walks."""
        for command in commands:
            with self.subTest(command=command):
                hit = refusal(command, cwd)
                self.assertIsNotNone(hit, c(command))
                self.assertEqual(hit[3], root, (c(command), hit))
                self.assertEqual(hit[1] is None, root is None,
                                 (c(command), hit))

    def passed(self, commands, cwd=None):
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(refusal(command, cwd), c(command))


class BareRecursiveTest(Matrix):
    """Cell 1: a recursive grep with no path walks the whole working
    directory, whatever it is, in every flag spelling."""

    def test_every_spelling_of_recursion_with_no_path(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        self.refused((
            "GREP -r foo", "GREP -R foo", "GREP --recursive foo",
            "GREP --dereference-recursive foo", "GREP -rn foo",
            "GREP -nri foo", "GREP -Rl foo", "GREP -irn 'two words'",
            "GREP foo -r", "GREP foo -rn", "GREP -r -e foo",
            "GREP -rn -e foo -e bar", "GREP -rnF -- -foo",
            "GREP -rn --include='*.py' foo", "GREP -rn --include '*.py' foo",
            "GREP -r --exclude-dir .git foo", "GREP -r -A 3 foo",
            "GREP -rA3 foo", "GREP -rm1 foo", "GREP -r -m 1 foo",
            "GREP -d recurse foo", "GREP -drecurse foo",
            "GREP --directories=recurse foo",
            "GREP --directories recurse foo",
            "GREP -d dereference-recurse foo", "GREP -rn \"$pat\"",
            "GREP -rl foo > out.txt", "GREP -rl foo | head",
            "GREP -rl foo 2>/dev/null", "cd /srv/x && GREP -rn foo"))

    def test_a_pipe_on_stdin_stops_no_walk(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """Measured in a scratch tree: under ugrep `cmd | grep -r x` read
        the pipe AND walked the working directory; GNU grep -r ignored the
        pipe and walked."""
        self.refused(("cmd | GREP -r foo", "cmd | GREP -rn foo | head",
                      "cmd | /usr/bin/GREP -r foo"))

    def test_every_program_of_the_family(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        self.refused((
            "eGREP -r 'a|b'", "fGREP -rn foo", "uGREP -r foo", "ug -r foo",
            "/usr/bin/GREP -r foo", "/bin/GREP -r foo", "command GREP -r foo",
            "\\GREP -r foo", "\"GREP\" -r foo", "sudo GREP -r foo",
            "timeout 30 GREP -rn foo", "LC_ALL=C GREP -rn foo",
            "time GREP -r foo", "nice -n 5 GREP -r foo",
            "if GREP -rq foo; then echo y; fi",
            "for f in a b; do GREP -rn \"$f\"; done", "(GREP -r foo)",
            "{ GREP -r foo; }", "! GREP -rq foo"))

    def test_rgrep_is_grep_r(self):
        """/usr/bin/rgrep is `exec grep -r "$@"`: recursive by its name."""
        self.assertEqual(refusal("rGREP foo"), (c("rGREP foo"), None,
                                               "rgrep", None))

    def test_gnu_long_option_prefixes(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """GNU grep accepts an unambiguous prefix (measured: --rec and --der
        both walked the scratch tree)."""
        self.refused(("/usr/bin/GREP --rec foo", "/usr/bin/GREP --der foo",
                      "/usr/bin/GREP --dir=recurse foo",
                      "/usr/bin/GREP --dir recurse foo"))

    def test_ugrep_recursion_without_r(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """Under the harness grep (ugrep) --depth, -NUM, --index and a glob
        holding a `/` each enable -r (ugrep --help; the depth and glob forms
        measured walking the scratch tree)."""
        self.refused((
            "GREP -3 foo", "cmd | GREP -2 foo", "GREP --depth=2 foo",
            "GREP --depth 2 foo", "ug --index foo", "uGREP -3-5 foo",
            "GREP --include='src/*.py' foo", "GREP -g 'src/*.py' foo",
            "GREP --glob=src/*.py foo"))

    def test_the_refusal_names_how_it_recurses(self):
        self.assertEqual(refusal("GREP -rn foo"), (c("GREP -rn foo"), None,
                                                   "-rn", None))
        self.assertEqual(refusal("GREP -d recurse foo")[2], "-d recurse")
        self.assertEqual(refusal("GREP --depth 2 foo")[2], "--depth 2")

    def test_no_pattern_walks_nothing(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal of the same grep given a pattern
        """Measured in a scratch tree: a recursive grep given no pattern at
        all prints usage and exits 2 under both engines, walking nothing;
        ugrep's --match is a pattern (it matches every line)."""
        self.assertIsNotNone(refusal("GREP -rn foo"))
        self.assertIsNotNone(refusal("GREP -r --match"))
        self.passed(("GREP -r", "GREP -rn", "cmd | GREP -r"))

    def test_the_spelling_is_capped(self):
        hit = refusal("GREP -rn " + "x" * 200)
        self.assertEqual(len(hit[0]), chat._GREP_SPELL)
        self.assertTrue(hit[0].endswith("…"))


class BroadRootTest(Matrix):
    """Cell 2: a recursive grep over /, ~, $HOME, /home/<user>, ~/dev and
    their kin walks a tree with no useful bound."""

    def test_the_broad_roots_as_typed(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal naming its root
        """Each hit carries the operand as typed and the root it resolves
        to; the refusal names both."""
        home, dev = RIG["home"], RIG["dev"]
        for command, typed, root in (
                ("GREP -rn foo /", "/", "/"),
                ("GREP -rn foo ~", "~", home),
                ("GREP -rn foo ~/", "~/", home),
                ("GREP -rn foo $HOME", "$HOME", home),
                ("GREP -rn foo \"$HOME\"", "\"$HOME\"", home),
                ("GREP -rn foo ${HOME}", "${HOME}", home),
                ("GREP -rn foo /home", "/home", "/home"),
                ("GREP -rn foo /home/someone", "/home/someone",
                 "/home/someone"),
                ("GREP -rn foo /home/someone/", "/home/someone/",
                 "/home/someone"),
                ("GREP -rn foo /home/someone/dev", "/home/someone/dev",
                 "/home/someone/dev"),
                ("GREP -rn foo ~/dev", "~/dev", dev),
                ("GREP -rn foo $HOME/dev", "$HOME/dev", dev),
                ("GREP -rn foo \"$HOME/dev\"", "\"$HOME/dev\"", dev),
                ("GREP -rn foo /tmp", "/tmp", "/tmp"),
                ("GREP -rn foo ~/dev/*", "~/dev/*", dev),
                ("GREP -rn foo /home/someone/dev/..",
                 "/home/someone/dev/..", "/home/someone"),
                ("GREP -rn foo src ~", "~", home),
                ("GREP -r -e foo /", "/", "/"),
                ("GREP -rn foo ~/dev/org", "~/dev/org", RIG["org"]),
                ("GREP -rn foo ../..", "../..", dev)):
            with self.subTest(command=command):
                hit = refusal(command)
                self.assertIsNotNone(hit, c(command))
                self.assertEqual(hit[1:2] + hit[3:], (typed, root),
                                 (c(command), hit))

    def test_the_directory_the_command_stands_in(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        self.refused(("GREP -rn foo .", "GREP -rn foo *",
                      "cd ~ && GREP -rn foo ."), cwd=RIG["home"],
                     root=RIG["home"])
        self.refused(("cd ~ && GREP -rn foo .",), root=RIG["home"])
        self.refused(("cd ~/dev; GREP -rn foo .",), root=RIG["dev"])


class BroadStoreTest(Matrix):
    """Cell 2, the stores no list of spellings names: a lane parent holds
    every worktree of its checkout (185 of them, 7.5G, measured on the hub),
    and ~/.helm and ~/.claude hold tens of gigabytes of transcripts. A
    directory one level below `/` or below a home, a lane parent
    `<checkout>-wt` and every directory between it and a worktree, and helm's
    own seat store are broad roots."""

    def test_one_level_below_the_root_or_a_home(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal naming its root
        home = RIG["home"]
        for command, root in (
                ("GREP -rn foo /usr", "/usr"),
                ("GREP -rn foo /proc", "/proc"),
                ("GREP -rn foo /var/", "/var"),
                ("GREP -rn foo /scratch", "/scratch"),
                ("GREP -rn foo ~/.helm", home + "/.helm"),
                ("GREP -rn foo $HOME/.claude", home + "/.claude"),
                ("GREP -rn foo /home/someone/.cache", "/home/someone/.cache")):
            with self.subTest(command=command):
                hit = refusal(command)
                self.assertIsNotNone(hit, c(command))
                self.assertEqual(hit[3], root, (c(command), hit))

    def test_a_lane_parent_and_the_directories_above_its_worktrees(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        wt = RIG["repo"] + "-wt"
        self.refused(("GREP -rn foo ~/dev/org/repo-wt",
                      "GREP -rn foo ~/dev/org/repo-wt/"), root=wt)
        self.refused(("GREP -rn foo ~/dev/org/repo-wt/compose",),
                     root=wt + "/compose")
        self.refused(("GREP -rn foo .",), cwd=wt, root=wt)

    def test_helms_seat_store(self):  # noqa: VACUOUS_ASSERTION — refused() asserts a non-empty refusal naming the root for every command
        from helm import home as helm_home
        seats = _os.path.join(helm_home.global_dir(), "seats")
        self.refused(("GREP -rn foo " + seats,), root=seats)
        self.refused(("GREP -rn foo " + helm_home.global_dir(),),
                     root=helm_home.global_dir())

    def test_narrow_paths_beside_them_pass(self):  # noqa: VACUOUS_ASSERTION — each store above is refused on the same rung
        from helm import home as helm_home
        self.passed((
            "GREP -rn foo /usr/share/doc", "GREP -rn foo /etc/systemd/system",
            "GREP -rn foo ~/.config/helm",
            "GREP -rn foo ~/dev/org/repo-wt/lane",
            "GREP -rn foo ~/dev/org/repo-wt/lane/src",
            "GREP -rn foo ~/dev/org/repo-wt/compose/t1",
            "GREP -rn foo ~/dev/org/repo/src/cache-wt",
            "GREP -rn foo " + _os.path.join(
                helm_home.global_dir(), "seats", "x", "journal")))
        self.passed(("GREP -rn foo .",), cwd=RIG["repo"] + "-wt/lane")


class NarrowPathTest(Matrix):
    """Cell 3: a recursive grep over a narrow explicit path passes."""

    def test_narrow_explicit_paths_pass(self):  # noqa: VACUOUS_ASSERTION — the bare arm of each spelling is refused in BareRecursiveTest on the same rung
        self.assertIsNotNone(refusal("GREP -rn foo"))
        self.passed((
            "GREP -rn foo src/", "GREP -rn foo helm/chat.py",
            "GREP -r foo .", "GREP -rn foo ~/dev/org/repo",
            "GREP -rn foo ~/dev/solo", "GREP -rn foo -- src",
            "GREP -r -e foo src", "GREP -rn foo *.py",
            "GREP -rn foo /tmp/claude-1000/x", "GREP -r foo -",
            "GREP -rn foo /home/someone/dev/org/repo/src",
            "GREP -rn foo ~/dev/nosuchdir", "GREP -rl foo src | head",
            "cd src && GREP -rn foo ."))

    def test_what_the_text_does_not_settle_passes(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal of the quoted spelling on the same rung
        """An unquoted expansion may carry the path, and a directory the
        text cannot resolve is not known to be broad."""
        self.assertIsNotNone(refusal("GREP -rn \"$pat\""))
        self.passed(("GREP -rn $args", "GREP -rn foo \"$dir\"",
                     "cd \"$d\" && GREP -rn foo .", "GREP -rn foo $PWD",
                     "GREP -r --from=list.txt foo", "GREP -r -$X foo"))

    def test_a_relative_path_without_a_cwd_is_not_known_broad(self):
        self.assertIsNone(chat.recursive_grep_refusal(c("GREP -rn foo ..")))
        self.assertIsNotNone(chat.recursive_grep_refusal(c("GREP -rn foo")))


class OtherProgramsTest(Matrix):
    """Cell 4: git grep and rg are other programs, with any path."""

    def test_git_grep_and_rg_pass(self):  # noqa: VACUOUS_ASSERTION — the bare grep arm is refused in BareRecursiveTest on the same rung
        self.assertIsNotNone(refusal("GREP -rn foo"))
        self.passed((
            "git GREP -n foo", "git GREP -rn foo", "git -C /x GREP -n foo",
            "/usr/bin/rg -n foo ~", "/usr/bin/rg foo /", "rg -n foo",
            "rg foo ~/dev", "pGREP -f foo", "ps aux | pGREP x"))


class StdinFilterTest(Matrix):
    """Cell 5: a grep that is not recursive reads its stdin or its files."""

    def test_filters_and_plain_greps_pass(self):  # noqa: VACUOUS_ASSERTION — the recursive pipe arm is refused in BareRecursiveTest on the same rung
        self.assertIsNotNone(refusal("cmd | GREP -r foo"))
        self.passed((
            "cmd | GREP foo", "cmd | GREP -n foo", "ps aux | GREP -v GREP",
            "tail -f log | GREP --line-buffered foo", "cmd | eGREP 'a|b'",
            "cmd | GREP -c foo | GREP -v 0", "GREP -n foo file.txt",
            "GREP foo", "GREP -c foo a b", "GREP -d skip foo",
            "GREP -er file.txt", "GREP -f rules.txt data", "GREP -i -e r f",
            "GREP --color=auto -n foo f", "GREP --regexp=-r f"))

    def test_gnu_minus_num_is_context_not_depth(self):  # noqa: VACUOUS_ASSERTION — the harness spelling of the same argv is refused in BareRecursiveTest
        """Measured: GNU grep -2 read the pipe; ugrep -2 walked. A path, a
        wrapper that execs, egrep and fgrep all reach GNU."""
        self.assertIsNotNone(refusal("cmd | GREP -2 foo"))
        self.passed(("cmd | /usr/bin/GREP -2 foo", "cmd | fGREP -3 foo",
                     "cmd | eGREP -2 foo", "cmd | sudo GREP -3 foo",
                     "cmd | command GREP -2 foo"))


class DataTest(Matrix):
    """Cell 6: the grep text as data is not a grep that runs."""

    def test_quoted_and_recorded_text_passes(self):  # noqa: VACUOUS_ASSERTION — the same text run as a command is refused in BareRecursiveTest
        self.assertIsNotNone(refusal("GREP -rn foo"))
        self.passed((
            "echo \"GREP -rn foo\"", "echo GREP -rn foo",
            "printf '%s\\n' 'GREP -r foo /'",
            "git commit -m \"use GREP -rn foo\"",
            "helm chat post --room r \"GREP -r foo\"",
            "cat <<'EOF'\nGREP -rn foo\nEOF",
            "cat > x.sh <<'EOF'\nGREP -rn foo\nEOF",
            "python3 - <<'EOF'\n# GREP -r foo\nEOF",
            "gh pr comment 1 --body 'GREP -r x ~'", "rg 'GREP -r' docs/",
            "git GREP 'GREP -rn'", "git log --GREP='GREP -r'",
            "node -e \"run('GREP -r x')\"", "jq -r '.x' f.json",
            "pGREP -f 'GREP -r'", "ssh host 'GREP -r foo /'",
            "docker exec c GREP -r foo /"))


class WrapperTest(Matrix):
    """Cell 7: a shell or an evaluator wrapping a bare recursive grep runs
    it, and so does a substitution."""

    def test_wrapped_greps_are_refused(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        self.refused((
            "sh -c 'GREP -r foo'", "bash -c \"GREP -rn foo\"",
            "sudo sh -c 'GREP -r foo'", "eval 'GREP -r foo'",
            "echo \"$(GREP -rl foo)\"", "x=$(GREP -rl foo)",
            "for f in $(GREP -rl foo); do echo $f; done",
            "diff <(GREP -r a) b", "bash <<'EOF'\nGREP -rn foo\nEOF",
            "sh -s <<'EOF'\nGREP -r foo\nEOF", "echo `GREP -rl foo`"))
        self.refused(("bash -lc 'GREP -R foo ~'",), root=RIG["home"])
        self.refused(("sudo sh -c 'GREP -r foo /'",), root="/")


class LimitsTest(Matrix):
    """What the rung does NOT read, pinned so a change to it is deliberate:
    a grep another program runs, and a body piped into a shell."""

    def test_the_stated_limits_pass(self):  # noqa: VACUOUS_ASSERTION — the same grep standing alone is refused in BareRecursiveTest
        self.assertIsNotNone(refusal("GREP -rn foo"))
        self.passed(("xargs GREP -rn foo", "find . -exec GREP -r x {} +",
                     "watch GREP -r foo", "cat <<'EOF' | bash\nGREP -r x\nEOF",
                     "/usr/bin/GREP -r --inc '*.py' foo"))


class UnsettledTextTest(unittest.TestCase):
    """Where the shell reader cannot settle the text, the rung PASSES, as
    the shared-checkout rung does, and a defect in the reader passes too: a
    guard on every Bash call misses an exotic edge rather than block work it
    cannot read. A crude reading was built and measured over emberian/
    dregg's tracked *.sh and *.md: it refused 14 lines, every one a line
    bash cannot parse (nothing in it would run), and found no real one."""

    CASE = "case $x in a) %s;; esac"

    def test_the_reader_really_cannot_settle_the_control(self):
        with self.assertRaises(chat._Unsettled):
            chat._ShellReader(self.CASE % c("GREP -rn foo")).read()

    def test_unsettled_text_passes(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal of the same grep in settled text on the same rung
        self.assertIsNotNone(refusal("GREP -rn foo"))
        for command in (self.CASE % "GREP -rn foo",
                        "echo it's `GREP -rn foo`"):
            with self.subTest(command=command):
                self.assertIsNone(refusal(command), c(command))

    def test_a_reader_defect_fails_open(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal of the same command with the reader intact
        self.assertIsNotNone(refusal("GREP -rn foo"))
        with mock.patch.object(chat, "_commands_run",
                               side_effect=RuntimeError("defect")):
            self.assertIsNone(refusal("GREP -rn foo"))


class HookTest(unittest.TestCase):
    """The shipped entry: exit 2 and both routes on a refusal, exit 0 on the
    must-miss, for Bash and Monitor alike."""

    def hook(self, tool="Bash", cwd=None, **tool_input):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "session_id": "sess-rgrep", "cwd": cwd or RIG["repo"],
                   "tool_use_id": "toolu_rgrep", "tool_input": tool_input}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(_sys, "stdin",
                               io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue() + err.getvalue()

    def test_a_bare_recursive_grep_exits_2_and_names_both_routes(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts rc 2
        for tool in ("Bash", "Monitor"):
            with self.subTest(tool=tool):
                rc, out = self.hook(tool, command=c("GREP -rn foo"))
                self.assertEqual(rc, 2, out)
                self.assertIn("[helm argv-guard] BLOCKED", out)
                self.assertIn(c("`GREP -rn foo` is recursive (-rn)"), out)
                self.assertIn("the whole working directory", out)
                self.assertIn("git grep -n PATTERN", out)
                self.assertIn("/usr/bin/rg -n PATTERN <narrow path>", out)

    def test_a_broad_root_and_a_depth_say_so(self):
        rc, out = self.hook(command=c("GREP -rn foo ~"))
        self.assertEqual(rc, 2, out)
        self.assertIn("walks ~ (%s" % RIG["home"][:chat._GREP_SPELL - 1],
                      out)
        self.assertIn(", a broad root.", out)
        rc, out = self.hook(command=c("GREP -rn foo /"))
        self.assertIn("walks /, a broad root.", out)
        rc, out = self.hook(command=c("cmd | GREP -2 foo"))
        self.assertEqual(rc, 2, out)
        self.assertIn("write -C NUM", out)
        rc, out = self.hook(command=c("GREP -rn foo"))
        self.assertEqual(rc, 2, out)
        self.assertIn("the whole working directory", out)
        self.assertNotIn("-C NUM", out)

    def test_a_wrapped_grep_exits_2(self):
        rc, out = self.hook(command=c("bash -c 'GREP -rn foo'"))
        self.assertEqual(rc, 2, out)
        self.assertIn("BLOCKED", out)

    def test_the_must_miss_passes_the_entry(self):  # noqa: VACUOUS_ASSERTION — the refusal arm above drives the same entry to rc 2, so rc 0 here is the rung declining
        for command in ("GREP -rn foo src/", "git GREP -n foo",
                        "/usr/bin/rg -n foo ~", "cmd | GREP foo",
                        "echo \"GREP -rn foo\""):
            with self.subTest(command=command):
                rc, out = self.hook(command=c(command))
                self.assertEqual(rc, 0, out)
                self.assertNotIn("BLOCKED", out)

    def test_a_write_is_not_a_command(self):  # noqa: VACUOUS_ASSERTION — the refusal arm above drives the same entry to rc 2; a file's CONTENT is never run by this call
        rc, out = self.hook("Write", file_path="/tmp/x.sh",
                            content=c("GREP -rn foo\n"))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("BLOCKED", out)


class EntryImportTest(unittest.TestCase):
    """THE HOOK BUDGET, bound structurally as HookEntryBudgetTest binds it:
    the rung reads the command with the reader the entry already holds, so a
    refused grep loads exactly the modules a passing command loads."""

    ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

    def run_hook(self, command):
        probe = (
            "import json, sys, runpy\n"
            "sys.argv = ['helm', 'chat', 'argv-guard', '--hook-json']\n"
            "rc = 0\n"
            "try:\n"
            "    runpy.run_path(%r, run_name='__main__')\n"
            "except SystemExit as e:\n"
            "    rc = e.code or 0\n"
            "sys.stderr.write('PROBE ' + json.dumps({'rc': rc, 'mods': "
            "sorted(sys.modules)}) + '\\n')\n"
            % _os.path.join(self.ROOT, "bin", "helm"))
        payload = json.dumps({"tool_name": "Bash", "session_id": "rgrep-arm",
                              "cwd": RIG["repo"],
                              "tool_input": {"command": command}})
        p = subprocess.run([_sys.executable, "-c", probe], input=payload,
                           capture_output=True, text=True,
                           env=dict(_os.environ, HELM_NO_TREE_WARNING="1"))
        line = [l for l in p.stderr.splitlines() if l.startswith("PROBE ")]
        self.assertTrue(line, "the probe never reported: " + p.stderr[-800:])
        report = json.loads(line[-1][len("PROBE "):])
        return report["rc"], set(report["mods"]), p.stderr

    def test_a_refused_grep_imports_nothing_a_pass_does_not(self):
        rc, refused, err = self.run_hook(c("GREP -rn foo"))
        self.assertEqual(rc, 2, err[-600:])
        self.assertIn("is recursive", err)
        rc, passed, err = self.run_hook(c("cmd | GREP foo"))
        self.assertEqual(rc, 0, err[-600:])
        self.assertIn("helm.chat", passed)
        self.assertEqual(sorted(refused - passed), [])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
