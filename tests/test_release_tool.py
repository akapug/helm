#!/usr/bin/env python3
"""The release command, driven end to end against a fixture world.

`scripts/release/release.py` publishes a helm release: it builds one commit on
top of the public main from the trunk tree minus the omit list, gates it, and
then, only with --publish, stages it on the private repository, fast-forwards
the public main, pushes an annotated tag and creates the GitHub release. Every
arm here runs the REAL entry point as a subprocess, against:

  * a source repository (the "trunk") with a CHANGELOG, a version, a
    `bin/helm` and an installer, like helm's own tree;
  * a bare "public" repository that already carries the previous release;
  * a bare "private" repository for the staging branch;
  * a stub `gh` first on PATH that records its argv and the notes it was
    handed, and a stub `gitleaks` that reports nothing.

No arm reaches a network, a real GitHub repository or the machine's own
private-needle and never-track files: both are pointed at fixture files. HOME
is a fixture directory too, so the command's defaults under the home (the
releases directory) land in the fixture.

The command refuses a work directory on a memory filesystem (the backup it
keeps there would not survive a reboot), so the fixture lives on a disk-backed
directory: the system temp directory when it is on disk, else /var/tmp.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "scripts", "release", "release.py")
OMIT_LIST = os.path.join(ROOT, "scripts", "release", "omit.txt")
MEMORY_FS = ("tmpfs", "ramfs")


def _mounts():
    """[(mount point, filesystem type)] from /proc/self/mountinfo, in the
    order listed. The tests' own reading, independent of the command's."""
    rows = []
    with open("/proc/self/mountinfo", encoding="utf-8") as f:
        for line in f:
            left, _, right = line.partition(" - ")
            point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)),
                           left.split()[4])
            rows.append((point, right.split()[0]))
    return rows


def _filesystem(path):
    """(mount point, filesystem type) holding `path`: the longest mount point
    over its real path, the last one listed when mounts stack."""
    real, best = os.path.realpath(path), ("", None)
    for point, fstype in _mounts():
        if ((real == point or real.startswith(point.rstrip("/") + "/"))
                and len(point) >= len(best[0])):
            best = (point, fstype)
    return best


def _disk_root():
    for cand in (tempfile.gettempdir(), "/var/tmp"):
        if os.access(cand, os.W_OK) and _filesystem(cand)[1] not in MEMORY_FS:
            return cand
    raise AssertionError("no writable disk-backed directory for the fixture: "
                         "the command refuses a work directory in memory")


def _memory_mount():
    """A writable mount point of a memory filesystem, or None."""
    return next((p for p, fs in _mounts()
                 if fs in MEMORY_FS and p != "/" and os.access(p, os.W_OK)), None)


def _tool():
    """The command's module, for the arms that call one of its functions."""
    spec = importlib.util.spec_from_file_location("_release_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _work_line(out):
    """The work directory the command said it uses, or None."""
    m = re.search(r"^work\s+(\S+)", out, re.M)
    return m.group(1) if m else None


def needle_pieces(text, size=5):
    """The `size`-character pieces of NEEDLE that `text` carries, other than
    the ones the placeholder `<needle #1>` spells itself."""
    pieces = {NEEDLE[i:i + size] for i in range(len(NEEDLE) - size + 1)}
    return sorted(p for p in pieces if p in text and p not in "<needle #1>")


VERSION = "9.9.1"
OWNER = "example-owner"
IDENT = "%s <%s@users.noreply.github.com>" % (OWNER, OWNER)
NEEDLE = "zz-fixture-needle-zz"

# The private files the command reads from OUTSIDE the tree, under the
# fixture HOME. Every value is synthetic: the patterns stand in for the real
# seat names, private tokens and owner name, which never enter the tree.
KEEP_FILE = os.path.join(".config", "helm", "release-attr-keep.json")
AUDIT_FILE = os.path.join(".config", "helm", "release-audit.json")
AUDIT = {
    "seat": r"(?<![A-Za-z0-9_/.-])(fixture-seat-\d+|@fixturefamily)(?![A-Za-z0-9_])",
    "private_token": r"fixture-private-token-\d+",
    "owner": r"\bFixtureowner\b",
    "literals": {"fixture-host": "fixture-host.lan"},
}
SEAT_LINE = "# fixture-seat-7 found the counter bug and fixed the reset."
WIDGET = '"""The widget counts."""\n%s\nCOUNT = 0  # as fixture-seat-8 left it\n' % SEAT_LINE
NOTES = textwrap.dedent("""\
    Notes.

    fixture-private-token-1 ships in this file.
    See task/123, measured 2026-01-02.
    The owner said to keep it; Fixtureowner agreed.
    Write to someone@fixture-mail.zz for access, not to someone@example.com.
    It runs on fixture-host.lan.
    """)

SECTION = textwrap.dedent("""\
    Changes since 9.9.0.

    The fixture release teaches the widget to count.

    - the widget counts
    - the counter resets""")

CHANGELOG = "# Changelog\n\n## %s — 2026-09-25\n\n%s\n\n## 9.9.0 — 2026-09-01\n\nOld.\n" % (
    VERSION, SECTION)

BIN_HELM = textwrap.dedent("""\
    #!/usr/bin/env python3
    import sys
    if sys.argv[1:] == ["--help"]:
        print("usage: helm <verb>")
    elif sys.argv[1:] == ["doctor"]:
        print("helm doctor: 3 ok, 0 warn, 0 fail")
    else:
        sys.exit(2)
    """)

INSTALL_SH = textwrap.dedent("""\
    #!/bin/sh
    set -e
    [ "$1" = "--prefix" ] || exit 2
    mkdir -p "$2"
    here=$(cd -P "$(dirname "$0")/.." && pwd)
    ln -s "$here/bin/helm" "$2/helm"
    "$2/helm" --help >/dev/null
    """)

# The stub gh: one JSON line per call, carrying argv and the notes file's text.
STUB_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    notes = None
    if "--notes-file" in args:
        with open(args[args.index("--notes-file") + 1], encoding="utf-8") as f:
            notes = f.read()
    with open(os.environ["STUB_GH_RECORD"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "notes": notes}) + "\\n")
    """)

# The stub gitleaks: a clean report wherever it is asked to write one. It is
# the last gate before the writes, so it is also where an arm plants a
# public-side commit BETWEEN the read and the first write (STUB_MOVE_SEED).
STUB_GITLEAKS = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, subprocess, sys
    args = sys.argv[1:]
    with open(args[args.index("--report-path") + 1], "w") as f:
        f.write("[]")
    seed = os.environ.get("STUB_MOVE_SEED")
    if seed:
        with open(os.path.join(seed, "HOTFIX.md"), "w") as f:
            f.write("a public-side commit\\n")
        for argv in (["add", "-A"], ["commit", "-q", "-m", "a public-side commit"],
                     ["push", "-q", os.environ["STUB_MOVE_PUBLIC"], "main:refs/heads/main"]):
            subprocess.run(["git", "-C", seed] + argv, check=True)
    """)

# A stub gitleaks that reports one finding, in the file STUB_LEAK_FILE names
# under the scanned directory (gitleaks reports the path it scanned).
STUB_GITLEAKS_FINDING = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    found = [{"File": os.path.join(args[-1], os.environ["STUB_LEAK_FILE"]),
              "RuleID": "generic-api-key", "Secret": "REDACTED"}]
    with open(args[args.index("--report-path") + 1], "w") as f:
        json.dump(found, f)
    """)

# A post-receive hook for the private repository: the moment the stage write
# lands, a public-side commit moves the public main (the race after the first
# write). GIT_DIR is unset because a hook inherits the receiving repository's.
POST_RECEIVE = textwrap.dedent("""\
    #!/bin/sh
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_QUARANTINE_PATH
    cd "%(seed)s" || exit 1
    echo "a public-side commit" > HOTFIX.md
    git add -A && git commit -q -m "a public-side commit" \\
      && git push -q "%(public)s" main:refs/heads/main
    """)

# The command run as __main__ with the subprocess calls whose argv, joined by
# spaces, carries STUB_ON interfered with. STUB_DO=timeout raises the
# TimeoutExpired that call's own timeout raises, without waiting it out.
# STUB_DO=lock plants the lock of the index file the call is handed (what a
# git process holding that index leaves) and then runs the call, so git
# itself fails. STUB_DO=fail runs, in the call's place, a command that
# writes STUB_ERR to stderr and exits 1, after the path STUB_GO exists when
# that is set: a test that must act between two of the command's lines
# creates it once it has.
DRIVER = textwrap.dedent("""\
    import os, runpy, subprocess, sys
    real = subprocess.run
    def run(argv, *args, **kw):
        if os.environ["STUB_ON"] in " ".join(map(str, argv)):
            if os.environ["STUB_DO"] == "timeout":
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            if os.environ["STUB_DO"] == "fail":
                argv = (sys.executable, "-c", "import os, sys, time\\n"
                        "while os.environ.get('STUB_GO') and not "
                        "os.path.exists(os.environ['STUB_GO']): time.sleep(0.01)\\n"
                        "sys.stderr.write(os.environ['STUB_ERR']); sys.exit(1)")
            else:
                open(kw["env"]["GIT_INDEX_FILE"] + ".lock", "x").close()
        return real(argv, *args, **kw)
    subprocess.run = run
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")
    """)

# THE SPY: the command run as __main__ with every child process it starts
# recorded, argv whole, one JSON line each in SPY_RECORD. Every door the
# subprocess module has to a child (run, call, check_output, Popen itself)
# goes through the module's Popen, so the spy stands there, not on one door.
SPY = textwrap.dedent("""\
    import json, os, runpy, subprocess, sys
    Real = subprocess.Popen
    class Spy(Real):
        def __init__(self, args, *rest, **kw):
            argv = [args] if isinstance(args, (str, bytes)) else list(args)
            with open(os.environ["SPY_RECORD"], "a", encoding="utf-8") as f:
                f.write(json.dumps([os.fsdecode(a) for a in argv]) + "\\n")
            super().__init__(args, *rest, **kw)
    subprocess.Popen = Spy
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")
    """)


def _git_verb(argv):
    """The git subcommand of a git argv: its first word after git's own
    options (`-C <dir>` and `-c <name=value>` take a value)."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in ("-C", "-c") else 1
    return argv[i] if i < len(argv) else None


