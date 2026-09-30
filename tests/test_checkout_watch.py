#!/usr/bin/env python3
"""helm work checkout-watch (task/3301, rounds 2 and 3).

A dirty shared checkout posts once per distinct `git status --porcelain`.
The same dirt posts nothing more. A clean tree clears the latch, so the
next dirt posts again. Round 3: every alert names the integrator that
seats_integrator resolves (or says none resolved); a git operation in
flight skips the tick without moving the latch; an unreadable checkout
posts once per distinct failure. The post seam is faked: nothing here
touches a real checkout or a real room. Every timer arm runs through
tests._tmphome.fake_user_systemd (task/3306): its units land under a temp
home and a recording systemctl answers, so no arm reaches this host's
systemd. The install's whole contract is checkoutwatch's row in
tests/test_timerhealth.py's installer table (task/3307).
"""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tests._tmphome import fake_user_systemd
from tests._tmphome import home as _tmp_home

_tmp_home(prefix="helm-test-checkout-watch-", var="HELM_HOME")

from helm import checkoutwatch  # noqa: E402
from helm.work._cli import cmd_work  # noqa: E402

_RELOAD = ["--user", "daemon-reload"]
_ENABLE = ["--user", "enable", "--now", checkoutwatch.TIMER_NAME]


def _git(where, *args):
    ran = subprocess.run(["git", "-C", where] + list(args),
                         capture_output=True, text=True)
    if ran.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(args), ran.stderr))
    return ran.stdout.strip()


def _repo():
    root = tempfile.mkdtemp(prefix="helm-test-checkout-watch-")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    path = os.path.join(root, "note.txt")
    with open(path, "w") as fh:
        fh.write("base\n")
    _git(root, "add", "note.txt")
    _git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    return root


