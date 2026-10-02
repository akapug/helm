#!/usr/bin/env python3
"""helm.console_walk — the console walk owed, the brief a fresh reader walks
by, and the one post-land line that says it is owed (task/3444).

Every fixture is a temp git repo and a temp HELM_HOME; commit times are set
through GIT_COMMITTER_DATE and report times through os.utime, so each answer
is decided by the clock the test names."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-console-walk-", var="HELM_HOME")

from helm import console_walk  # noqa: E402

DAY = 86400
T0 = 1790000000   # a fixed instant; every other time is an offset from it


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-console-walk-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home")})
        env.start()
        self.addCleanup(env.stop)
        self.root = os.path.realpath(os.path.join(self.tmp, "proj"))
        os.makedirs(self.root)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.commit("README", "base", T0 - 30 * DAY)

    def git(self, *args, when=None):
        env = dict(os.environ)
        if when is not None:
            env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = \
                "@%d +0000" % when
        proc = subprocess.run(("git",) + args, cwd=self.root, env=env,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def commit(self, path, subject, when):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a") as f:
            f.write("%s\n" % subject)
        self.git("add", path)
        self.git("commit", "-q", "-m", subject, when=when)
        return self.git("rev-parse", "HEAD")

    def report(self, n, when):
        d = console_walk.reviews_dir(self.root)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "2026-09-%02d-console-walk-%d.md" % (n, n))
        with open(path, "w") as f:
            f.write("# console walk %d\n" % n)
        os.utime(path, (when, when))
        return path


class Owed(Base):
    def test_a_web_ui_commit_newer_than_the_last_report_makes_it_owed(self):
        self.report(1, T0 - 5 * DAY)
        sha = self.commit("helm/web_ui/views/home.html.part",
                          "train9: merge lane home-tabs", T0 - 2 * DAY)
        self.commit("helm/web_ui/views/home.html.part",
                    "train10: merge lane home-more", T0 - 1 * DAY)
        got = console_walk.owed(self.root, now=T0)
        self.assertTrue(got["owed"], got)
        # the land that MADE it owed is the first one after the walk
        self.assertEqual(got["land"]["sha"], sha)
        self.assertIn("train9: merge lane home-tabs", got["land"]["subject"])
        self.assertEqual(got["age_s"], 2 * DAY)

    def test_a_console_server_module_counts_as_the_console(self):
        self.report(1, T0 - 5 * DAY)
        self.commit("helm/web_owed.py", "train9: merge lane owed-card",
                    T0 - 2 * DAY)
        self.assertTrue(console_walk.owed(self.root, now=T0)["owed"])

    def test_a_report_newer_than_the_last_web_commit_is_not_owed(self):
        self.commit("helm/web_ui/views/home.html.part", "train9: web",
                    T0 - 3 * DAY)
        self.report(1, T0 - 2 * DAY)
        # a land that does not touch the console owes no walk
        self.commit("helm/chat.py", "train10: chat only", T0 - 1 * DAY)
        got = console_walk.owed(self.root, now=T0)
        self.assertFalse(got["owed"], got)
        self.assertIsNone(got["land"])

    def test_owed_by_age_alone_at_seven_days(self):
        self.commit("helm/web_ui/views/home.html.part", "train9: web",
                    T0 - 20 * DAY)
        self.report(1, T0 - 10 * DAY)
        self.assertEqual(console_walk.WALK_EVERY_S, 7 * DAY)
        got = console_walk.owed(self.root, now=T0 - 10 * DAY
                                + console_walk.WALK_EVERY_S - 60)
        self.assertFalse(got["owed"], got)
        got = console_walk.owed(self.root, now=T0 - 3 * DAY)
        self.assertTrue(got["owed"], got)
        self.assertIsNone(got["land"])
        self.assertIn("7 days", got["reason"])
        self.assertEqual(got["age_s"], 0)

    def test_the_named_cause_and_the_age_come_from_one_source(self):
        # the walk turned 7 days old (T0-3d) before the land (T0-1d): the
        # age is the cause, so no land is named against the age's clock
        self.report(1, T0 - 10 * DAY)
        self.commit("helm/web_ui/views/home.html.part", "train9: web",
                    T0 - 1 * DAY)
        got = console_walk.owed(self.root, now=T0)
        self.assertTrue(got["owed"], got)
        self.assertIsNone(got["land"])
        self.assertIn("7 days", got["reason"])
        self.assertEqual(got["age_s"], 3 * DAY)

    def test_an_unreadable_reviews_dir_reads_unknown_not_owed(self):
        d = console_walk.reviews_dir(self.root)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        with open(d, "w") as f:          # a file where the dir should be
            f.write("x")
        got = console_walk.owed(self.root, now=T0)
        self.assertIsNone(got["owed"], got)
        self.assertIn("UNKNOWN", got["reason"])

    def test_an_empty_stub_is_not_a_walk(self):
        path = self.report(1, T0 - DAY)
        open(path, "w").close()
        os.utime(path, (T0 - DAY, T0 - DAY))
        got = console_walk.owed(self.root, now=T0)
        self.assertTrue(got["owed"], got)
        self.assertIn("no walk on record", got["reason"])

    def test_a_cut_subject_leaves_no_unbalanced_bracket(self):
        words = console_walk._land_words({
            "sha": "a" * 40, "subject": "train439: merge lane " + "x" * 50
            + " (no task, P?, a very long parenthetical that runs on)"})
        self.assertEqual(words.count("("), words.count(")"))

    def test_a_cut_inside_a_nested_parenthetical_drops_the_outer_one(self):
        subject = ("train440: merge lane " + "x" * 20
                   + " (no task (a) then a tail that runs on well past the cut")
        self.assertGreater(len(subject), 80)
        self.assertEqual(subject[:80].count("("), 2)   # the cut is inside both
        self.assertEqual(subject[:80].count(")"), 1)   # after the inner one closed
        words = console_walk._land_words({"sha": "a" * 40, "subject": subject})
        self.assertTrue(words.startswith("a" * 11 + " train440: merge lane"), words)
        self.assertEqual(words.count("("), words.count(")"), words)

    def test_no_report_at_all_reads_owed_with_no_walk_on_record(self):
        got = console_walk.owed(self.root, now=T0)
        self.assertTrue(got["owed"], got)
        self.assertIn("no walk on record", got["reason"])
        self.assertIsNone(got["last"])
        self.assertEqual(got["next_n"], 1)


class Brief(Base):
    def test_the_brief_carries_both_widths_the_next_report_and_both_rules(
            self):
        self.report(1, T0 - 20 * DAY)
        self.report(2, T0 - 5 * DAY)     # the land comes before it ages
        self.commit("helm/web_ui/views/home.html.part",
                    "train9: merge lane home-tabs", T0 - 2 * DAY)
        text = console_walk.brief(self.root, now=T0)
        self.assertIn("1440", text)
        self.assertIn("420", text)
        want = os.path.join(console_walk.reviews_dir(self.root),
                            "%s-console-walk-3.md"
                            % console_walk._date(T0))
        self.assertIn(want, text)
        self.assertIn("RULE 1", text)
        self.assertIn("one number and one word per noun", text)
        self.assertIn("RULE 2", text)
        self.assertIn("never tells the owner to run a CLI verb", text)
        # the owed reason travels with the brief
        self.assertIn("train9: merge lane home-tabs", text)
        # walk 3 (task/3723): a walker's hand in Chat consumed the owner's
        # unread; the brief keeps every walker out of it
        self.assertIn("Never open Chat", text)
        self.assertIn("marks the owner's unread messages as read", text)

    def test_the_brief_walks_the_owners_server_and_defines_each_grade(self):
        """Console walk 4 (task/3723) flagged two gaps in this brief. It sent
        the walker to `helm web` and `helm web shot`, and each starts a server
        of its own, which the laptop-health rule bans. And the walker graded
        findings P1, P2 and P3 with no definition printed here. The brief now
        names the owner's own server, one browser tab and no server started,
        and it prints the grades walk 3 used."""
        from helm.web_common import DEFAULT_PORT
        text = console_walk.brief(self.root, now=T0)
        self.assertIn("http://127.0.0.1:%d" % DEFAULT_PORT, text)
        self.assertIn("one browser tab", text)
        self.assertIn("Start no server", text)
        self.assertNotIn("helm web shot --view", text)
        self.assertNotIn("Open the console (`helm web`)", text)
        self.assertIn("Never open Chat", text)
        for grade, words in (
                ("P1:", "a false or contradicting number or word"),
                ("P1:", "a CLI verb"),
                ("P2:", "a hover, a footer or a secondary fold"),
                ("P3:", "cosmetic")):
            with self.subTest(grade=grade, words=words):
                self.assertIn(grade, text)
                line = text[text.index(grade):].split("\n  P", 1)[0]
                self.assertIn(words, line)

    def test_the_verb_prints_the_brief(self):
        import io
        from contextlib import redirect_stdout
        from helm import web_server
        out = io.StringIO()
        with redirect_stdout(out):
            rc = web_server.cmd_web(["walk", "--repo", self.root])
        self.assertEqual(rc, 0)
        self.assertIn("console-walk-1.md", out.getvalue())
        self.assertIn("no walk on record", out.getvalue())


class Checks(Base):
    """The release-gate check block the confirm walk runs (task/3938): each
    named check is listed under "CHECK THESE (release gate)", and a
    --release flag reads those checks from the design doc's Acceptance
    section console line."""

    def test_brief_lists_named_checks_in_the_release_gate_block(self):
        """Explicit --check lines are enumerated in the block, in order, and
        the never-open-Chat line still prints below them."""
        text = console_walk.brief(self.root, now=T0, checks=(
            "stories with N of M",
            "one health line per project",
            "a task's meld thread under it",
        ))
        self.assertIn("CHECK THESE (release gate)", text)
        block = text.split("CHECK THESE (release gate)")[1]
        before_method = block.split("CONSOLE WALK")[0]
        self.assertIn("  1. stories with N of M", before_method)
        self.assertIn("  2. one health line per project", before_method)
        self.assertIn("  3. a task's meld thread under it", before_method)
        self.assertNotIn("CHECK THESE", before_method.split("\n\n")[-1])
        self.assertIn("Never open Chat", text)

    def test_brief_with_no_checks_prints_no_block(self):
        text = console_walk.brief(self.root, now=T0)
        self.assertNotIn("CHECK THESE", text)
        self.assertIn("Never open Chat", text)

    def test_brief_ignores_blank_checks(self):
        text = console_walk.brief(self.root, now=T0, checks=("  ", ""))
        self.assertNotIn("CHECK THESE", text)

    def test_cmd_walk_accepts_repeatable_check_flag(self):
        import io
        from contextlib import redirect_stdout
        from helm import web_server
        out = io.StringIO()
        with redirect_stdout(out):
            rc = web_server.cmd_web([
                "walk", "--repo", self.root,
                "--check", "check A", "--check", "check B"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("CHECK THESE (release gate)", text)
        self.assertIn("  1. check A", text)
        self.assertIn("  2. check B", text)

    def test_cmd_walk_rejects_unknown_flag(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from helm import web_server
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = web_server.cmd_web(
                ["walk", "--repo", self.root, "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm web walk", err.getvalue())


class ReleaseChecks(Base):
    """A --release flag reads its checks from the design doc's Acceptance
    section console line (task/3748: 0.3.3)."""

    def _write_design_doc(self, release="0.3.3"):
        """Drop a design doc named <date>-<release>-design.md under the
        project's reviews dir, with an Acceptance section whose console line
        is the 0.3.3 console check."""
        d = console_walk.reviews_dir(self.root)
        os.makedirs(d, exist_ok=True)
        text = ("# helm %s — one number, one story\n\n"
                "## Acceptance (release gate)\n"
                "5. On the owner's console: stories with N of M, one health "
                "line per project, and a task's meld thread under it. The "
                "0.3.3 confirm walk checks these, with a reader who never "
                "opens Chat.\n"
                "\n## Tasks\n"
                "helm#3742\n"
                ) % release
        path = os.path.join(d, "%s-%s-design.md" % (T0, release))
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_release_flag_reads_acceptance_console_line(self):
        import io
        from contextlib import redirect_stdout
        from helm import web_server
        path = self._write_design_doc()
        os.path.exists(path)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = web_server.cmd_web([
                "walk", "--repo", self.root, "--release", "0.3.3"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("CHECK THESE (release gate)", text)
        self.assertIn("stories with N of M", text)
        self.assertIn("one health line", text)
        self.assertIn("a task's meld thread under it", text)
        self.assertNotIn("0.3.3 confirm walk checks these", text)

    def test_release_and_check_show_release_checks_then_explicit(self):
        # (task/3938 round 3) an explicit --check must not HIDE the release
        # checks: --release reads the doc's checks and --check adds its own,
        # both in the block, release checks first.
        import io
        from contextlib import redirect_stdout
        from helm import web_server
        self._write_design_doc("0.3.3")
        self._write_design_doc("0.3.30")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = web_server.cmd_web([
                "walk", "--repo", self.root,
                "--release", "0.3.3", "--check", "explicit A"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        # the release check AND the explicit one, in order
        rel = "stories with N of M"
        self.assertIn("CHECK THESE (release gate)", text)
        self.assertIn(rel, text)
        self.assertIn("explicit A", text)
        self.assertLess(text.index(rel), text.index("explicit A"), text)

    def test_release_checks_empty_when_no_doc(self):
        self.assertEqual(console_walk.release_checks(self.root, "0.3.3"), [])

    def test_release_flag_does_not_take_a_prefix_match(self):
        # (task/3938 round 3) --release 0.3.3 must read the 0.3.3 doc, not
        # the sibling whose name CONTAINS "0.3.3" (the 0.3.30 doc). The
        # 0.3.30 doc carries an EARLIER date, so it sorts first and is what
        # the substring test picks — the whole defect the cure closes. Each
        # doc's check line is distinct (and dot-free up to its period, the
        # way the acceptance parser reads the console clause).
        d = console_walk.reviews_dir(self.root)
        os.makedirs(d, exist_ok=True)
        for date, release, checks in (
                (T0 - DAY, "0.3.30", "zeta check, omega check, delta under it"),
                (T0, "0.3.3", "alpha check, beta check, gamma under it"),
                ):
            text = ("# helm %s — one number, one story\n\n"
                    "## Acceptance (release gate)\n"
                    "5. On the owner's console: %s. The %s confirm walk "
                    "checks these, with a reader who never opens Chat.\n"
                    "\n## Tasks\n"
                    "helm#3742\n" % (release, checks, release))
            with open(os.path.join(d,
                                   "%s-%s-design.md" % (date, release)), "w") \
                    as fh:
                fh.write(text)
        self.assertEqual(
            console_walk.release_checks(self.root, "0.3.3"),
            ["alpha check", "beta check", "gamma under it"])
        self.assertEqual(
            console_walk.release_checks(self.root, "0.3.30"),
            ["zeta check", "omega check", "delta under it"])

    def test_release_checks_empty_when_no_acceptance_console_line(self):
        d = console_walk.reviews_dir(self.root)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "2026-09-30-0.3.3-design.md"), "w") as fh:
            fh.write("# design\n\n## Acceptance\n1. other check.\n")
        self.assertEqual(console_walk.release_checks(self.root, "0.3.3"), [])
        self.report(1, T0 - 5 * DAY)
        self.commit("helm/web_ui/views/home.html.part",
                    "train9: merge lane home-tabs", T0 - 2 * DAY)
        posts = []

        def post(text):
            posts.append(text)
            return True

        line = console_walk.surface(self.root, post, now=T0)
        self.assertIn("console walk owed since", line)
        self.assertIn("train9: merge lane home-tabs", line)
        self.assertIn("2d", line)
        self.assertEqual(posts, [line])
        # the same owed state, on a later land and a later call: said once
        self.commit("helm/web_ui/views/home.html.part",
                    "train10: merge lane home-more", T0 - 1 * DAY)
        self.assertIsNone(console_walk.surface(self.root, post, now=T0 + 60))
        self.assertEqual(len(posts), 1)
        # a walk discharges it; nothing is said while nothing is owed
        self.report(2, T0 + 120)
        self.assertIsNone(console_walk.surface(self.root, post, now=T0 + 180))
        # a new land after that walk is a NEW owed state, said again
        self.commit("helm/web_ui/views/home.html.part",
                    "train11: merge lane ledger-tab", T0 + 240)
        again = console_walk.surface(self.root, post, now=T0 + 300)
        self.assertIn("train11", again)
        self.assertEqual(len(posts), 2)

    def test_with_no_walk_on_record_a_later_land_is_the_same_state(self):
        # the live state today: no report yet, and each console land must
        # not post the line again (REVIEW 3444 finding 1)
        self.commit("helm/web_ui/views/home.html.part", "train9: web",
                    T0 - 2 * DAY)
        posts = []

        def post(text):
            posts.append(text)
            return True

        self.assertIsNotNone(console_walk.surface(self.root, post, now=T0))
        self.commit("helm/web_ui/views/home.html.part", "train10: web",
                    T0 + 60)
        self.assertIsNone(console_walk.surface(self.root, post,
                                               now=T0 + 120))
        self.assertEqual(len(posts), 1)

    def test_a_land_after_the_age_turned_is_the_same_state(self):
        self.report(1, T0 - 10 * DAY)
        posts = []

        def post(text):
            posts.append(text)
            return True

        self.assertIsNotNone(console_walk.surface(self.root, post, now=T0))
        self.commit("helm/web_ui/views/home.html.part", "train9: web",
                    T0 + 60)
        self.assertIsNone(console_walk.surface(self.root, post,
                                               now=T0 + 120))
        self.assertEqual(len(posts), 1)

    def test_a_record_that_fails_is_said(self):
        self.commit("helm/web_ui/views/home.html.part", "train9: web",
                    T0 - DAY)
        said = []
        with mock.patch.object(console_walk, "_write_said",
                               return_value="disk full"):
            line = console_walk.surface(self.root, lambda t: True, now=T0,
                                        say=said.append)
        self.assertIsNotNone(line)
        self.assertEqual(len(said), 1)
        self.assertIn("disk full", said[0])

    def test_a_post_that_fails_is_said_again_next_time(self):
        self.commit("helm/web_ui/views/home.html.part", "train9: web",
                    T0 - DAY)
        self.assertIsNone(console_walk.surface(self.root, lambda t: False,
                                               now=T0))
        posts = []
        console_walk.surface(self.root, lambda t: posts.append(t) or True,
                             now=T0 + 60)
        self.assertEqual(len(posts), 1)


if __name__ == "__main__":
    unittest.main()