def outward(argv):
    """Is this child an OUTWARD act of a release: a git push (or its
    plumbing, send-pack) of any ref to any repository, or any gh call (the
    GitHub release)? A tag made in the work repository is not one: it never
    leaves the work directory unless a push carries it."""
    name = os.path.basename(argv[0])
    return name == "gh" or (name == "git" and _git_verb(argv) in ("push", "send-pack"))


def _git(cwd, *args, env=None, check=True):
    p = subprocess.run(("git",) + args, cwd=cwd, capture_output=True, text=True,
                       env=env)
    if check and p.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), p.stderr))
    return p.stdout.strip()


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


class ReleaseFixture(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-release-test-", dir=_disk_root())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        t = self.tmp
        self.home = os.path.join(t, "home")
        os.makedirs(self.home)
        self.src = os.path.join(t, "src")
        self.public = os.path.join(t, "public.git")
        self.private = os.path.join(t, "private.git")
        self.stubs = os.path.join(t, "stubs")
        self.record = os.path.join(t, "gh-record.jsonl")
        self.fixture_env = dict(
            os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.fixture_env.pop(k, None)

        # The trunk: what a release is cut from.
        os.makedirs(self.src)
        _git(self.src, "init", "-q", "-b", "main", "--template=")
        _write(os.path.join(self.src, "helm", "__init__.py"),
               '__version__ = "%s"\n' % VERSION)
        _write(os.path.join(self.src, "CHANGELOG.md"), CHANGELOG)
        _write(os.path.join(self.src, "README.md"), "# helm\n")
        _write(os.path.join(self.src, "bin", "helm"), BIN_HELM, 0o755)
        _write(os.path.join(self.src, "scripts", "install.sh"), INSTALL_SH, 0o755)
        _write(os.path.join(self.src, "docs", "INTERNAL.md"), "an internal plan\n")
        _write(os.path.join(self.src, "scripts", "release", "omit.txt"),
               "# what the release leaves out\ndocs/INTERNAL.md\n")
        self.commit_trunk("trunk: the fixture tree")

        # The public repository, carrying the previous release.
        _git(t, "init", "-q", "--bare", "-b", "main", "--template=", self.public)
        seed = os.path.join(t, "seed")
        _git(t, "init", "-q", "-b", "main", "--template=", seed)
        _write(os.path.join(seed, "README.md"), "# helm 9.9.0\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "helm 9.9.0", env=dict(
            self.fixture_env, GIT_AUTHOR_NAME=OWNER,
            GIT_AUTHOR_EMAIL="%s@users.noreply.github.com" % OWNER,
            GIT_COMMITTER_NAME=OWNER,
            GIT_COMMITTER_EMAIL="%s@users.noreply.github.com" % OWNER))
        _git(seed, "push", "-q", self.public, "main:refs/heads/main")
        self.public_before = _git(self.public, "rev-parse", "refs/heads/main")
        self.seed = seed

        _git(t, "init", "-q", "--bare", "-b", "main", "--template=", self.private)

        os.makedirs(self.stubs)
        _write(os.path.join(self.stubs, "gh"), STUB_GH, 0o755)
        _write(os.path.join(self.stubs, "gitleaks"), STUB_GITLEAKS, 0o755)
        self.driver = os.path.join(self.stubs, "driver.py")
        _write(self.driver, DRIVER)
        self.needles = os.path.join(t, "needles.txt")
        _write(self.needles, "# fixture needles\n%s\n" % NEEDLE)
        self.never_track = os.path.join(t, "never-track.txt")
        _write(self.never_track, "")
        self.keep_file = os.path.join(self.home, KEEP_FILE)
        self.audit_file = os.path.join(self.home, AUDIT_FILE)
        self.keep([])
        _write(self.audit_file, json.dumps(AUDIT), 0o600)
        self.runs = 0

    def keep(self, rows, path=None):
        """Write a KEEP list (the default one unless `path`), owner-only."""
        _write(path or self.keep_file, json.dumps(rows), 0o600)

    def plant_attribution(self):
        """A seat-attribution line in helm/ production code: a seat named as
        the one who found something."""
        _write(os.path.join(self.src, "helm", "widget.py"), WIDGET)
        return self.commit_trunk("trunk: a widget with an attribution line")

    def commit_trunk(self, message):
        _git(self.src, "add", "-A")
        _git(self.src, "commit", "-q", "-m", message, env=self.fixture_env)
        return _git(self.src, "rev-parse", "HEAD")

    def command(self, *extra, version=VERSION, env=None, work="", stub=None):
        """(argv, env, work dir) of the real command as release() runs it.
        `work` is the --work to pass: "" a fresh directory in the fixture,
        None no --work at all (the command's default), else that path.
        `version` None passes none (the --nightly form).
        `stub`, a (STUB_DO, STUB_ON) pair, runs it under DRIVER."""
        self.runs += 1
        if work == "":
            work = os.path.join(self.tmp, "work-%d" % self.runs)
        env = dict(self.fixture_env,
                   PATH=self.stubs + os.pathsep + os.environ.get("PATH", ""),
                   STUB_GH_RECORD=self.record,
                   HELM_PRIVATE_NEEDLES=self.needles,
                   HELM_NEVER_TRACK_LOCAL=self.never_track,
                   HELM_HOME=os.path.join(self.tmp, "helm-home"),
                   HOME=self.home,
                   **dict(zip(("STUB_DO", "STUB_ON"), stub or ())),
                   **(env or {}))
        argv = [sys.executable] + [self.driver] * bool(stub) + [TOOL] + [
            version] * bool(version) + ["--source", self.src,
            "--public", self.public, "--private", self.private,
            "--gh-repo", "%s/helm" % OWNER] + (
                ["--work", work] if work else []) + list(extra)
        return argv, env, work

    def release(self, *extra, version=VERSION, env=None, work="", umask=None,
                stub=None):
        """Run the real command; -> (rc, combined output, work dir). The work
        dir returned is the one the command said it used, when it said one;
        the other arguments are command()'s."""
        argv, env, work = self.command(*extra, version=version, env=env,
                                       work=work, stub=stub)
        p = subprocess.run(argv, capture_output=True, text=True, env=env,
                           timeout=300, preexec_fn=(lambda: os.umask(umask))
                           if umask is not None else None)
        out = p.stdout + p.stderr
        said = _work_line(out)
        if said and not os.path.realpath(said).startswith(
                os.path.realpath(self.tmp) + os.sep):
            self.addCleanup(shutil.rmtree, said, ignore_errors=True)
        return p.returncode, out, said or work

    def gh_calls(self):
        if not os.path.exists(self.record):
            return []
        with open(self.record, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def assertNothingWritten(self, out):
        self.assertEqual(_git(self.public, "rev-parse", "refs/heads/main"),
                         self.public_before, out)
        self.assertEqual(_git(self.public, "tag", "-l"), "", out)
        self.assertEqual(_git(self.private, "for-each-ref"), "", out)
        self.assertEqual(self.gh_calls(), [], out)

    def assertPublicMovedOnlyBySeed(self, out):
        """The public main is the seed's own commit (the planted race), not
        the candidate, and carries no tag; gh was never called."""
        moved = _git(self.seed, "rev-parse", "HEAD")
        self.assertNotEqual(moved, self.public_before, "the race did not run")
        self.assertEqual(_git(self.public, "rev-parse", "refs/heads/main"), moved, out)
        self.assertEqual(_git(self.public, "tag", "-l"), "", out)
        self.assertEqual(self.gh_calls(), [], out)
        return moved


class DefaultIsADryRunTest(ReleaseFixture):

    def test_no_flag_prints_every_write_and_performs_none(self):
        rc, out, _work = self.release()
        self.assertEqual(rc, 0, out)
        self.assertIn("DRY RUN", out)
        for write in ("%s:refs/heads/release/%s" % ("", VERSION),
                      ":refs/heads/main",
                      "refs/tags/v%s:refs/tags/v%s" % (VERSION, VERSION),
                      "gh release create v%s --repo %s/helm --verify-tag"
                      % (VERSION, OWNER)):
            self.assertIn(write, out)
        self.assertNothingWritten(out)

    def test_the_dry_run_names_the_candidate_it_would_publish(self):
        rc, out, work = self.release()
        self.assertEqual(rc, 0, out)
        cand = _git(os.path.join(work, "release.git"), "rev-parse",
                    "refs/heads/main")
        self.assertIn(cand, out)
        # Built on the public main, so the push is a fast-forward.
        self.assertEqual(_git(os.path.join(work, "release.git"), "rev-parse",
                              cand + "^"), self.public_before)

    def test_the_same_inputs_build_the_same_candidate(self):  # noqa: VACUOUS_ASSERTION — the first sha is pinned to 40 hex before the equality, so two empty answers cannot pass
        """A reviewed dry run and the publish that follows it name ONE object."""
        rc1, out1, w1 = self.release()
        rc2, out2, w2 = self.release()
        self.assertEqual((rc1, rc2), (0, 0), out1 + out2)
        first = _git(os.path.join(w1, "release.git"), "rev-parse", "refs/heads/main")
        self.assertRegex(first, r"^[0-9a-f]{40}$")
        self.assertEqual(first, _git(os.path.join(w2, "release.git"),
                                     "rev-parse", "refs/heads/main"))


class PublishTest(ReleaseFixture):

    def test_publish_backs_up_fast_forwards_tags_and_releases(self):
        rc, out, work = self.release("--publish")
        self.assertEqual(rc, 0, out)
        cand = _git(self.public, "rev-parse", "refs/heads/main")

        # The backup: a mirror of the public repository as it was, fsck clean.
        backup = os.path.join(work, "backup-public.git")
        self.assertEqual(_git(backup, "rev-parse", "refs/heads/main"),
                         self.public_before)
        _git(backup, "fsck", "--no-progress")

        # The public main moved by one fast-forward commit, authored by the
        # owner's identity and carrying the trunk tree minus the omit list.
        self.assertEqual(_git(self.public, "rev-parse", cand + "^"),
                         self.public_before)
        self.assertEqual(_git(self.public, "log", "-1", "--format=%an <%ae>|%cn <%ce>",
                              cand), IDENT + "|" + IDENT)
        paths = _git(self.public, "ls-tree", "-r", "--name-only", cand).split()
        self.assertIn("README.md", paths)
        self.assertIn("bin/helm", paths)
        self.assertNotIn("docs/INTERNAL.md", paths)
        self.assertNotIn("scripts/release/omit.txt", paths)

        # An annotated tag, tagged by the same identity, on that commit.
        self.assertEqual(_git(self.public, "cat-file", "-t", "refs/tags/v" + VERSION),
                         "tag")
        self.assertEqual(_git(self.public, "rev-parse", "v%s^{commit}" % VERSION),
                         cand)
        tag = _git(self.public, "cat-file", "-p", "refs/tags/v" + VERSION)
        self.assertIn("tagger " + IDENT, tag)

        # Staged on the private repository as release/<version>.
        self.assertEqual(_git(self.private, "rev-parse",
                              "refs/heads/release/" + VERSION), cand)

        # One gh call: --verify-tag, and the CHANGELOG section as the notes.
        calls = self.gh_calls()
        self.assertEqual(len(calls), 1, calls)
        argv = calls[0]["argv"]
        self.assertEqual(argv[:6], ["release", "create", "v" + VERSION,
                                    "--repo", OWNER + "/helm", "--verify-tag"])
        self.assertEqual(argv[argv.index("--title") + 1], "helm " + VERSION)
        self.assertEqual(calls[0]["notes"].strip(), SECTION)

    def test_publish_refuses_when_the_public_main_would_not_fast_forward(self):
        # A candidate reviewed on a dry run, staged as a branch of the trunk...
        rc, out, work = self.release()
        self.assertEqual(rc, 0, out)
        _git(self.src, "fetch", "-q", os.path.join(work, "release.git"),
             "+refs/heads/main:refs/heads/staged")
        # ...and then the public main moves on without it.
        _write(os.path.join(self.seed, "HOTFIX.md"), "a public-side commit\n")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-q", "-m", "a public-side commit",
             env=self.fixture_env)
        _git(self.seed, "push", "-q", self.public, "main:refs/heads/main")
        self.public_before = _git(self.public, "rev-parse", "refs/heads/main")

        rc, out, _work = self.release("--publish", "--candidate", "staged")
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn("fast-forward", out)
        self.assertIn(self.public_before[:12], out)
        self.assertNothingWritten(out)

    def test_publish_refuses_before_any_write_when_the_public_main_moved_after_the_read(self):  # noqa: VACUOUS_ASSERTION — assertPublicMovedOnlyBySeed pins the public main to the seed's NEW commit before the absences are asserted
        """The public main is re-read before the first write. A public-side
        commit planted during the gates (by the stub gitleaks, the last gate)
        is seen there: exit 1, and nothing was written anywhere."""
        rc, out, _work = self.release("--publish", env={
            "STUB_MOVE_SEED": self.seed, "STUB_MOVE_PUBLIC": self.public})
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn("moved since it was read", out)
        moved = self.assertPublicMovedOnlyBySeed(out)
        self.assertIn(moved[:12], out)
        self.assertEqual(_git(self.private, "for-each-ref"), "", out)

    def test_publish_stops_with_the_owed_writes_when_the_public_main_moves_after_the_stage_write(self):  # noqa: VACUOUS_ASSERTION — the staged branch is pinned to the candidate sha and the public main to the seed's NEW commit before the absences are asserted
        """The public main is re-read once more between the stage write and
        the main push. A public-side commit that lands the moment the stage
        write does (a post-receive hook on the private repository) stops the
        publish there: exit 3, the staged branch stays, nothing public was
        written, and the three owed writes are printed as commands."""
        _write(os.path.join(self.private, "hooks", "post-receive"),
               POST_RECEIVE % {"seed": self.seed, "public": self.public}, 0o755)
        work = os.path.join(self.tmp, "owed-" + NEEDLE)
        rc, out, _work = self.release("--publish", work=work)
        self.assertEqual(rc, 3, out)
        self.assertIn("STOPPED", out)
        self.assertIn("nothing public was written", out)
        self.assertIn("<needle #1>", out)
        self.assertNotIn(NEEDLE, out)
        cand = _git(os.path.join(work, "release.git"), "rev-parse", "refs/heads/main")
        self.assertEqual(_git(self.private, "rev-parse",
                              "refs/heads/release/" + VERSION), cand, out)
        self.assertPublicMovedOnlyBySeed(out)
        owed = out.split("writes still owed, in order:", 1)[1]
        for write in ("%s:refs/heads/main" % cand,
                      "refs/tags/v%s:refs/tags/v%s" % (VERSION, VERSION),
                      "gh release create v%s" % VERSION):
            self.assertIn(write, owed)
        self.assertNotIn("refs/heads/release/", owed)

    def test_publish_stops_with_the_owed_writes_when_the_public_main_cannot_be_re_read(self):  # noqa: VACUOUS_ASSERTION — the staged branch is pinned to the candidate sha and the public repository to its moved location before the absences are asserted
        """The re-read before the main push can fail outright (the public
        repository is unreachable). That is exit 3 with the owed writes too,
        never a push into the unknown."""
        gone = self.public + ".gone"
        _write(os.path.join(self.private, "hooks", "post-receive"),
               "#!/bin/sh\nmv %s %s\n" % (self.public, gone), 0o755)
        rc, out, work = self.release("--publish")
        self.assertEqual(rc, 3, out)
        self.assertIn("STOPPED", out)
        self.assertIn("unreadable", out)
        cand = _git(os.path.join(work, "release.git"), "rev-parse", "refs/heads/main")
        self.assertEqual(_git(self.private, "rev-parse",
                              "refs/heads/release/" + VERSION), cand, out)
        self.assertEqual(_git(gone, "rev-parse", "refs/heads/main"),
                         self.public_before, out)
        self.assertEqual(_git(gone, "tag", "-l"), "", out)
        self.assertEqual(self.gh_calls(), [], out)
        self.assertIn("%s:refs/heads/main" % cand,
                      out.split("writes still owed, in order:", 1)[1])


class ErrorsLeaveRedactedTest(ReleaseFixture):
    """A call that times out or fails, and any other error, reaches the
    terminal through the one redacting door, as a refusal or a stop, never as
    a traceback. A traceback prints a command's argv and git's stderr as they
    are, and both name paths and remotes that can carry a private needle."""

    def move(self, repo, name):
        """Move the fixture repository `repo` ("public" or "private") to a
        path in the fixture named `name`."""
        path = os.path.join(self.tmp, name)
        os.rename(getattr(self, repo), path)
        setattr(self, repo, path)

    @staticmethod
    def cut_error(cut):
        """A stderr whose last `cut` characters begin inside NEEDLE, five
        characters in: cut before it is redacted, it keeps a piece of the
        needle that no longer matches the needle."""
        return "error: " + NEEDLE + (" is corrupt " + "x" * cut)[:cut - len(NEEDLE) + 5]

    def test_a_git_error_cut_to_its_tail_carries_no_piece_of_a_needle(self):
        """git's stderr is cut to its last 400 characters, and here the cut
        falls inside the needle. Redacted before the cut, the needle is
        printed by number and no piece of it is printed."""
        rc, out, _work = self.release(env={"STUB_ERR": self.cut_error(400)},
                                      stub=("fail", "fsck --no-progress"))
        self.assertEqual(rc, 1, out)
        self.assertEqual(needle_pieces(out), [], out)
        self.assertRegex(out, r"(?m)^REFUSED\s+git fsck --no-progress: .*"
                              r"<needle #1> is corrupt x")
        self.assertNothingWritten(out)

    def test_a_write_error_cut_to_its_tail_carries_no_piece_of_a_needle(self):
        """A failed write's stderr is cut to its last 300 characters, here
        inside the needle: the stop names it by number, and no piece of it."""
        rc, out, _work = self.release("--publish",
                                      env={"STUB_ERR": self.cut_error(300)},
                                      stub=("fail", ":refs/heads/release/"))
        self.assertEqual(rc, 3, out)
        self.assertEqual(needle_pieces(out), [], out)
        self.assertRegex(out, r"(?m)^STOPPED\s+stage failed \(rc 1\): .*"
                              r"<needle #1> is corrupt x")
        self.assertNothingWritten(out)

    def test_a_git_call_that_times_out_is_a_refusal_naming_it_redacted(self):
        """The public path carries the needle, so the timed-out command's
        argv does: it is named with the needle by number."""
        self.move("public", "public-%s.git" % NEEDLE)
        rc, out, _work = self.release(work=os.path.join(self.tmp, "w-" + NEEDLE),
                                      stub=("timeout", "ls-remote --tags"))
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"(?m)^REFUSED\s+git ls-remote --tags \S*public-"
                              r"<needle #1>\.git refs/tags/v%s: timed out after "
                              r"600 s$" % re.escape(VERSION))
        self.assertIn("nothing was pushed, tagged on a remote or released", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_reading_the_blobs_that_times_out_is_a_refusal(self):
        """`git cat-file --batch` runs in the work repository, whose path
        carries the needle."""
        rc, out, _work = self.release(work=os.path.join(self.tmp, "w-" + NEEDLE),
                                      stub=("timeout", "cat-file --batch"))
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"(?m)^REFUSED\s+git cat-file --batch: timed out "
                              r"after 600 s$")
        self.assertNotIn("Traceback", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_git_failing_to_write_the_index_is_a_refusal_carrying_its_stderr_redacted(self):
        """git's own stderr names the index it could not lock, in a temporary
        directory whose path carries the needle. It reaches the terminal in
        the refusal, redacted, and never raw."""
        tmpdir = os.path.join(self.tmp, "tmp-" + NEEDLE)
        os.mkdir(tmpdir)
        rc, out, _work = self.release(env={"TMPDIR": tmpdir},
                                      stub=("lock", "update-index"))
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"(?m)^REFUSED\s+git update-index -z --index-info: ")
        self.assertRegex(out, r"tmp-<needle #1>/[^/\s']+/index\.lock")
        self.assertIn("nothing was pushed, tagged on a remote or released", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_a_path_git_will_not_index_is_refused_without_gits_stderr(self):
        """A trunk path git will not put in an index (one under a `.git`
        directory, which only plumbing commits): update-index skips it, names
        it on stderr and exits 0. The tree gate refuses the candidate that
        lacks it and names it redacted; git's own line is not printed."""
        def mktree(text):
            return subprocess.run(("git", "mktree"), cwd=self.src, input=text,
                                  capture_output=True, text=True,
                                  check=True).stdout.strip()
        blob = _git(self.src, "hash-object", "-w",
                    os.path.join(self.src, "README.md"))
        inner = mktree("100644 blob %s\t%s\n" % (blob, NEEDLE))
        tree = mktree(_git(self.src, "ls-tree", "HEAD")
                      + "\n040000 tree %s\t.git\n" % inner)
        commit = _git(self.src, "commit-tree", tree, "-p", "HEAD", "-m",
                      "trunk: a path git will not index", env=self.fixture_env)
        _git(self.src, "update-ref", "refs/heads/main", commit)
        rc, out, _work = self.release()
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"(?m)^REFUSED\s+the candidate tree is not the "
                              r"trunk tree minus the omit list: extra \[\], "
                              r"missing \['\.git/<needle #1>'\]")
        self.assertNotIn("Ignoring path", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_a_publish_write_that_times_out_stops_with_that_write_owed(self):
        """A write that timed out may or may not have reached its remote. The
        publish stops there, exit 3, and the timed-out write is the first of
        the writes owed, printed redacted like the rest."""
        self.move("private", "private-%s.git" % NEEDLE)
        work = os.path.join(self.tmp, "work-stage")
        rc, out, _work = self.release("--publish", work=work,
                                      stub=("timeout", ":refs/heads/release/"))
        self.assertEqual(rc, 3, out)
        self.assertRegex(out, r"(?m)^STOPPED\s+stage timed out after 600 s")
        cand = _git(os.path.join(work, "release.git"), "rev-parse", "refs/heads/main")
        owed = out.split("writes still owed, in order:\n", 1)[1].splitlines()
        for want in (" push ", "private-<needle #1>.git",
                     "%s:refs/heads/release/%s" % (cand, VERSION)):
            self.assertIn(want, owed[0])
        self.assertIn("%s:refs/heads/main" % cand, owed[1])
        self.assertIn("refs/tags/v%s:refs/tags/v%s" % (VERSION, VERSION), owed[2])
        self.assertIn("gh release create v%s" % VERSION, owed[3])
        self.assertNotIn("Traceback", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_any_other_error_before_a_write_is_a_redacted_refusal(self):
        """An error no refusal was written for (the installer timing out in
        the battery, its argv naming the work directory) still leaves through
        the redacting door: exit 1, and nothing was pushed."""
        rc, out, _work = self.release(work=os.path.join(self.tmp, "w-" + NEEDLE),
                                      stub=("timeout", "install-candidate"))
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"(?m)^REFUSED\s+TimeoutExpired: .*w-<needle #1>/"
                              r"install-candidate.* timed out after 300 seconds$")
        self.assertIn("nothing was pushed, tagged on a remote or released", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_any_other_error_after_a_write_began_is_exit_3(self):
        """The same error in the battery on the published repository comes
        after the writes began, so it is exit 3: they may be partial."""
        work = os.path.join(self.tmp, "w-" + NEEDLE)
        rc, out, _work = self.release("--publish", work=work,
                                      stub=("timeout", "install-public"))
        self.assertEqual(rc, 3, out)
        self.assertRegex(out, r"(?m)^REFUSED\s+TimeoutExpired: .*w-<needle #1>/"
                              r"install-public.* timed out after 300 seconds$")
        self.assertIn("may be partial", out)
        self.assertNotIn("nothing was pushed", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn(NEEDLE, out)
        cand = _git(os.path.join(work, "release.git"), "rev-parse", "refs/heads/main")
        self.assertEqual(len(cand), 40, out)
        self.assertEqual(_git(self.public, "rev-parse", "refs/heads/main"), cand, out)

    def test_a_refusal_the_terminal_cannot_take_leaves_no_raw_text(self):
        """stdout closed under the REFUSED line (a `| head` that had read
        enough): printing it raises, and the interpreter's own printer then
        writes the exception that was being handled. A refusal names the
        command that failed as it is, and here the public path in it carries
        the needle; the door was the only thing hiding it. That printer goes
        through the door too: the needle reaches stderr by number only."""
        self.move("public", "public-%s.git" % NEEDLE)
        go = os.path.join(self.tmp, "go")
        argv, env, _work = self.command(
            env={"STUB_ERR": "fatal: the mirror is refused", "STUB_GO": go},
            stub=("fail", "clone -q --mirror"))
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env)
        last = "ok       tag v%s is not on the public repository" % VERSION
        for line in p.stdout:
            if line.rstrip("\n") == last:
                break
        else:
            self.fail("the command never said %r" % last)
        p.stdout.close()
        open(go, "w").close()
        err = p.stderr.read()
        rc = p.wait(timeout=300)
        self.assertNotIn(NEEDLE, err)
        self.assertIn("git clone -q --mirror --template= ", err)
        self.assertIn("public-<needle #1>.git", err)
        self.assertNotEqual(rc, 0, err)
        self.assertNothingWritten(err)


class RefusalTest(ReleaseFixture):

    def test_a_version_the_changelog_has_no_section_for_is_refused(self):
        _write(os.path.join(self.src, "helm", "__init__.py"),
               '__version__ = "9.9.2"\n')
        self.commit_trunk("trunk: 9.9.2 without its changelog section")
        rc, out, _work = self.release("--publish", version="9.9.2")
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn("CHANGELOG.md", out)
        self.assertIn("no section for 9.9.2", out)
        self.assertNothingWritten(out)

    def test_a_version_the_tree_does_not_declare_is_refused(self):
        _write(os.path.join(self.src, "helm", "__init__.py"),
               '__version__ = "9.9.0"\n')
        self.commit_trunk("trunk: the version was not bumped")
        rc, out, _work = self.release("--publish")
        self.assertEqual(rc, 1, out)
        self.assertIn("__version__", out)
        self.assertNothingWritten(out)

    def test_a_forbidden_filename_is_refused_by_name(self):
        _write(os.path.join(self.src, "config", ".env"), "TOKEN=placeholder\n")
        self.commit_trunk("trunk: an environment file")
        rc, out, _work = self.release("--publish")
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn("config/.env", out)
        self.assertNothingWritten(out)

    def test_an_attribution_line_in_the_release_message_is_refused_by_name(self):
        msg = os.path.join(self.tmp, "message.txt")
        line = "Co-Authored-By: Claude <noreply@anthropic.com>"
        _write(msg, "helm %s\n\nThe widget counts.\n\n%s\n" % (VERSION, line))
        rc, out, _work = self.release("--publish", "--message-file", msg)
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn(line, out)
        self.assertNothingWritten(out)

    def test_an_attribution_line_in_the_release_notes_is_refused(self):
        with open(os.path.join(self.src, "CHANGELOG.md"), encoding="utf-8") as f:
            text = f.read()
        line = "Generated with [Claude Code](https://example.com)"
        _write(os.path.join(self.src, "CHANGELOG.md"),
               text.replace("- the counter resets", "- the counter resets\n\n" + line))
        self.commit_trunk("trunk: a footer in the changelog")
        rc, out, _work = self.release("--publish")
        self.assertEqual(rc, 1, out)
        self.assertIn("release notes", out)
        self.assertIn(line, out)
        self.assertNothingWritten(out)

    def test_a_private_needle_in_the_tree_is_refused_without_printing_it(self):
        _write(os.path.join(self.src, "docs", "GUIDE.md"),
               "ask %s for access\n" % NEEDLE)
        self.commit_trunk("trunk: a guide")
        rc, out, _work = self.release("--publish")
        self.assertEqual(rc, 1, out)
        self.assertIn("docs/GUIDE.md", out)
        self.assertIn("needle #1", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_a_private_needle_in_a_path_is_refused_without_printing_it(self):
        """A path hit is reported like a content hit, by number and never by
        value; here the path IS the value, so the path is printed redacted."""
        _write(os.path.join(self.src, "docs", "%s.md" % NEEDLE), "a guide\n")
        self.commit_trunk("trunk: a guide named after a needle")
        rc, out, _work = self.release("--publish")
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn("needle #1", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_a_private_needle_in_a_binary_blob_is_refused_without_printing_it(self):
        """A needle inside a blob with a NUL in its first bytes (a picture, a
        dump, an archive) is still a needle in a file: the sweep reads every
        blob, and reports the hit by number."""
        with open(os.path.join(self.src, "docs", "blob.bin"), "wb") as f:
            f.write(b"\x00PNG\x00" + NEEDLE.encode() + b"\x00")
        self.commit_trunk("trunk: a binary fixture")
        rc, out, _work = self.release("--publish")
        self.assertEqual(rc, 1, out)
        self.assertIn("docs/blob.bin", out)
        self.assertIn("needle #1", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_an_authoring_line_carrying_a_needle_is_not_printed_by_value(self):
        """The authoring-line refusal quotes the line; a needle in it is
        printed by number like everywhere else."""
        msg = os.path.join(self.tmp, "message.txt")
        _write(msg, "helm %s\n\nThe widget counts.\n\nCo-Authored-By: %s "
               "<noreply@anthropic.com>\n" % (VERSION, NEEDLE))
        rc, out, _work = self.release("--publish", "--message-file", msg)
        self.assertEqual(rc, 1, out)
        self.assertIn("AI authoring line", out)
        self.assertIn("Co-Authored-By: <needle #1>", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_a_gitleaks_finding_in_a_needle_named_path_is_not_printed_by_value(self):
        """gitleaks names the file it flagged; that path is printed redacted."""
        _write(os.path.join(self.stubs, "gitleaks"), STUB_GITLEAKS_FINDING, 0o755)
        _write(os.path.join(self.src, "docs", "keys.md"), "k\n")
        self.commit_trunk("trunk: a keys file")
        rc, out, _work = self.release("--publish", env={
            "STUB_LEAK_FILE": "docs/%s-keys.md" % NEEDLE})
        self.assertEqual(rc, 1, out)
        self.assertIn("gitleaks: findings outside the reviewed set", out)
        self.assertIn("docs/<needle #1>-keys.md (generic-api-key)", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_a_private_repository_that_is_the_public_one_is_refused(self):
        """The stage write is a private branch. --private resolving to the
        public repository would push release/<version> onto it: refused
        before any write, in every mode."""
        rc, out, _work = self.release("--private", self.public)
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn("the private repository", out)
        self.assertIn("is the public repository", out)
        self.assertNothingWritten(out)
        rc, out, _work = self.release("--private", self.public, "--publish")
        self.assertEqual(rc, 1, out)
        self.assertIn("is the public repository", out)
        self.assertNothingWritten(out)

    def test_an_omit_entry_that_matches_nothing_is_refused(self):
        _write(os.path.join(self.src, "scripts", "release", "omit.txt"),
               "docs/INTERNAL.md\ndocs/GONE.md\n")
        self.commit_trunk("trunk: a stale omit entry")
        rc, out, _work = self.release()
        self.assertEqual(rc, 1, out)
        self.assertIn("docs/GONE.md", out)
        self.assertNothingWritten(out)

    def test_no_loaded_needles_refuses_dry_run_and_publish_before_writes(self):  # noqa: VACUOUS_ASSERTION — the mode table is a non-empty literal and each case pins rc 1 and the refusal before the absences
        _write(self.needles, "# none\n")
        for mode in ((), ("--publish",)):
            with self.subTest(mode=mode):
                rc, out, _work = self.release(*mode)
                self.assertEqual(rc, 1, out)
                self.assertIn("no private needles loaded", out)
                self.assertNotIn("passed every gate", out)
                self.assertNothingWritten(out)


class WorkDirectoryTest(ReleaseFixture):
    """The work directory keeps the backup of the public repository, so it
    must survive a reboot: it is on disk, by default under the releases
    directory in the home, and never in memory or inside the checkout."""

    def test_a_work_directory_on_a_memory_filesystem_is_refused_naming_the_mount(self):  # noqa: VACUOUS_ASSERTION — rc 1 and the named mount are pinned before the absences are asserted
        point = _memory_mount()
        if point is None:
            self.skipTest("this host mounts no writable memory filesystem")
        fstype = _filesystem(point)[1]
        work = os.path.join(point, "helm-release-arm-%d-%d" % (os.getpid(), id(self)))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        rc, out, _work = self.release(work=work)
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn("%s mounted at %s" % (fstype, point), out)
        self.assertFalse(os.path.exists(work), out)
        self.assertNothingWritten(out)

    def test_the_default_work_directory_lands_under_the_releases_directory(self):
        """No --work: a new directory per run under ~/.helm/releases/<version>,
        so a dry run and the publish after it each keep their own backup."""
        releases = os.path.join(self.home, ".helm", "releases", VERSION)
        rc, out, work = self.release(work=None)
        self.assertEqual(rc, 0, out)
        self.assertTrue(work.startswith(releases + os.sep), out)
        backup = os.path.join(work, "backup-public.git")
        self.assertEqual(_git(backup, "rev-parse", "refs/heads/main"),
                         self.public_before, out)
        rc, out, again = self.release(work=None)
        self.assertEqual(rc, 0, out)
        self.assertTrue(again.startswith(releases + os.sep), out)
        self.assertNotEqual(again, work, out)
        self.assertTrue(os.path.isdir(backup), out)

    def test_a_version_cannot_escape_the_default_releases_directory(self):  # noqa: VACUOUS_ASSERTION — the version table is a non-empty literal and each case pins the typed refusal before the escaped path's absence
        versions = (("../../../escaped", os.path.join(self.tmp, "escaped")),
                    (os.path.join(self.tmp, "absolute-version"),
                     os.path.join(self.tmp, "absolute-version")))
        for version, escaped in versions:
            with self.subTest(version=version):
                _write(os.path.join(self.src, "helm", "__init__.py"),
                       '__version__ = %r\n' % version)
                _write(os.path.join(self.src, "CHANGELOG.md"),
                       "# Changelog\n\n## %s\n\nUnsafe version fixture.\n" % version)
                self.commit_trunk("trunk: unsafe version fixture")
                rc, out, _work = self.release(version=version, work=None)
                self.assertEqual(rc, 1, out)
                self.assertIn("safe path component", out)
                self.assertFalse(os.path.exists(escaped), out)
                self.assertNothingWritten(out)

    def test_a_version_symlink_cannot_escape_the_default_releases_directory(self):  # noqa: VACUOUS_ASSERTION — the real symlink target and typed refusal are pinned before its retained emptiness
        releases = os.path.join(self.home, ".helm", "releases")
        outside = os.path.join(self.tmp, "outside-releases")
        os.makedirs(releases)
        os.mkdir(outside)
        os.symlink(outside, os.path.join(releases, VERSION))
        rc, out, _work = self.release(work=None)
        self.assertEqual(rc, 1, out)
        self.assertIn("escapes the releases directory", out)
        self.assertEqual(os.listdir(outside), [])
        self.assertNothingWritten(out)

    def test_a_work_directory_on_disk_is_accepted_and_its_filesystem_named(self):  # noqa: VACUOUS_ASSERTION — the work line must carry the filesystem and the backup directory must exist
        rc, out, work = self.release()
        self.assertEqual(rc, 0, out)
        point, fstype = _filesystem(work)
        self.assertNotIn(fstype, MEMORY_FS)
        line = next(ln for ln in out.splitlines() if ln.startswith("work"))
        self.assertIn("%s at %s" % (fstype, point), line)
        self.assertTrue(os.path.isdir(os.path.join(work, "backup-public.git")), out)

    def test_a_work_directory_inside_the_checkout_is_refused(self):  # noqa: VACUOUS_ASSERTION — rc 1 and the refusal text are pinned before the absences are asserted
        """The work directory holds the reports for the owner, which name
        private things; inside the checkout they are one `git add` away
        from the tree."""
        work = os.path.join(self.src, "release-work")
        rc, out, _work = self.release(work=work)
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn("inside the checkout", out)
        self.assertFalse(os.path.exists(work), out)
        self.assertNothingWritten(out)

    def test_an_existing_file_cannot_be_the_work_directory(self):
        work = os.path.join(self.tmp, "existing-file")
        _write(work, "not a directory\n")
        rc, out, _work = self.release(work=work)
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn(work, out)
        self.assertIn("is not a directory", out)
        self.assertNotIn("Traceback", out)
        self.assertNothingWritten(out)

    def test_an_unreadable_work_directory_is_a_typed_refusal(self):
        work = os.path.join(self.tmp, "unreadable-work")
        os.mkdir(work, 0o000)
        try:
            rc, out, _work = self.release(work=work)
        finally:
            os.chmod(work, 0o700)
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn(work, out)
        self.assertIn("cannot be prepared", out)
        self.assertNotIn("Traceback", out)
        self.assertNothingWritten(out)

    def test_an_explicit_work_root_is_owner_only_before_private_artifacts_exist(self):
        message = os.path.join(self.tmp, "private-message.txt")
        _write(message, "helm %s\n\n%s\n" % (VERSION, NEEDLE), 0o600)
        work = os.path.join(self.tmp, "explicit-work")
        rc, out, _work = self.release("--message-file", message, work=work,
                                      umask=0o022)
        self.assertEqual(rc, 1, out)
        self.assertEqual(os.stat(work).st_mode & 0o777, 0o700)
        copied = os.path.join(work, "message.txt")
        self.assertEqual(os.stat(copied).st_mode & 0o777, 0o600)
        with open(copied, encoding="utf-8") as f:
            self.assertIn(NEEDLE, f.read())
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_private_inputs_and_work_inside_a_linked_sibling_worktree_are_refused(self):  # noqa: VACUOUS_ASSERTION — the path table is a non-empty literal and each real sibling path pins rc 1 and its containment refusal
        sibling = os.path.join(self.tmp, "sibling")
        _git(self.src, "worktree", "add", "-q", "-b", "fixture-sibling", sibling)
        keep = os.path.join(sibling, "private", "keep.json")
        audit = os.path.join(sibling, "private", "audit.json")
        self.keep([], keep)
        _write(audit, json.dumps(AUDIT), 0o600)
        cases = (
            (("--keep-file", keep), "", keep),
            (("--audit-file", audit), "", audit),
            ((), os.path.join(sibling, "release-work"),
             os.path.join(sibling, "release-work")),
        )
        for extra, work, refused in cases:
            with self.subTest(path=refused):
                rc, out, _work = self.release(*extra, work=work)
                self.assertEqual(rc, 1, out)
                self.assertIn("inside the checkout", out)
                self.assertIn(sibling, out)
                self.assertNothingWritten(out)


# Mount tables for the filesystem lookup. The mount points sit under a
# directory no host has, so the real path of each probe is the probe itself.
MOUNTINFO = textwrap.dedent("""\
    22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw
    23 22 0:21 / /zz-host/ram rw,nosuid shared:2 - tmpfs tmpfs rw
    24 23 8:4 / /zz-host/ram/disk rw shared:3 - xfs /dev/sda4 rw
    25 22 8:3 / /zz-host/ramdisk rw shared:4 - ext4 /dev/sda3 rw
    26 22 0:22 / /zz-host/with\\040space rw shared:5 - ramfs ramfs rw
    27 22 8:5 / /zz-host/stack rw shared:6 - ext4 /dev/sda5 rw
    28 27 0:23 / /zz-host/stack rw shared:7 - tmpfs tmpfs rw
    """)


class FilesystemLookupTest(unittest.TestCase):

    def setUp(self):
        self.tool = _tool()
        self.tmp = tempfile.mkdtemp(prefix="helm-release-mounts-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def table(self, text):
        path = os.path.join(self.tmp, "mountinfo-%d" % len(os.listdir(self.tmp)))
        _write(path, text)
        return path

    def test_the_longest_mount_point_over_the_path_holds_it(self):  # noqa: VACUOUS_ASSERTION — the probe table is a non-empty literal, so every subTest runs
        table = self.table(MOUNTINFO)
        for probe, want in (
                ("/zz-host/ram/work", ("/zz-host/ram", "tmpfs")),
                ("/zz-host/ram/disk/work", ("/zz-host/ram/disk", "xfs")),
                ("/zz-host/ramdisk/work", ("/zz-host/ramdisk", "ext4")),
                ("/zz-host/ramfiles/work", ("/", "ext4")),
                ("/zz-host/with space/work", ("/zz-host/with space", "ramfs")),
                ("/zz-host/stack/work", ("/zz-host/stack", "tmpfs")),
                ("/zz-host/elsewhere/work", ("/", "ext4"))):
            with self.subTest(probe=probe):
                self.assertEqual(self.tool.mount_of(probe, table), want)

    def test_a_path_on_a_memory_filesystem_is_refused_naming_the_mount(self):
        table = self.table(MOUNTINFO)
        for probe, named in (("/zz-host/ram/work", "tmpfs mounted at /zz-host/ram"),
                             ("/zz-host/with space/w", "ramfs mounted at /zz-host/with space"),
                             ("/zz-host/stack/w", "tmpfs mounted at /zz-host/stack")):
            with self.subTest(probe=probe):
                with self.assertRaises(self.tool.Refusal) as cm:
                    self.tool.on_disk(probe, table)
                self.assertIn(named, str(cm.exception))
        self.assertEqual(self.tool.on_disk("/zz-host/ram/disk/w", table),
                         ("/zz-host/ram/disk", "xfs"))

    def test_a_filesystem_that_cannot_be_determined_is_refused_with_the_reason(self):  # noqa: VACUOUS_ASSERTION — the case table is a non-empty literal, so every subTest runs
        for text, reason in (
                (MOUNTINFO.split("\n", 1)[1], "no mount"),
                ("22 1 8:2 / / rw - ext4 /dev/sda2 rw\nnot a mount line\n",
                 "cannot parse"),
                (None, "cannot read")):
            table = self.table(text) if text is not None else os.path.join(
                self.tmp, "absent")
            with self.subTest(reason=reason):
                with self.assertRaises(self.tool.Refusal) as cm:
                    self.tool.on_disk("/zz-host/elsewhere/work", table)
                self.assertIn(reason, str(cm.exception))
                self.assertIn("/zz-host/elsewhere/work", str(cm.exception))


class SeatAttributionGateTest(ReleaseFixture):
    """A line in helm/ production code that names a seat as the one who found,
    caught or reviewed something ships only when the KEEP list names it. The
    KEEP list and the seat pattern name private things, so both live outside
    the tree, readable by their owner only; without them the gate does not
    run, and a gate that did not run is a failed gate."""

    def test_a_seat_attribution_line_off_the_keep_list_is_refused_by_place(self):
        self.plant_attribution()
        rc, out, work = self.release("--publish")
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn("seat attribution", out)
        self.assertIn("helm/widget.py:2", out)
        self.assertNotIn("fixture-seat-7", out)
        self.assertNothingWritten(out)
        with open(os.path.join(work, "reports", "arm_attr.txt"), encoding="utf-8") as f:
            report = f.read()
        self.assertIn("offending 1", report)
        self.assertIn("OFFENDING helm/widget.py:2: " + SEAT_LINE, report)

    def test_a_keep_listed_attribution_line_passes(self):
        self.plant_attribution()
        self.keep([{"file": "helm/widget.py", "text": SEAT_LINE,
                    "reason": "names the fixture's own seat"}])
        rc, out, work = self.release()
        self.assertEqual(rc, 0, out)
        self.assertIn("seat attribution: 1 line in helm/, on the KEEP list", out)
        with open(os.path.join(work, "reports", "arm_attr.txt"), encoding="utf-8") as f:
            self.assertIn("definition-B lines=1  (KEEP-listed 1, offending 0)", f.read())

    def test_a_stale_keep_entry_is_refused(self):
        """An entry that matches no line at the candidate is a list that went
        stale; it must not pass silently."""
        self.keep([{"file": "helm/gone.py", "text": "# fixture-seat-9 caught it",
                    "reason": "a line that is no longer there"}])
        rc, out, work = self.release()
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn("stale", out)
        self.assertIn("helm/gone.py", out)
        with open(os.path.join(work, "reports", "arm_attr.txt"), encoding="utf-8") as f:
            self.assertIn("STALE KEEP entry", f.read())

    def test_an_absent_keep_list_fails_the_gate_in_every_mode(self):  # noqa: VACUOUS_ASSERTION — the mode table is a non-empty literal and each case pins rc 1 and the refusal before the absences
        os.unlink(self.keep_file)
        for mode in ((), ("--publish",)):
            with self.subTest(mode=mode):
                rc, out, _work = self.release(*mode)
                self.assertEqual(rc, 1, out)
                self.assertIn("REFUSED", out)
                self.assertIn("the seat-attribution gate did not run", out)
                self.assertIn("%s is absent" % self.keep_file, out)
                self.assertNothingWritten(out)

    def test_a_keep_list_anyone_else_can_reach_is_refused_unread(self):  # noqa: VACUOUS_ASSERTION — the mode table is a non-empty literal and each case pins rc 1 and the named mode before the absences
        secret = "fixture-private-token-9"
        self.keep([{"file": "helm/widget.py", "text": SEAT_LINE, "reason": secret}])
        for mode, shown in ((0o644, "-rw-r--r--"), (0o640, "-rw-r-----"),
                            (0o620, "-rw--w----"), (0o604, "-rw----r--")):
            with self.subTest(mode=shown):
                os.chmod(self.keep_file, mode)
                rc, out, _work = self.release()
                self.assertEqual(rc, 1, out)
                self.assertIn("the seat-attribution gate did not run", out)
                self.assertIn("%s is %s" % (self.keep_file, shown), out)
                self.assertIn("chmod 600", out)
                self.assertNotIn(secret, out)
                self.assertNothingWritten(out)

    def test_an_explicit_keep_file_is_read_and_one_inside_the_checkout_is_refused(self):
        self.plant_attribution()
        rows = [{"file": "helm/widget.py", "text": SEAT_LINE, "reason": "fixture"}]
        elsewhere = os.path.join(self.tmp, "elsewhere", "keep.json")
        self.keep(rows, elsewhere)
        rc, out, _work = self.release("--keep-file", elsewhere)
        self.assertEqual(rc, 0, out)
        self.assertIn(elsewhere, out)
        inside = os.path.join(self.src, "keep.json")
        self.keep(rows, inside)
        rc, out, _work = self.release("--keep-file", inside)
        self.assertEqual(rc, 1, out)
        self.assertIn("the seat-attribution gate did not run", out)
        self.assertIn("inside the checkout", out)
        self.assertNothingWritten(out)

    def test_private_paths_carrying_a_loaded_needle_are_redacted_at_the_output_door(self):  # noqa: VACUOUS_ASSERTION — rc 0 and numbered terminal redaction are pinned before every known report is checked for absence
        private = os.path.join(self.tmp, "external-" + NEEDLE)
        keep = os.path.join(private, "keep.json")
        audit = os.path.join(private, "audit.json")
        work = os.path.join(self.tmp, "work-" + NEEDLE)
        self.keep([], keep)
        _write(audit, json.dumps(AUDIT), 0o600)
        rc, out, _work = self.release("--keep-file", keep, "--audit-file", audit,
                                      work=work)
        self.assertEqual(rc, 0, out)
        self.assertIn("<needle #1>", out)
        self.assertNotIn(NEEDLE, out)
        for name in os.listdir(os.path.join(work, "reports")):
            with self.subTest(report=name):
                with open(os.path.join(work, "reports", name), encoding="utf-8") as f:
                    self.assertNotIn(NEEDLE, f.read())

    def test_a_version_carrying_a_loaded_needle_is_redacted_in_the_mode_banner(self):
        rc, out, _work = self.release(version=NEEDLE)
        self.assertEqual(rc, 1, out)
        self.assertIn("release <needle #1>", out)
        self.assertNotIn(NEEDLE, out)
        self.assertNothingWritten(out)

    def test_an_unknown_private_pattern_key_is_reported_by_count_and_schema_only(self):
        unknown = "unknown-" + NEEDLE
        _write(self.audit_file, json.dumps(dict(AUDIT, **{unknown: "arbitrary"})),
               0o600)
        rc, out, _work = self.release()
        self.assertEqual(rc, 1, out)
        self.assertIn("1 unknown key", out)
        self.assertIn("expected seat, private_token, owner, literals", out)
        self.assertNotIn(unknown, out)
        self.assertNotIn("<needle #1>", out)
        self.assertNothingWritten(out)

    def test_private_patterns_that_are_absent_loose_or_malformed_fail_the_gate(self):  # noqa: VACUOUS_ASSERTION — the case table is a non-empty literal and each case pins rc 1, the reason and a NOT RUN report
        """The seat pattern comes from the private patterns file; the gate
        cannot run without it, and neither can the private report classes."""
        cases = (
            ("absent", None, None),
            ("-rw-rw-r--", json.dumps(AUDIT), 0o664),
            ("not valid JSON", "{", 0o600),
            ("missing seat", json.dumps({k: v for k, v in AUDIT.items() if k != "seat"}), 0o600),
            ("not a regular expression", json.dumps(dict(AUDIT, seat="(")), 0o600),
        )
        for said, text, mode in cases:
            with self.subTest(case=said):
                if text is None:
                    os.unlink(self.audit_file)
                else:
                    _write(self.audit_file, text, mode)
                rc, out, work = self.release()
                self.assertEqual(rc, 1, out)
                self.assertIn("the seat-attribution gate did not run", out)
                self.assertIn(self.audit_file, out)
                self.assertIn(said, out)
                self.assertNothingWritten(out)
                with open(os.path.join(work, "reports", "world_audit.txt"),
                          encoding="utf-8") as f:
                    self.assertIn("NOT RUN", f.read())


class OwnerReadReportsTest(ReleaseFixture):
    """What the gates do not refuse, the owner reads before a public release:
    private tokens, seat names, addresses, task cites, dates and the owner's
    voice. The command writes those reports under the work directory, owner
    only, and never into the repository; the terminal gets counts, never a
    private value."""

    def plant_world(self):
        self.plant_attribution()
        self.keep([{"file": "helm/widget.py", "text": SEAT_LINE,
                    "reason": "names the fixture's own seat"}])
        _write(os.path.join(self.src, "docs", "NOTES.md"), NOTES)
        self.commit_trunk("trunk: notes the owner reads")

    def report(self, work, name):
        path = os.path.join(work, "reports", name)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, path)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_the_reports_are_written_under_the_work_directory_never_the_repository(self):  # noqa: VACUOUS_ASSERTION — every report row is pinned by value before the absences on the terminal and in the checkout
        self.plant_world()
        rc, out, work = self.release()
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.stat(os.path.join(work, "reports")).st_mode & 0o777, 0o700)
        audit = self.report(work, "world_audit.txt")
        for row in (r"private-token\s+1 / 1\s+docs/NOTES\.md\(1\)",
                    r"task-cite\s+1 / 1\s+docs/NOTES\.md\(1\)",
                    r"date-prose\s+3 / 2\s+",
                    r"owner-voice\s+2 / 1\s+docs/NOTES\.md\(2\)",
                    r"seat-token\s+2 / 1\s+helm/widget\.py\(2\)",
                    r"email-nonplaceholder\s+1 / 1\s+docs/NOTES\.md\(1\)"):
            self.assertRegex(audit, re.compile(r"^\s+" + row, re.M))
        self.assertIn("private-token breakdown: fixture-private-token-1=1", audit)
        self.assertIn("email domains (non-placeholder): fixture-mail.zz=1", audit)
        seats = self.report(work, "seat_attribution.txt")
        self.assertRegex(seats, r"A \(seat token on the line\):\s+lines=2\s+files=1")
        self.assertRegex(seats, r"B \(seat token \+ attribution verb\):\s+lines=1\s+files=1")
        self.assertIn("helm/widget.py:2: KEEP", seats)
        content = self.report(work, "content.txt")
        for row in (r"seat:fixture-seat-7\s+total=1\s+files=1",
                    r"seat:fixture-seat-8\s+total=1\s+files=1",
                    r"literal:fixture-host\s+total=1\s+files=1\s+top: docs/NOTES\.md\(1\)",
                    r"email:non-placeholder\s+total=1\s+files=1"):
            self.assertRegex(content, row)
        self.assertIn("definition-B lines=1", self.report(work, "arm_attr.txt"))
        # The terminal names the reports and counts; the values stay in them.
        self.assertIn(os.path.join(work, "reports"), out)
        for value in ("fixture-private-token-1", "Fixtureowner", "fixture-host.lan",
                      "fixture-mail.zz", "fixture-seat-7"):
            self.assertNotIn(value, out)
        # Nothing landed in the checkout.
        self.assertEqual(_git(self.src, "status", "--porcelain", "--ignored"), "")

    def test_a_private_needle_is_reported_by_number_never_by_value(self):  # noqa: VACUOUS_ASSERTION — the needle's row is pinned by number and path before its value's absence is asserted
        """A report names the files that carry each hit, so a needle in a
        file's name reaches the report by that path: it is written redacted."""
        _write(os.path.join(self.src, "docs", "%s-guide.md" % NEEDLE),
               "ask %s for access\n" % NEEDLE)
        self.commit_trunk("trunk: a guide named after a needle")
        rc, out, work = self.release(umask=0o022)
        self.assertEqual(rc, 1, out)
        self.assertEqual(os.stat(os.path.join(work, "reports")).st_mode & 0o777, 0o700)
        content = self.report(work, "content.txt")
        self.assertRegex(content, r"needle#1\s+total=1\s+files=1\s+"
                                  r"top: docs/<needle #1>-guide\.md\(1\)")
        for name in os.listdir(os.path.join(work, "reports")):
            with self.subTest(report=name):
                self.assertNotIn(NEEDLE, self.report(work, name))
        self.assertNotIn(NEEDLE, out)


NIGHTLY_GREEN = re.compile(
    r"^NIGHTLY\s+GREEN trunk=([0-9a-f]{40}) candidate=([0-9a-f]{40})$", re.M)
NIGHTLY_RED = re.compile(r"^NIGHTLY\s+RED step=(\S+) trunk=([0-9a-f]{40})$", re.M)


class NightlyTest(ReleaseFixture):
    """--nightly: the dry run the nightly job (helm/releasenightly.py) runs on
    trunk as it stands, every night, to show a release can still be cut."""

    def setUp(self):
        super().setUp()
        self.spy = os.path.join(self.stubs, "spy.py")
        _write(self.spy, SPY)

    def nightly(self, *extra, **kw):
        return self.release("--nightly", *extra, version=None, **kw)

    def spied(self, *extra, version=None):
        """(rc, output, [argv of every child the command started])."""
        record = os.path.join(self.tmp, "spy-%d.jsonl" % (self.runs + 1))
        argv, env, _work = self.command(*extra, version=version,
                                        env={"SPY_RECORD": record})
        p = subprocess.run([argv[0], self.spy] + argv[1:], capture_output=True,
                           text=True, env=env, timeout=300)
        calls = []
        if os.path.exists(record):
            with open(record, encoding="utf-8") as f:
                calls = [json.loads(line) for line in f if line.strip()]
        return p.returncode, p.stdout + p.stderr, calls

    def test_the_nightly_makes_no_outward_call(self):
        """Every child the nightly starts is seen, and none of them pushes a
        ref anywhere or calls gh: no tag, no push, no GitHub release."""
        rc, out, calls = self.spied("--nightly")
        self.assertEqual(rc, 0, out)
        m = NIGHTLY_GREEN.search(out)
        self.assertIsNotNone(m, out)
        self.assertEqual(m.group(1), _git(self.src, "rev-parse", "HEAD"))
        # The spy saw the run: the public reads, the fresh clone the battery
        # checks and the gitleaks scan all passed through it.
        gits = [_git_verb(a) for a in calls if os.path.basename(a[0]) == "git"]
        for verb in ("fetch", "ls-remote", "clone", "commit-tree", "tag"):
            self.assertIn(verb, gits)
        self.assertIn("gitleaks", [os.path.basename(a[0]) for a in calls])
        self.assertEqual([a for a in calls if outward(a)], [])
        self.assertNothingWritten(out)
        # The tag the gates read exists in the work repository only.
        work = _work_line(out)
        self.assertEqual(_git(os.path.join(work, "release.git"), "tag", "-l"),
                         "v%s-nightly" % VERSION)

    def test_the_spy_sees_every_outward_call_of_a_publish(self):
        """The control: the same spy on --publish sees the stage push, the
        main push, the tag push and the gh call, so a spy that saw none on the
        nightly saw none because none was made."""
        rc, out, calls = self.spied("--publish", version=VERSION)
        self.assertEqual(rc, 0, out)
        self.assertEqual([_git_verb(a) or "?" if os.path.basename(a[0]) == "git"
                          else os.path.basename(a[0])
                          for a in calls if outward(a)],
                         ["push", "push", "push", "gh"])

    def test_a_failing_step_is_named_red(self):
        """The closing line names the FIRST step that refused, whichever it
        is, and nothing is written."""
        self.plant_attribution()
        rc, out, _work = self.nightly()
        self.assertEqual(rc, 1, out)
        m = NIGHTLY_RED.search(out)
        self.assertIsNotNone(m, out)
        self.assertEqual(m.groups(), ("gate/attribution",
                                      _git(self.src, "rev-parse", "HEAD")))
        self.assertIsNone(NIGHTLY_GREEN.search(out), out)
        self.assertNothingWritten(out)
        # another step: a stale omit list stops the build, before any gate
        _write(os.path.join(self.src, "scripts", "release", "omit.txt"),
               "docs/INTERNAL.md\ndocs/GONE.md\n")
        self.commit_trunk("trunk: a stale omit entry")
        rc, out, _work = self.nightly()
        self.assertEqual(rc, 1, out)
        self.assertEqual(NIGHTLY_RED.search(out).group(1), "build", out)
        self.assertNothingWritten(out)

    def test_the_nightly_runs_gitleaks_where_a_dry_run_would_skip_it(self):
        """A dry run skips gitleaks when it is not installed; a publish
        refuses. The nightly refuses like the publish, so it can never read
        green where the publish would stop."""
        absent = os.path.join(self.tmp, "no-such-gitleaks")
        rc, out, _work = self.release("--gitleaks", absent)
        self.assertEqual(rc, 0, out)
        self.assertIn("SKIP", out)
        rc, out, _work = self.nightly("--gitleaks", absent)
        self.assertEqual(rc, 1, out)
        self.assertEqual(NIGHTLY_RED.search(out).group(1), "gate/gitleaks", out)
        self.assertNothingWritten(out)

    def test_the_nightly_judges_trunk_between_releases(self):  # noqa: VACUOUS_ASSERTION — no gh call is the contract; rc 0 and the GREEN line naming the new trunk pin that the run happened
        """Between releases trunk declares the version already released: its
        tag is public, so a dry run of it refuses at the read and judges no
        gate. The nightly rehearses the next cut of that version, with the
        CHANGELOG's newest section as the notes, and judges the tree."""
        _git(self.seed, "tag", "v" + VERSION)
        _git(self.seed, "push", "-q", self.public, "refs/tags/v" + VERSION)
        _write(os.path.join(self.src, "README.md"), "# helm, moved on\n")
        trunk = self.commit_trunk("trunk: moves on after the release")
        rc, out, _work = self.release()
        self.assertEqual(rc, 1, out)
        self.assertIn("already has tag v%s" % VERSION, out)
        rc, out, work = self.nightly()
        self.assertEqual(rc, 0, out)
        self.assertEqual(NIGHTLY_GREEN.search(out).group(1), trunk, out)
        self.assertIn("%s-nightly" % VERSION, out)
        with open(os.path.join(work, "notes-v%s-nightly.md" % VERSION),
                  encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), SECTION)
        self.assertEqual(_git(self.public, "tag", "-l"), "v" + VERSION)
        self.assertEqual(_git(self.public, "rev-parse", "refs/heads/main"),
                         self.public_before)
        self.assertEqual(self.gh_calls(), [])

    def test_the_nightly_keeps_only_its_newest_work_directories(self):
        """One run a night leaves a clone and a backup behind; the nightly's
        default directory keeps its newest seven runs and nothing else of
        its own. A name it did not make is never touched."""
        base = os.path.join(self.home, ".helm", "releases", "nightly")
        old = ["202601%02dT000000Z-nightly-old%d" % (d, d) for d in range(1, 10)]
        for name in old + ["hands-off"]:
            os.makedirs(os.path.join(base, name))
        _write(os.path.join(base, "20260101T000000Z-nightly-file"), "not a run\n")
        rc, out, work = self.nightly(work=None)
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.path.dirname(work), base)
        runs = sorted(n for n in os.listdir(base) if "-nightly-" in n
                      and os.path.isdir(os.path.join(base, n)))
        self.assertEqual(runs, old[-6:] + [os.path.basename(work)])
        self.assertTrue(os.path.isdir(os.path.join(base, "hands-off")))
        self.assertTrue(os.path.isfile(os.path.join(
            base, "20260101T000000Z-nightly-file")))

    def test_the_nightly_takes_no_version_and_a_release_needs_one(self):  # noqa: VACUOUS_ASSERTION — the argv table is a non-empty literal and each case pins exit 2 and its message
        for argv, said in ((["--nightly", VERSION], "--nightly takes no version"),
                           ([], "the version is required"),
                           (["--nightly", "--publish"], "not allowed with"),
                           (["--nightly", "--candidate", "main"], "--candidate")):
            with self.subTest(argv=argv):
                p = subprocess.run([sys.executable, TOOL] + argv, text=True,
                                   capture_output=True, timeout=60,
                                   env=self.command()[1])
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn(said, p.stderr)


class RedactOrderTest(unittest.TestCase):

    def test_a_needle_that_contains_another_is_masked_whole(self):
        """A shorter needle listed first must not cut a longer one that
        contains it: the longer one's own characters would print."""
        tool = _tool()
        tool.NEEDLES[:] = ["zz-host", "zz-host.zz-private-suffix"]
        self.addCleanup(tool.NEEDLES.clear)
        out = tool.redact("remote zz-host.zz-private-suffix:repo, and zz-host")
        self.assertNotIn("zz-private-suffix", out)
        self.assertEqual(out, "remote <needle #2>:repo, and <needle #1>")

    def test_needles_that_overlap_without_nesting_print_no_character_of_either(self):
        """Two needles that share a piece of the text without one containing
        the other: replacing either first cuts the other, and its remaining
        characters would print. Neither list order may leak them, and each
        needle keeps its own number."""
        tool = _tool()
        self.addCleanup(tool.NEEDLES.clear)
        tool.NEEDLES[:] = ["zz-ab-host", "host-cd-zz"]
        out = tool.redact("x zz-ab-host-cd-zz y")
        self.assertEqual(out, "x <needle #1><needle #2> y")
        tool.NEEDLES[:] = ["host-cd-zz", "zz-ab-host"]
        out = tool.redact("x zz-ab-host-cd-zz y")
        self.assertEqual(out, "x <needle #2><needle #1> y")
        tool.NEEDLES[:] = ["zz-abc", "bc-de", "de-zz"]
        out = tool.redact("zz-abc-de-zz")
        self.assertEqual(out, "<needle #1><needle #2><needle #3>")


class OmitListTest(unittest.TestCase):

    def test_every_document_the_tests_excuse_is_one_the_release_omits(self):
        """tests/_release.py lets a test skip when a listed document is absent,
        on the claim that the release export omits it. That claim is this
        list; a document excused there and shipped here would skip nowhere and
        be excused for nothing."""
        # By its package name, as every other reader of it imports it: a
        # sys.path entry added here would outlive this test and change the
        # import path of every module the same process runs after it.
        from tests import _release
        with open(OMIT_LIST, encoding="utf-8") as f:
            entries = {ln.strip() for ln in f
                       if ln.strip() and not ln.lstrip().startswith("#")}
        planted = "docs/PLANTED-NOT-OMITTED.md"
        self.assertTrue(_release.RELEASE_OMITS)
        missing = sorted(p for p in _release.RELEASE_OMITS | {planted}
                         if p not in entries
                         and not any(e.endswith("/") and p.startswith(e)
                                     for e in entries))
        self.assertEqual(missing, [planted])


if __name__ == "__main__":
    unittest.main()