class CheckoutWatchTest(unittest.TestCase):
    def setUp(self):
        self.root = _repo()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_watch(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cmd_work(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_a_dirty_tracked_file_posts_once_then_again_only_after_clean(self):
        path = os.path.join(self.root, "note.txt")
        with open(path, "a") as fh:
            fh.write("dirty\n")
        mtime = str(int(os.stat(path).st_mtime))
        with mock.patch("helm.chat.post") as post:
            rc, out, err = self.run_watch(
                "checkout-watch", "--repo", self.root, "--apply")
            self.assertEqual(rc, 0, err)
            self.assertEqual(post.call_count, 1)
            body = post.call_args[0][0]
            first_event = post.call_args.kwargs["event_id"]
            self.assertEqual(post.call_args.kwargs["who"], checkoutwatch.POSTER)
            self.assertFalse(post.call_args.kwargs["sign"])
            self.assertIn("note.txt", body)
            self.assertIn("mtime %s" % mtime, body)
            self.assertIn(checkoutwatch.FIX, body)
            self.assertIn(body, out)
            latch = checkoutwatch.latch_path(self.root)
            self.assertTrue(os.path.isfile(latch))
            self.assertFalse(latch.startswith(self.root + os.sep))
            rc, out, err = self.run_watch(
                "checkout-watch", "--repo", self.root, "--apply")
            self.assertEqual(rc, 0, err)
            self.assertEqual(post.call_count, 1)
            self.assertIn("unchanged", out)
            _git(self.root, "checkout", "--", "note.txt")
            rc, out, err = self.run_watch(
                "checkout-watch", "--repo", self.root, "--apply")
            self.assertEqual(rc, 0, err)
            self.assertEqual(post.call_count, 1)
            self.assertIn("clean", out)
            self.assertFalse(os.path.exists(latch))
            with open(path, "a") as fh:
                fh.write("again\n")
            rc, out, err = self.run_watch(
                "checkout-watch", "--repo", self.root, "--apply")
            self.assertEqual(rc, 0, err)
            self.assertEqual(post.call_count, 2)
            self.assertNotEqual(post.call_args.kwargs["event_id"], first_event)
            self.assertIn("note.txt", post.call_args[0][0])
            self.assertIn(checkoutwatch.FIX, post.call_args[0][0])

    def test_dry_run_names_the_dirt_and_does_not_post(self):
        with open(os.path.join(self.root, "note.txt"), "a") as fh:
            fh.write("dirty\n")
        with mock.patch("helm.chat.post") as post:
            rc, out, err = self.run_watch(
                "checkout-watch", "--repo", self.root)
        self.assertEqual(rc, 0, err)
        self.assertEqual(post.call_count, 0)
        self.assertIn("note.txt", out)
        self.assertIn(checkoutwatch.FIX, out)
        self.assertFalse(os.path.exists(checkoutwatch.latch_path(self.root)))

    def test_a_new_unignored_file_posts_and_an_ignored_one_does_not(self):
        fresh = os.path.join(self.root, "fresh.txt")
        with open(fresh, "w") as fh:
            fh.write("new\n")
        with mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 0)
        self.assertEqual(post.call_count, 1)
        self.assertIn("fresh.txt", post.call_args[0][0])
        self.assertIn("mtime %s" % int(os.stat(fresh).st_mtime),
                      post.call_args[0][0])
        ignore = os.path.join(self.root, ".gitignore")
        with open(ignore, "w") as fh:
            fh.write("skip.dat\n")
        _git(self.root, "add", ".gitignore")
        _git(self.root, "-c", "core.hooksPath=/dev/null", "commit", "-qm",
             "ignore")
        with open(os.path.join(self.root, "skip.dat"), "w") as fh:
            fh.write("secret\n")
        # The fresh file is still untracked. Put the tree back to clean
        # except for the ignored name, which porcelain must not report.
        os.remove(fresh)
        checkoutwatch._clear_latch(self.root)
        with mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(post.call_count, 0)
        self.assertIn("clean", lines[0])

    def test_an_unread_status_does_not_post(self):
        missing = tempfile.mkdtemp(prefix="helm-test-checkout-watch-gone-")
        os.rmdir(missing)
        with mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(missing)
        self.assertEqual(rc, 1)
        self.assertIn("unread", lines[0])
        self.assertEqual(post.call_count, 0)


def _gitdir(root):
    """The checkout's own git dir, as git names it: a linked worktree's
    `.git` is a file, so a join would name nothing."""
    return _git(root, "rev-parse", "--absolute-git-dir")


def _dirty(root, line="dirty\n"):
    with open(os.path.join(root, "note.txt"), "a") as fh:
        fh.write(line)


def _resolves(seat):
    """The resolver's answer, patched where seats_integrator reads it."""
    return mock.patch("helm.seats_integrator.integrator_seat",
                      return_value=(seat, None))


def _unresolved(why):
    return mock.patch("helm.seats_integrator.integrator_seat",
                      return_value=(None, why))


class CheckoutWatchAddressTest(unittest.TestCase):
    """Note 1: the alert wakes the seat that restores the checkout. The
    mention is the resolver's answer at post time, never a literal; the
    seat names below exist in no source file."""

    def setUp(self):
        # the watch names the checkout by its real path
        self.root = os.path.realpath(_repo())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _dirty(self.root)

    def test_the_alert_mentions_the_integrator_the_resolver_names(self):
        with _resolves("lane-3301-integrator"), \
                mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(post.call_count, 1)
        body = post.call_args[0][0]
        head = body.splitlines()[0]
        self.assertTrue(head.startswith("@lane-3301-integrator shared "
                                        "checkout "), head)
        self.assertEqual(body.count("@"), 1, body)
        self.assertNotIn("no integrator resolved", body)
        self.assertIn("note.txt", body)
        self.assertIn(checkoutwatch.FIX, body)
        # CONTROL: another resolver answer, another mention. A literal in
        # the source cannot follow it.
        checkoutwatch._clear_latch(self.root)
        with _resolves("other-lead-integrator"), \
                mock.patch("helm.chat.post") as post:
            checkoutwatch.watch(self.root, apply=True)
        self.assertTrue(post.call_args[0][0].startswith(
            "@other-lead-integrator "), post.call_args[0][0])

    def test_no_resolved_integrator_posts_anyway_and_says_so_in_the_line(self):
        with _unresolved("the roster names no integrator"), \
                mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(post.call_count, 1)
        body = post.call_args[0][0]
        head = body.splitlines()[0]
        self.assertIn("shared checkout %s is dirty" % self.root, head)
        self.assertIn("no integrator resolved: the roster names no "
                      "integrator", head)
        self.assertNotIn("@", body)
        self.assertIn("note.txt", body)

    def test_dry_run_prints_the_same_addressed_alert(self):
        with _resolves("lane-3301-integrator"), \
                mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(self.root)
            self.assertEqual(rc, 0, lines)
            self.assertEqual(post.call_count, 0)
            self.assertTrue(lines[0].startswith("@lane-3301-integrator "),
                            lines[0])
            # CONTROL on the same seam: --apply posts exactly that text.
            rc, applied = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 0, applied)
        self.assertEqual(post.call_count, 1, applied)
        self.assertEqual(post.call_args[0][0], lines[0])


