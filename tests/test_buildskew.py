#!/usr/bin/env python3
"""A built tool's binary against its source checkout (task/2963).

The owner read cli-proxy-api's source at its checkout's HEAD and took it for
the code that was running, while the installed binary had been built from an
older commit. The doctor rung and the `helm seat status` line read the commit
the binary reports (`--version`, "Commit: <sha>") and the checkout's HEAD, and
say which: the same commit, a SKEW naming the installed commit and how to
read the code that runs (`git show <sha>:<path>`), or UNKNOWN when either
side does not read. UNKNOWN is never OK.

Fakes only: a stub binary in the real binary's shape (the banner on stdout,
then the flag error on stderr and exit 2: it has no --version flag), a temp
git repo, and a fixture registry naming it as the CLIProxyAPI project, the
record helm already keeps for the primary fork checkout. HELM_PROXY_BIN
points at the stub; no path is a literal."""
import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402

_tmp_home()

from helm import buildskew, doctor, registry  # noqa: E402

# the registry record helm keeps for the primary fork checkout
PROJECT = "CLIProxyAPI"


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(["git", "-C", repo] + list(args), check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


class BuildSkewBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-buildskew-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "src")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q")
        self.first = self.commit("main.go", "package main // one")
        self.bin = os.path.join(self.tmp, "cli-proxy-api")
        env = mock.patch.dict(os.environ, {"HELM_PROXY_BIN": self.bin})
        env.start()
        self.addCleanup(env.stop)
        self.register(self.repo)
        self.addCleanup(self.register, None)

    def register(self, path):
        """The registry's CLIProxyAPI record at `path`; None removes it."""
        reg = registry.load()
        if path is None:
            reg["projects"].pop(PROJECT, None)
        else:
            reg["projects"][PROJECT] = {
                "name": PROJECT, "path": path, "kind": "git",
                "sessions": {}, "edges": []}
        registry.save(reg)

    def commit(self, name, body):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(body + "\n")
        _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", "change " + name)
        return _git(self.repo, "rev-parse", "HEAD")

    def stub(self, out, rc=2, err="flag provided but not defined: -version\n"
             "Usage of cli-proxy-api:\n  -config string"):
        """The installed binary as the real one runs `--version`: its banner
        on stdout BEFORE flag parsing, then the flag error and usage on
        stderr, exit 2 (it has no --version flag)."""
        with open(self.bin, "w") as f:
            f.write("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\ncat >&2 <<'EOF'\n%s\nEOF\n"
                    "exit %d\n" % (out, err, rc))
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IXUSR)

    def version(self, sha):
        return "CLIProxyAPI Version: 6.1.2, Commit: %s, BuiltAt: 2026-09-01" % sha

    def only(self):
        got = buildskew.readings()
        self.assertEqual([r["tool"] for r in got], ["cli-proxy-api"])
        return got[0]