class CheckoutWatchInFlightTest(unittest.TestCase):
    """Note 2: git's own markers of an operation in flight skip the tick.
    Nothing posts and the latch does not move; once the marker goes, the
    same dirt posts once."""

    MARKERS = (("MERGE_HEAD", False), ("CHERRY_PICK_HEAD", False),
               ("REVERT_HEAD", False), ("rebase-merge", True),
               ("rebase-apply", True))

    def _plant(self, gitdir, marker, is_dir):
        path = os.path.join(gitdir, marker)
        if is_dir:
            os.mkdir(path)
            return lambda: shutil.rmtree(path)
        with open(path, "w") as fh:
            fh.write("0" * 40 + "\n")
        return lambda: os.remove(path)

    def test_each_marker_skips_the_tick_and_leaves_the_latch(self):  # noqa: VACUOUS_ASSERTION — the loop is a constant five-row table and `reached` proves every row ran its post control
        reached = []
        for marker, is_dir in self.MARKERS:
            with self.subTest(marker=marker):
                root = _repo()
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                _dirty(root)
                latch = checkoutwatch.latch_path(root)
                checkoutwatch._write_latch(root, " M earlier.txt\n")
                remove = self._plant(_gitdir(root), marker, is_dir)
                with _resolves("lane-3301-integrator"), \
                        mock.patch("helm.chat.post") as post:
                    rc, lines = checkoutwatch.watch(root, apply=True)
                    self.assertEqual(rc, 0, lines)
                    self.assertEqual(post.call_count, 0, lines)
                    self.assertEqual(len(lines), 1, lines)
                    self.assertIn("skipped", lines[0])
                    self.assertIn(marker, lines[0])
                    with open(latch, encoding="utf-8") as fh:
                        self.assertEqual(fh.read(), " M earlier.txt\n")
                    # the dry run skips too, and says which marker
                    rc, lines = checkoutwatch.watch(root)
                    self.assertEqual(rc, 0, lines)
                    self.assertIn(marker, lines[0])
                    # CONTROL: the marker goes, the same dirt posts once
                    remove()
                    rc, lines = checkoutwatch.watch(root, apply=True)
                    self.assertEqual(rc, 0, lines)
                    self.assertEqual(post.call_count, 1, lines)
                    self.assertIn("note.txt", post.call_args[0][0])
                    rc, lines = checkoutwatch.watch(root, apply=True)
                    self.assertEqual(post.call_count, 1, lines)
                    self.assertIn("unchanged", lines[0])
                    reached.append(marker)
        self.assertEqual(reached, [m for m, _d in self.MARKERS])

    def test_a_skipped_tick_with_no_latch_leaves_none(self):
        root = _repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _dirty(root)
        remove = self._plant(_gitdir(root), "index.lock", False)
        latch = checkoutwatch.latch_path(root)
        with mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(root, apply=True)
            self.assertEqual((rc, post.call_count), (0, 0), lines)
            self.assertFalse(os.path.exists(latch))
            # CONTROL on the same two observables: the lock goes, the tick
            # posts and writes the latch.
            remove()
            rc, lines = checkoutwatch.watch(root, apply=True)
        self.assertEqual((rc, post.call_count), (0, 1), lines)
        self.assertTrue(os.path.isfile(latch))

    def test_a_stale_index_lock_alerts_once_instead_of_skipping_forever(self):
        root = _repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _dirty(root)
        lock = os.path.join(_gitdir(root), "index.lock")
        self._plant(_gitdir(root), "index.lock", False)
        old = time.time() - checkoutwatch.INDEX_LOCK_GRACE_S - 1
        os.utime(lock, (old, old))
        with mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(root, apply=True)
            self.assertEqual((rc, post.call_count), (1, 1), lines)
            body = post.call_args[0][0]
            self.assertIn("index.lock is stale (older than 240s)", body)
            rc, lines = checkoutwatch.watch(root, apply=True)
        self.assertEqual((rc, post.call_count), (1, 1), lines)
        self.assertIn("unchanged", lines[0])
        self.assertIn("index.lock is stale (older than 240s)", lines[0])

    def test_a_stale_sequencer_marker_alerts_once_with_dirt_present(self):  # noqa: VACUOUS_ASSERTION — the loop is a constant five-row table and `reached` proves every row posted
        """A conflicted sequencer left for hours is not an operation in
        flight. Past the same grace as index.lock, one stale alert posts
        even while the tree is dirty; the next tick posts nothing more."""
        reached = []
        for marker, is_dir in self.MARKERS:
            with self.subTest(marker=marker):
                root = _repo()
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                _dirty(root)
                path = os.path.join(_gitdir(root), marker)
                self._plant(_gitdir(root), marker, is_dir)
                old = time.time() - checkoutwatch.INDEX_LOCK_GRACE_S - 1
                os.utime(path, (old, old))
                with mock.patch("helm.chat.post") as post:
                    rc, lines = checkoutwatch.watch(root, apply=True)
                    self.assertEqual((rc, post.call_count), (1, 1), lines)
                    body = post.call_args[0][0]
                    self.assertIn("%s is stale (older than %ds)" % (
                        marker, checkoutwatch.INDEX_LOCK_GRACE_S), body)
                    rc, lines = checkoutwatch.watch(root, apply=True)
                self.assertEqual((rc, post.call_count), (1, 1), lines)
                self.assertIn("unchanged", lines[0])
                reached.append(marker)
        self.assertEqual(reached, [m for m, _d in self.MARKERS])

    def test_porcelain_does_not_refresh_the_index(self):  # noqa: VACUOUS_ASSERTION — the plain git status above is the unconditional positive control: it rewrites this index's mtime, and porcelain on the same index does not
        """A plain status rewrites .git/index to refresh stat info and takes
        index.lock while it does. The watch's status must not: a checkout
        write in that window fails on the lock. The plain status is the
        positive control on the same index mtime."""
        root = _repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        tracked = os.path.join(root, "note.txt")
        index = os.path.join(root, ".git", "index")
        now = time.time()
        os.utime(index, (now - 10, now - 10))
        os.utime(tracked, (now, now))
        plain_before = os.stat(index).st_mtime
        ran = subprocess.run(
            ["git", "-C", root, "status", "--porcelain",
             "--untracked-files=normal"],
            capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertNotEqual(os.stat(index).st_mtime, plain_before)
        os.utime(index, (now - 10, now - 10))
        os.utime(tracked, (now, now))
        before = os.stat(index).st_mtime
        rc, watched, err = checkoutwatch.porcelain(root)
        self.assertEqual(rc, 0, err)
        self.assertEqual(watched, ran.stdout)
        self.assertEqual(os.stat(index).st_mtime, before)

    def test_the_git_dir_is_the_one_git_names_not_a_dot_git_join(self):
        """A linked worktree's `.git` is a file: its markers live in the
        worktree's own git dir, which only git can name. The same marker in
        the main checkout's git dir does not belong to the worktree."""
        main = _repo()
        self.addCleanup(shutil.rmtree, main, ignore_errors=True)
        wt = main + "-wt"
        _git(main, "worktree", "add", "-q", "--detach", wt)
        self.addCleanup(shutil.rmtree, wt, ignore_errors=True)
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))
        _dirty(wt)
        remove = self._plant(_gitdir(wt), "MERGE_HEAD", False)
        with mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(wt, apply=True)
            self.assertEqual((rc, post.call_count), (0, 0), lines)
            self.assertIn("MERGE_HEAD", lines[0])
            # CONTROL: the marker moves to the MAIN checkout's git dir; the
            # worktree has no operation in flight, and its dirt posts.
            remove()
            self._plant(_gitdir(main), "MERGE_HEAD", False)
            rc, lines = checkoutwatch.watch(wt, apply=True)
        self.assertEqual((rc, post.call_count), (0, 1), lines)
        self.assertIn("note.txt", post.call_args[0][0])


class CheckoutWatchUnreadTest(unittest.TestCase):
    """Note 3: a checkout that git cannot read alerts, once per distinct
    failure, and keeps the nonzero rc for the timer's journal. The same
    failure posts nothing more; a different one posts; a clean read
    resets the latch."""

    def setUp(self):
        # the watch names the checkout by its real path
        self.root = os.path.realpath(_repo())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.index = os.path.join(_gitdir(self.root), "index")

    def _corrupt(self, data):
        with open(self.index, "wb") as fh:
            fh.write(data)

    def _mend(self):
        os.remove(self.index)
        _git(self.root, "reset", "-q")

    def test_one_alert_per_distinct_failure_then_a_clean_read_resets(self):
        timeout = (-1, "", "Command '['git', 'status']' timed out after "
                   "15 seconds")
        with _resolves("lane-3301-integrator"), \
                mock.patch("helm.chat.post") as post:
            self._corrupt(b"garbage")
            rc, lines = checkoutwatch.watch(self.root, apply=True)
            self.assertEqual(rc, 1, lines)
            self.assertEqual(post.call_count, 1, lines)
            body = post.call_args[0][0]
            self.assertTrue(body.startswith("@lane-3301-integrator shared "
                                            "checkout %s is unreadable"
                                            % self.root), body)
            self.assertIn("index file smaller than expected", body)
            self.assertIn(body, lines)
            # the same failure: nothing more, and still rc 1
            rc, lines = checkoutwatch.watch(self.root, apply=True)
            self.assertEqual(rc, 1, lines)
            self.assertEqual(post.call_count, 1, lines)
            self.assertIn("unchanged", lines[0])
            self.assertIn("index file smaller than expected", lines[0])
            # a different failure posts again
            self._corrupt(b"junk" * 16)
            rc, lines = checkoutwatch.watch(self.root, apply=True)
            self.assertEqual(rc, 1, lines)
            self.assertEqual(post.call_count, 2, lines)
            self.assertIn("index file corrupt", post.call_args[0][0])
            # a timeout is a failure too, and it dedupes on its own text
            with mock.patch.object(checkoutwatch, "porcelain",
                                   return_value=timeout):
                rc, lines = checkoutwatch.watch(self.root, apply=True)
                self.assertEqual(rc, 1, lines)
                self.assertEqual(post.call_count, 3, lines)
                self.assertIn("timed out after 15 seconds",
                              post.call_args[0][0])
                rc, lines = checkoutwatch.watch(self.root, apply=True)
                self.assertEqual((rc, post.call_count), (1, 3), lines)
            # a clean read resets: the first failure posts again after it
            self._mend()
            rc, lines = checkoutwatch.watch(self.root, apply=True)
            self.assertEqual(rc, 0, lines)
            self.assertIn("clean", lines[0])
            self.assertFalse(os.path.exists(
                checkoutwatch.latch_path(self.root)))
            self._corrupt(b"garbage")
            rc, lines = checkoutwatch.watch(self.root, apply=True)
            self.assertEqual((rc, post.call_count), (1, 4), lines)
            self.assertIn("index file smaller than expected",
                          post.call_args[0][0])

    def test_a_failure_then_dirt_posts_the_dirt(self):
        with mock.patch("helm.chat.post") as post:
            self._corrupt(b"garbage")
            rc, lines = checkoutwatch.watch(self.root, apply=True)
            self.assertEqual((rc, post.call_count), (1, 1), lines)
            self._mend()
            _dirty(self.root)
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual((rc, post.call_count), (0, 2), lines)
        self.assertIn("is dirty", post.call_args[0][0])
        self.assertIn("note.txt", post.call_args[0][0])

    def test_no_resolved_integrator_on_a_failure_says_so(self):
        with _unresolved("the roster is unreadable"), \
                mock.patch("helm.chat.post") as post:
            self._corrupt(b"garbage")
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual((rc, post.call_count), (1, 1), lines)
        head = post.call_args[0][0].splitlines()[0]
        self.assertIn("no integrator resolved: the roster is unreadable",
                      head)
        self.assertNotIn("@", head)

    def test_a_missing_checkout_posts_once_and_the_dry_run_never(self):
        missing = tempfile.mkdtemp(prefix="helm-test-checkout-watch-gone-")
        os.rmdir(missing)
        with mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(missing)
            self.assertEqual((rc, post.call_count), (1, 0), lines)
            self.assertIn("unreadable", lines[0])
            self.assertFalse(os.path.exists(
                checkoutwatch.latch_path(missing)))
            rc, lines = checkoutwatch.watch(missing, apply=True)
            self.assertEqual((rc, post.call_count), (1, 1), lines)
            rc, lines = checkoutwatch.watch(missing, apply=True)
            self.assertEqual((rc, post.call_count), (1, 1), lines)
        checkoutwatch._clear_latch(missing)