class TheReadingTest(BuildSkewBase):

    def test_the_same_commit_reads_same(self):
        self.stub(self.version(self.first))
        r = self.only()
        self.assertEqual(r["state"], buildskew.SAME, r)
        self.assertEqual(r["installed"], self.first)

    def test_a_short_reported_commit_is_UNKNOWN_never_the_same(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a fixed tuple of three lengths
        """REVIEW P2 (short prefix false SAME): a 7-11 hex abbreviation that
        prefixes HEAD may be an unrelated commit sharing the prefix, so it
        certifies nothing; it reads UNKNOWN, never SAME."""
        for n in (7, 9, 11):
            with self.subTest(hex=n):
                self.stub(self.version(self.first[:n]))
                r = self.only()
                self.assertEqual(r["state"], buildskew.UNKNOWN, r)
                self.assertIn("abbreviation of %d hex is too short to "
                              "certify; 12 or more is needed" % n, r["why"])

    def test_a_short_reported_commit_off_head_is_UNKNOWN_never_skew(self):
        """REVIEW P2: with fewer than 12 hex the installed commit is not
        identified, so a mismatch with HEAD cannot claim SKEW either."""
        self.stub(self.version(self.first[:9]))
        self.commit("main.go", "package main // two")
        r = self.only()
        self.assertEqual(r["state"], buildskew.UNKNOWN, r)
        self.assertIn("too short to certify", r["why"])

    def test_a_twelve_hex_commit_at_head_is_the_same(self):
        """The installed binary reports exactly 12 hex, the shortest that
        certifies."""
        self.stub(self.version(self.first[:12]))
        r = self.only()
        self.assertEqual((r["state"], r["installed"]),
                         (buildskew.SAME, self.first[:12]), r)

    def test_a_twelve_hex_commit_behind_head_is_a_skew(self):
        self.stub(self.version(self.first[:12]))
        head = self.commit("main.go", "package main // two")
        r = self.only()
        self.assertEqual((r["state"], r["installed"], r["head"]),
                         (buildskew.SKEW, self.first[:12], head), r)
        self.assertTrue(r["has"], r)

    def test_a_checkout_ahead_of_the_binary_is_a_skew_naming_both(self):
        self.stub(self.version(self.first))
        head = self.commit("main.go", "package main // two")
        r = self.only()
        self.assertEqual(r["state"], buildskew.SKEW, r)
        self.assertEqual((r["installed"], r["head"]), (self.first, head))
        line = buildskew.line(r)
        self.assertIn(self.first[:12], line)
        self.assertIn("git -C %s show %s:<path>" % (self.repo, self.first[:12]), line)

    def test_an_installed_commit_the_checkout_lacks_says_fetch_it(self):
        other = "0123456789abcdef0123456789abcdef01234567"
        self.stub(self.version(other))
        r = self.only()
        self.assertEqual(r["state"], buildskew.SKEW, r)
        self.assertIn("not in this checkout", buildskew.line(r))

    def test_a_version_with_no_commit_is_UNKNOWN(self):
        self.stub("CLIProxyAPI Version: 6.1.2")
        r = self.only()
        self.assertEqual(r["state"], buildskew.UNKNOWN, r)
        self.assertIn("Commit", r["why"])

    def test_the_installed_banner_verbatim_reads_its_commit(self):
        """The banner exactly as the installed binary prints it: a version
        carrying '+' parts is not a modified commit."""
        self.stub("CLIProxyAPI Version: 7.2.110-helm.16+length-cut+unsigned-"
                  "thinking+compaction-cap, Commit: %s, BuiltAt: "
                  "2026-09-27T02:18:10Z" % self.first[:12])
        r = self.only()
        self.assertEqual((r["state"], r["installed"], r["suffix"]),
                         (buildskew.SAME, self.first[:12], ""), r)

    def test_a_commit_in_error_text_with_no_banner_is_UNKNOWN(self):
        """REVIEW P2 (unanchored read): "Commit: <sha>" in usage or error text
        is not the binary's banner, and must not read SAME."""
        self.stub("", err="error: config says Commit: %s is pinned\n"
                  "Usage of cli-proxy-api:" % self.first)
        r = self.only()
        self.assertEqual(r["state"], buildskew.UNKNOWN, r)
        self.assertIn("names no version banner", r["why"])

    def test_two_banners_that_disagree_are_UNKNOWN_naming_both(self):
        other = "0123456789abcdef0123456789abcdef01234567"
        self.stub(self.version(self.first) + "\n" + self.version(other))
        r = self.only()
        self.assertEqual(r["state"], buildskew.UNKNOWN, r)
        self.assertIn("conflicting", r["why"])
        self.assertIn(self.first, r["why"])
        self.assertIn(other, r["why"])

    def test_a_stray_commit_that_disagrees_with_the_banner_is_UNKNOWN(self):
        other = "0123456789abcdef0123456789abcdef01234567"
        self.stub(self.version(self.first), err="flag provided but not "
                  "defined: -version\nCommit: %s" % other)
        r = self.only()
        self.assertEqual(r["state"], buildskew.UNKNOWN, r)
        self.assertIn("conflicting", r["why"])
        self.assertIn(other, r["why"])

    def test_a_value_that_is_not_a_commit_is_never_echoed(self):
        """A stray `Commit:` value in the binary's output that is not
        commit-shaped is named by its length only: it reaches no reason, no
        doctor line and no `seat status` line."""
        sentinel = "sk-FAKE-SENTINEL-2963-do-not-print"
        cases = (
            (self.version(self.first), "defined: -version\nCommit: " + sentinel,
             "conflicting"),
            ("CLIProxyAPI Version: 7.2.110-helm.16, Commit: %s, BuiltAt: x"
             % sentinel, "", "names no commit"),
        )
        for out, err, words in cases:
            with self.subTest(words=words):
                self.stub(out, err=err)
                r = self.only()
                self.assertEqual(r["state"], buildskew.UNKNOWN, r)
                self.assertIn(words, r["why"])
                self.assertIn("not a commit", r["why"])
                printed = [buildskew.line(r)] + buildskew.status_lines() + [
                    text for _level, text in doctor.check_build_skew()]
                for text in printed:
                    self.assertNotIn(sentinel, text)
                    self.assertNotIn("FAKE-SENTINEL", text)

    def test_a_secret_glued_to_real_hex_is_never_echoed(self):
        """A secret-shaped value glued after real hex is named by its length
        in every line that could print it: the MODIFIED line, a conflict,
        and a banner whose hex is too short to be a commit. So is a plain
        one-word marker, since no text the binary chose is printed but its
        hex; the MODIFIED state itself still reads (the control)."""
        secret = "-sk-FAKE-SENTINEL-2963-glued"
        cases = (
            ("CLIProxyAPI Version: 7.2.110-helm.16, Commit: %s%s, BuiltAt: x"
             % (self.first[:12], secret), "", buildskew.MODIFIED, "build marker"),
            (self.version(self.first), "Commit: %s%s" % (self.first[:12], secret),
             buildskew.UNKNOWN, "conflicting"),
            ("CLIProxyAPI Version: 7.2.110-helm.16, Commit: dead%s, BuiltAt: x"
             % secret, "", buildskew.UNKNOWN, "names no commit"),
        )
        for out, err, state, words in cases:
            with self.subTest(words=words):
                self.stub(out, err=err)
                r = self.only()
                self.assertEqual(r["state"], state, r)
                shown = buildskew.line(r)
                self.assertIn(words, shown)
                printed = [shown] + buildskew.status_lines() + [
                    text for _level, text in doctor.check_build_skew()]
                for text in printed:
                    self.assertNotIn("FAKE-SENTINEL", text)
        self.stub("CLIProxyAPI Version: 7.2.110-helm.16, Commit: %s-skantsynthetic123456, "
                  "BuiltAt: x" % self.first[:12])
        r = self.only()
        self.assertEqual(r["state"], buildskew.MODIFIED, r)
        self.assertIn("a 21-character build marker", buildskew.line(r))
        self.assertNotIn("skantsynthetic", buildskew.line(r))

    def test_a_banner_printed_twice_with_one_commit_still_reads(self):
        """Control: candidates that AGREE are not a conflict."""
        self.stub(self.version(self.first) + "\n" + self.version(self.first))
        r = self.only()
        self.assertEqual((r["state"], r["installed"]),
                         (buildskew.SAME, self.first), r)

    def test_the_real_shape_reads_the_commit_whatever_the_exit(self):
        """CURE P1-a: the real binary prints its banner, then exits 2 on the
        unknown flag. The commit is read off the output whatever the exit."""
        self.stub(self.version(self.first), rc=2)
        r = self.only()
        self.assertEqual((r["state"], r["installed"]),
                         (buildskew.SAME, self.first), r)

    def test_a_version_run_that_times_out_is_UNKNOWN(self):
        self.stub(self.version(self.first))
        with open(self.bin, "w") as f:
            f.write("#!/bin/sh\nsleep 5\n")
        with mock.patch.object(buildskew, "VERSION_TIMEOUT_S", 0.3):
            r = self.only()
        self.assertEqual(r["state"], buildskew.UNKNOWN, r)
        self.assertIn("did not run", r["why"])

    def test_a_modified_build_never_reads_same(self):
        """CURE P2: "Commit: <hex>+cooldownverify" (or -dirty) was built from
        a modified tree; its hex at HEAD must not read SAME."""
        for suffix in ("+cooldownverify", "-dirty"):
            with self.subTest(suffix=suffix):
                self.stub(self.version(self.first[:12] + suffix))
                r = self.only()
                self.assertEqual(r["state"], buildskew.MODIFIED, r)
                line = buildskew.line(r)
                self.assertIn("built from commit %s plus local changes (a %d-"
                              "character build marker)"
                              % (self.first[:12], len(suffix)), line)
                self.assertNotIn(suffix, line)
                self.assertIn("no checkout shows that code", line)

    def test_a_skew_names_the_worktree_at_the_installed_commit(self):
        """CURE P3: a linked worktree whose HEAD IS the installed commit is
        where the running code can be read; the git show form stays the
        fallback."""
        self.stub(self.version(self.first))
        wt = os.path.join(self.tmp, "wt-installed")
        _git(self.repo, "worktree", "add", "-q", "-b", "installed", wt, self.first)
        self.commit("main.go", "package main // two")
        r = self.only()
        self.assertEqual(r["state"], buildskew.SKEW, r)
        self.assertEqual(r["worktree"], wt)
        self.assertIn("the running code is checked out at %s" % wt,
                      buildskew.line(r))

    def test_a_missing_binary_is_UNKNOWN(self):
        r = self.only()
        self.assertEqual(r["state"], buildskew.UNKNOWN, r)
        self.assertIn("HELM_PROXY_BIN", r["why"])

    def test_a_missing_or_non_git_checkout_is_UNKNOWN(self):
        self.stub(self.version(self.first))
        for path in (os.path.join(self.tmp, "gone"), self.tmp):
            with self.subTest(path=path):
                self.register(path)
                r = self.only()
                self.assertEqual(r["state"], buildskew.UNKNOWN, r)
                self.assertIn(PROJECT, r["why"])

    def test_no_registry_record_is_UNKNOWN_naming_the_project(self):
        """CURE P1-b: the checkout is the registry's CLIProxyAPI record, the
        primary fork checkout, never the upstream checker's clone."""
        self.stub(self.version(self.first))
        self.register(None)
        r = self.only()
        self.assertEqual(r["state"], buildskew.UNKNOWN, r)
        self.assertIn("no registry record for project %s" % PROJECT,
                      r["why"])
        # the upstream checker's clone variable no longer steers it
        with mock.patch.dict(os.environ, {"HELM_PROXY_FORK_DIR": self.repo}):
            self.assertEqual(self.only()["state"], buildskew.UNKNOWN)

    def test_nothing_raises_out_of_a_broken_probe(self):
        self.stub(self.version(self.first))
        with mock.patch.object(buildskew.subprocess, "Popen",
                               side_effect=OSError("exec format error")):
            r = self.only()
        self.assertEqual(r["state"], buildskew.UNKNOWN)
        self.assertIn("did not run (OSError)", r["why"])


def _alive(pid):
    """Is `pid` a live process? A zombie is dead: it runs nothing."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


class TheProbeBoundsTest(BuildSkewBase):
    """REVIEW P2/P3: what the `--version` probe and the whole reading may
    cost, and that the reading writes nothing."""

    def test_a_timeout_kills_the_probe_s_whole_process_group(self):
        """A timeout that kills only the direct process leaves its children
        running. The probe runs in its own session and the timeout kills the
        whole group."""
        pidfile = os.path.join(self.tmp, "child.pid")
        with open(self.bin, "w") as f:
            f.write("#!/bin/sh\nsleep 30 &\necho $! > %s\nwait\n" % pidfile)
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IXUSR)

        def reap():
            try:
                with open(pidfile) as f:
                    os.kill(int(f.read().strip()), 9)
            except (OSError, ValueError):
                pass
        self.addCleanup(reap)
        with mock.patch.object(buildskew, "VERSION_TIMEOUT_S", 1.0):
            r = self.only()
        self.assertEqual(r["state"], buildskew.UNKNOWN, r)
        self.assertIn("did not run", r["why"])
        with open(pidfile) as f:
            child = int(f.read().strip())
        self.assertGreater(child, 1, "control: the probe started its child")
        for _ in range(30):
            if not _alive(child):
                break
            time.sleep(0.1)
        self.assertFalse(_alive(child), "the probe's child %d outlived the "
                         "timeout" % child)

    def test_the_reading_that_spends_its_budget_is_UNKNOWN(self):
        """REVIEW P2 (cost): ONE deadline over the whole reading. A probe
        that spends it leaves nothing for the checkout reads."""
        def slow(*_a, **_k):
            time.sleep(0.3)
            return self.first, "", None
        self.stub(self.version(self.first))
        with mock.patch.object(buildskew, "BUDGET_S", 0.2, create=True), \
                mock.patch.object(buildskew, "_installed", side_effect=slow):
            r = self.only()
        self.assertEqual(r["state"], buildskew.UNKNOWN, r)
        self.assertIn("ran out of its 0.2 s budget", r["why"])

    def test_later_calls_get_only_the_remaining_budget(self):
        """Each git call's timeout is what is left of the one deadline, never
        a fresh per-call allowance."""
        from helm import vcs
        seen = []
        cls = type(vcs.backend(self.repo))
        real = cls.proc

        def spy(self_, cwd, *args, timeout=None):
            seen.append(timeout)
            return real(self_, cwd, *args, timeout=timeout)

        def slow(*_a, **_k):
            time.sleep(0.3)
            return self.first, "", None
        self.stub(self.version(self.first))
        self.commit("main.go", "package main // two")
        with mock.patch.object(buildskew, "BUDGET_S", 10, create=True), \
                mock.patch.object(buildskew, "_installed", side_effect=slow), \
                mock.patch.object(cls, "proc", spy):
            r = self.only()
        self.assertEqual(r["state"], buildskew.SKEW, r)
        # control: a SKEW asks git three things (HEAD, whether the checkout
        # holds the installed commit, the worktree list)
        self.assertEqual(len(seen), 3, seen)
        self.assertTrue(all(t is not None and 0 < t <= 9.7 for t in seen),
                        seen)

    def test_the_reading_does_not_migrate_a_mixed_era_registry(self):
        """REVIEW P2 (read-only): the ordinary registry load migrates a
        mixed-era registry.json and persists it. The reading uses the strict
        load, which never writes."""
        from helm import home, pk
        raw = pk.read_json(home.registry_path())
        raw["projects"][PROJECT]["notes"] = "an inline mixed-era field"
        pk.write_json(home.registry_path(), raw)

        def snap():
            out = {}
            for p in (home.registry_path(), home.authored_path()):
                try:
                    with open(p, "rb") as f:
                        out[p] = f.read()
                except FileNotFoundError:
                    out[p] = None
            return out
        before = snap()
        # control: the fixture IS mixed-era, so an ordinary load owes a write
        self.assertIn(b"an inline mixed-era field",
                      before[home.registry_path()])
        self.assertTrue(registry._load(False, persist=False)[1],
                        "control: an ordinary load would migrate this")
        self.stub(self.version(self.first))
        r = self.only()
        self.assertEqual(r["state"], buildskew.SAME, r)
        self.assertEqual(snap(), before, "the reading rewrote the registry")


class TheDoctorRungTest(BuildSkewBase):

    def test_the_rung_is_registered(self):
        self.assertIn("check_build_skew", doctor.CHECKS)

    def test_same_is_OK_and_skew_and_unknown_are_never_OK(self):
        self.stub(self.version(self.first))
        self.assertEqual([lvl for lvl, _ in doctor.check_build_skew()],
                         [doctor.OK])
        self.commit("main.go", "package main // two")
        rows = doctor.check_build_skew()
        self.assertEqual([lvl for lvl, _ in rows], [doctor.WARN], rows)
        self.assertIn("SKEW", rows[0][1])
        self.stub("no version here")
        rows = doctor.check_build_skew()
        self.assertEqual([lvl for lvl, _ in rows], [doctor.WARN], rows)
        self.assertIn("UNKNOWN", rows[0][1])


class TheSeatStatusLineTest(BuildSkewBase):

    def test_seat_status_prints_the_line(self):
        from helm import seat  # noqa: F401 — the facade before its impl
        from helm import seat_health, seat_usability
        self.stub(self.version(self.first))
        self.commit("main.go", "package main // two")
        buf = io.StringIO()
        with mock.patch.object(seat_usability, "join", return_value={}), \
                mock.patch.object(seat_health, "_seat_row",
                                  return_value="codex  row"), \
                mock.patch.object(seat_health, "_minted_instances",
                                  return_value=[]), \
                contextlib.redirect_stdout(buf):
            seat_health._status_render(["codex"])
        out = buf.getvalue()
        self.assertIn("cli-proxy-api", out)
        self.assertIn("SKEW", out)
        self.assertIn(self.first[:12], out)
        # a column-0 line on that screen is a seat row; this one is not
        build = [ln for ln in out.splitlines() if "cli-proxy-api" in ln]
        self.assertTrue(build and all(ln.startswith(" ") for ln in build), out)

    def test_a_reading_that_does_not_import_is_one_UNKNOWN_line(self):
        """REVIEW P2: the status screen never raises. An import failure of
        the reading itself is one indented UNKNOWN line, and the render goes
        on to its legend."""
        from helm import seat  # noqa: F401 — the facade before its impl
        from helm import seat_health, seat_usability
        import helm
        saved = helm.__dict__.pop("buildskew")
        self.addCleanup(setattr, helm, "buildskew", saved)
        buf = io.StringIO()
        with mock.patch.dict(sys.modules, {"helm.buildskew": None}), \
                mock.patch.object(seat_usability, "join", return_value={}), \
                mock.patch.object(seat_health, "_seat_row",
                                  return_value="codex  row"), \
                mock.patch.object(seat_health, "_minted_instances",
                                  return_value=[]), \
                mock.patch.object(seat_usability, "legend",
                                  return_value="LEGEND"), \
                contextlib.redirect_stdout(buf):
            rc = seat_health._status_render(["codex"])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        build = [ln for ln in out.splitlines() if "build" in ln]
        self.assertEqual(len(build), 1, out)
        self.assertTrue(build[0].startswith(" ") and "UNKNOWN" in build[0],
                        out)
        self.assertEqual(out.splitlines()[-1], "LEGEND", out)

    def test_the_doctor_rung_survives_the_same_import_failure(self):
        """The other surface asking the same question: one WARN, UNKNOWN."""
        import helm
        saved = helm.__dict__.pop("buildskew")
        self.addCleanup(setattr, helm, "buildskew", saved)
        with mock.patch.dict(sys.modules, {"helm.buildskew": None}):
            rows = doctor.check_build_skew()
        self.assertEqual([lvl for lvl, _ in rows], [doctor.WARN], rows)
        self.assertIn("UNKNOWN", rows[0][1])


if __name__ == "__main__":
    unittest.main()