class CheckoutWatchLatchTest(unittest.TestCase):
    """The local transaction makes retries idempotent and clean resets safe."""

    def setUp(self):
        self.root = os.path.realpath(_repo())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_a_posted_alert_with_a_failed_finalize_reuses_its_event_id(self):
        _dirty(self.root)
        real_write = checkoutwatch._write_latch
        writes = []

        def flaky_write(root, text):
            writes.append(text)
            if len(writes) == 2:
                raise OSError("disk full")
            return real_write(root, text)

        logical_rows = {}

        def idempotent_post(body, room=None, who=None, event_id=None,
                            sign=None):
            self.assertEqual(who, checkoutwatch.POSTER)
            self.assertIs(sign, False)
            logical_rows.setdefault((room, who, event_id), body)
            return logical_rows[(room, who, event_id)]

        with _resolves("lane-3301-integrator") as resolver, \
                mock.patch.object(checkoutwatch, "_write_latch",
                                  side_effect=flaky_write), \
                mock.patch("helm.chat.post",
                           side_effect=idempotent_post) as post:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
            self.assertEqual(rc, 1, lines)
            self.assertIn("posted; latch not finalized (disk full)", lines[0])
            pending = checkoutwatch._read_pending(
                checkoutwatch._read_latch(self.root))
            self.assertIsNotNone(pending)
            first_event = post.call_args.kwargs["event_id"]
            first_room = post.call_args.kwargs["room"]
            first_body = post.call_args.args[0]
            self.assertEqual(pending[0], first_event)
            self.assertEqual(pending[3], first_room)

            # The operation key and prose are one durable logical event even
            # if dynamic addressing changes before the retry.
            resolver.return_value = ("other-integrator", None)
            with mock.patch.object(checkoutwatch, "_room",
                                   return_value="another-room"):
                rc, lines = checkoutwatch.watch(self.root, apply=True)
            self.assertEqual(rc, 0, lines)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args.kwargs["event_id"], first_event)
            self.assertEqual(post.call_args.kwargs["room"], first_room)
            self.assertEqual(post.call_args.args[0], first_body)
            self.assertEqual(len(logical_rows), 1)

            rc, lines = checkoutwatch.watch(self.root, apply=True)
            self.assertEqual(rc, 0, lines)
            self.assertEqual(post.call_count, 2)
            self.assertIn("unchanged", lines[0])

    def test_failed_latch_arm_posts_nothing(self):  # noqa: VACUOUS_ASSERTION — the unpatched control immediately below posts on the same dirty tree
        _dirty(self.root)
        with mock.patch("helm.pk.atomic_write",
                        side_effect=OSError("read only")), \
                mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 1, lines)
        self.assertEqual(post.call_count, 0)
        self.assertIn("not posted; latch not armed (read only)", lines[0])
        with mock.patch("helm.chat.post") as control:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual((rc, control.call_count), (0, 1), lines)

    def test_an_unreadable_or_malformed_latch_cannot_duplicate_a_post(self):  # noqa: VACUOUS_ASSERTION — deleting only the bad latch makes the same dirty tree post in the final control
        _dirty(self.root)
        with mock.patch.object(checkoutwatch, "_read_latch",
                               side_effect=OSError("permission denied")), \
                mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 1, lines)
        self.assertEqual(post.call_count, 0)
        self.assertIn("not posted; latch unreadable (permission denied)",
                      lines[0])

        checkoutwatch._write_latch(self.root, checkoutwatch.PENDING + "{")
        with mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 1, lines)
        self.assertEqual(post.call_count, 0)
        self.assertIn("pending latch is malformed", lines[0])
        os.remove(checkoutwatch.latch_path(self.root))
        with mock.patch("helm.chat.post") as control:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual((rc, control.call_count), (0, 1), lines)

    def test_failed_removal_leaves_a_clean_tombstone_not_suppression(self):
        _dirty(self.root)
        rc, state, err = checkoutwatch.porcelain(self.root)
        self.assertEqual(rc, 0, err)
        checkoutwatch._write_latch(self.root, state)
        _git(self.root, "checkout", "--", "note.txt")
        with mock.patch("helm.checkoutwatch.os.remove",
                        side_effect=OSError("busy")):
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 0, lines)
        self.assertIn("clean", lines[0])
        self.assertEqual(checkoutwatch._read_latch(self.root),
                         checkoutwatch.CLEAN)

        _dirty(self.root)
        with mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual((rc, post.call_count), (0, 1), lines)
        self.assertIn("note.txt", post.call_args[0][0])

    def test_failed_latch_inspection_is_loud_and_not_clean(self):  # noqa: VACUOUS_ASSERTION — the unpatched control clears the same latch and reports clean
        checkoutwatch._write_latch(self.root, " M old.txt\n")
        with mock.patch("helm.checkoutwatch.os.lstat",
                        side_effect=OSError("permission denied")), \
                mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 1, lines)
        self.assertEqual(post.call_count, 0)
        self.assertIn("latch inspect failed (permission denied)", lines[0])
        self.assertNotIn("checkout-watch: clean", lines[0])
        rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 0, lines)
        self.assertIn("clean", lines[0])

    def test_failed_tombstone_and_removal_is_loud_and_not_clean(self):  # noqa: VACUOUS_ASSERTION — the unpatched control clears the same retained latch and reports clean
        checkoutwatch._write_latch(self.root, " M old.txt\n")
        with mock.patch("helm.pk.atomic_write",
                        side_effect=OSError("read only")), \
                mock.patch("helm.checkoutwatch.os.remove",
                           side_effect=OSError("busy")), \
                mock.patch("helm.chat.post") as post:
            rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 1, lines)
        self.assertEqual(post.call_count, 0)
        self.assertIn("latch clear failed (read only; busy)", lines[0])
        self.assertNotIn("checkout-watch: clean", lines[0])
        self.assertEqual(checkoutwatch._read_latch(self.root), " M old.txt\n")
        rc, lines = checkoutwatch.watch(self.root, apply=True)
        self.assertEqual(rc, 0, lines)
        self.assertIn("clean", lines[0])


class CheckoutWatchTimerTest(unittest.TestCase):
    def setUp(self):
        self.fake = fake_user_systemd(self)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cmd_work(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_the_unit_is_two_minutes_and_the_off_switch_writes_nothing(self):
        fake = self.fake
        self.assertEqual(checkoutwatch.INTERVAL_S, 120)
        service_path, service, timer_path, timer = checkoutwatch.timer_units()
        self.assertEqual(service_path, os.path.join(
            fake.unit_dir, checkoutwatch.SERVICE_NAME))
        self.assertEqual(timer_path, os.path.join(
            fake.unit_dir, checkoutwatch.TIMER_NAME))
        self.assertIn("OnUnitActiveSec=120", timer)
        self.assertIn("work checkout-watch --apply --repo ", service)
        from helm import work
        here = os.path.dirname(os.path.dirname(
            os.path.abspath(checkoutwatch.__file__)))
        cwd = work.find_root(here) or here
        self.assertIn("WorkingDirectory=%s" % cwd, service)
        self.assertIn("--repo %s" % cwd, service)
        os.environ[checkoutwatch.TIMER_ENV] = "0"
        ok, detail = checkoutwatch.ensure_timer()
        self.assertIsNone(ok)
        self.assertEqual(detail, "install skipped by HELM_CHECKOUT_WATCH_TIMER"
                         "=0: no unit file written, no systemctl run")
        self.assertFalse(os.path.exists(fake.unit_dir))
        self.assertEqual(fake.calls(), [])
        # POSITIVE CONTROL on the same two observables: with the switch gone
        # the same call writes both units and runs systemctl twice.
        del os.environ[checkoutwatch.TIMER_ENV]
        ok, detail = checkoutwatch.ensure_timer()
        self.assertIs(ok, True, detail)
        self.assertEqual(sorted(os.listdir(fake.unit_dir)),
                         [checkoutwatch.SERVICE_NAME, checkoutwatch.TIMER_NAME])
        self.assertEqual(fake.calls(), [_RELOAD, _ENABLE])

    def test_install_timer_through_the_verb(self):
        """`helm work checkout-watch --install-timer` end to end, on a dirty
        repo it must never watch: switched off it writes and runs nothing; on,
        it writes both units, reloads and enables; again, it rewrites nothing
        and only enables; a failing systemctl is rc 1 in the installer's
        words. Nothing posts until the control: --apply on the same repo."""
        fake = self.fake
        root = _repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, "note.txt"), "a") as fh:
            fh.write("dirty\n")
        _sp, service, timer_path, timer = checkoutwatch.timer_units()
        self.assertIn("OnUnitActiveSec=120", timer)
        self.assertIn("work checkout-watch --apply --repo ", service)
        chat_post = mock.patch("helm.chat.post")
        post = chat_post.start()
        self.addCleanup(chat_post.stop)
        install = ("checkout-watch", "--repo", root, "--install-timer")

        os.environ[checkoutwatch.TIMER_ENV] = "0"
        rc, out, err = self.run_cli(*install)
        self.assertEqual(rc, 0, err)
        self.assertIn("no unit file written", out)
        self.assertFalse(os.path.exists(fake.unit_dir))
        self.assertEqual(fake.calls(), [])

        del os.environ[checkoutwatch.TIMER_ENV]
        rc, out, err = self.run_cli(*install)
        self.assertEqual(rc, 0, err)
        self.assertIn("helm work checkout-watch: installed every 120s (%s)\n"
                      % timer_path, out)
        self.assertEqual(sorted(os.listdir(fake.unit_dir)),
                         [checkoutwatch.SERVICE_NAME, checkoutwatch.TIMER_NAME])
        with open(timer_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), timer)
        with open(os.path.join(fake.unit_dir, checkoutwatch.SERVICE_NAME),
                  encoding="utf-8") as fh:
            self.assertEqual(fh.read(), service)
        self.assertEqual(fake.calls(), [_RELOAD, _ENABLE])

        rc, out, err = self.run_cli(*install)
        self.assertEqual(rc, 0, err)
        self.assertIn("helm work checkout-watch: already installed, "
                      "unchanged, every 120s (%s)\n" % timer_path, out)
        self.assertEqual(fake.calls()[2:], [_ENABLE])

        failing = fake_user_systemd(self, rc=1, stderr="no bus\n", home=False)
        rc, out, err = self.run_cli(*install)
        self.assertEqual(rc, 1)
        self.assertIn("helm work checkout-watch: %s --user enable --now %s "
                      "failed: no bus\n"
                      % (failing.systemctl, checkoutwatch.TIMER_NAME), err)
        self.assertEqual(failing.calls(), [_ENABLE])

        self.assertEqual(post.call_count, 0)
        rc, out, err = self.run_cli("checkout-watch", "--repo", root,
                                    "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(post.call_count, 1)
        self.assertIn("note.txt", post.call_args[0][0])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
