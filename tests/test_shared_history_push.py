#!/usr/bin/env python3
"""The shared-history rung of the managed pre-push hook, through real pushes.

THE CLASS. A seat in a worktree of the shared checkout ran `git push
aspublic <branch>`. The public repository's main is a line of release commits
that shares no history with the private repository, so that one push
published the whole private development history, and no rung refused it: the
host-path scan reads blob content, never whether the destination has ever
seen this history.

THE RUNG. A push is refused when it would put on the destination a root
commit the destination does not already hold (`git rev-list --max-parents=0
<pushed> --not <held>`), or when it shares no history with the destination's
default branch and carries a commit the destination does not hold. The root
question catches the shapes an ancestor check passes: a merge that joins the
private history onto the public main, either way round. A destination with no
branch at all (a brand-new repository) takes a first push when it is private
or not on GitHub, and refuses one when it is public or its visibility cannot
be read. The release tool's own publish passes on a marker naming the one
commit it publishes. A push whose destination is not proven (steered off the
fetch URL, or its argv, remote config or URL unreadable) is refused before
any remote-tracking ref is read. No remote-tracking ref is ever evidence:
when a proven destination's default branch cannot be read (ls-remote fails,
no HEAD is advertised, or its tip is not in this repo), the rung refuses
every push the destination's own advertisement does not show it holds in
full, and a side ref it advertises proves nothing about the default branch.

Every arm drives a real `git push` through the hook the shipped installer
writes (`helm work install-guard --apply --profile leak`, through the `helm
work` dispatcher) into bare repositories on local paths:

  * "origin", the private repository, holding the project's history;
  * "aspublic", the public repository, whose main is an unrelated release
    line, fetched once so the incident repo's tracking refs exist;
  * fresh and otherwise shaped repositories where an arm needs one.

Git config, HOME and HELM_HOME are temporary, and the `helm` on PATH is a
stub that records the refusal counter's call, so nothing reaches the estate
or a network.
"""
import contextlib
import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-sh-", var="HELM_HOME")

from helm import autoland, hostpath_guard, wiring, work  # noqa: E402
from helm.work import _guard  # noqa: E402
from tests import test_release_tool as trt  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNG = "[helm shared-history]"
MARKER = "HELM_RELEASE_PUBLISH"
DOOR = ("the release publish: python3 scripts/release/release.py <version> "
        "--publish")
# The private history the lane carries: these commits on main, and one more
# on the lane itself.
PRIVATE_COMMITS = 5
LANE_COMMITS = PRIVATE_COMMITS + 1
# Words that would hand a seat a way around the rung. None may appear in what
# the rung prints: its one door is the owner's release publish.
BYPASSES = (MARKER, "--no-verify", "HELM_HOSTPATH_SKIP", "SKIP=")


def rung_lines(text):
    """The lines the shared-history rung printed."""
    return "\n".join(ln for ln in text.splitlines() if ln.startswith(RUNG))


class _Sandbox(unittest.TestCase):
    """The incident's world: a project repo with a private origin and a
    public `aspublic` whose history it does not share, and the managed
    pre-push installed by the real installer."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-sh-push-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = os.path.join(self.tmp, "bin")
        home = os.path.join(self.tmp, "home")
        os.makedirs(bin_dir)
        os.makedirs(home)
        self.counted = os.path.join(self.tmp, "counted.txt")
        with open(os.path.join(bin_dir, "helm"), "w") as f:
            f.write('#!/bin/sh\necho "$@" >> %s\nexit 0\n'
                    % shlex.quote(self.counted))
        os.chmod(os.path.join(bin_dir, "helm"), 0o755)
        needles = os.path.join(self.tmp, "needles.txt")
        with open(needles, "w") as f:
            f.write("zz-synthetic-shared-history-needle\n")
        env = mock.patch.dict(os.environ, {
            "HOME": home,
            "HELM_HOME": os.path.join(self.tmp, "helm"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_CHAT_NODE_URL": "", "HELM_CHAT_LOG": "0",
            "HELM_LANDLOCK": "0", "HELM_PRIVATE_NEEDLES": needles,
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": REPO})
        env.start()
        self.addCleanup(env.stop)
        # popped INSIDE the patch, so stopping it restores whatever was set
        for key in ("HELM_HOSTPATH_SKIP", MARKER):
            os.environ.pop(key, None)
        self.origin = self.bare("private.git")
        self.public = self.bare("public.git")
        # The public release line, built in a repository of its own.
        self.release = os.path.join(self.tmp, "release")
        self.ok(self.tmp, "init", "-q", "-b", "main", self.release)
        self.ident(self.release)
        self.write(self.release, "README", "helm 0.1.0\n")
        self.public_main = self.commit(self.release, "helm 0.1.0")
        self.ok(self.release, "push", "-q", self.public, "main")
        # The project: its own private history on origin.
        self.root = os.path.join(self.tmp, "proj")
        self.ok(self.tmp, "init", "-q", "-b", "main", self.root)
        self.ident(self.root)
        self.ok(self.root, "remote", "add", "origin", self.origin)
        for n in range(1, PRIVATE_COMMITS + 1):
            self.write(self.root, "notes.md", "private work %d\n" % n)
            self.main = self.commit(self.root, "private work %d" % n)
        self.ok(self.root, "push", "-q", "origin", "main")
        self.ok(self.root, "remote", "add", "aspublic", self.public)
        self.ok(self.root, "fetch", "-q", "aspublic")
        rc, _out, err = self.cli("install-guard", "--apply", "--profile",
                                 "leak")
        self.assertEqual(rc, 0, err)
        self.hook = _guard.hook_path(self.root, "pre-push")
        self.assertTrue(os.access(self.hook, os.X_OK))
        # The lane: one more private commit on a branch of main.
        self.ok(self.root, "checkout", "-q", "-b", "lane")
        self.write(self.root, "lane.md", "lane work\n")
        self.lane = self.commit(self.root, "lane work")

    # -- helpers ---------------------------------------------------------

    def git(self, cwd, *args, env=None):
        return subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                              text=True, timeout=120,
                              env=None if env is None
                              else dict(os.environ, **env))

    def ok(self, cwd, *args):
        r = self.git(cwd, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def bare(self, name):
        path = os.path.join(self.tmp, name)
        self.ok(self.tmp, "init", "-q", "--bare", "-b", "main", path)
        return path

    def ident(self, repo):
        self.ok(repo, "config", "user.email", "t@example.com")
        self.ok(repo, "config", "user.name", "t")

    def write(self, repo, rel, content):
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def commit(self, repo, msg):
        """--no-verify: the leak profile's pre-commit is not the subject."""
        self.ok(repo, "add", "-A")
        self.ok(repo, "commit", "-q", "--no-verify", "-m", msg)
        return self.ok(repo, "rev-parse", "HEAD")

    def cli(self, *args):
        """The shipped `helm work` dispatcher, never install_guard directly."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def push(self, *args, env=None):
        return self.git(self.root, "push", *args, env=env)

    def ref(self, repo, name):
        return self.git(repo, "rev-parse", "--verify", "-q",
                        name).stdout.strip()

    def feature(self, name="feature"):
        """A branch of the public main with one commit of its own: the shape
        of a push to a public repository whose history this repo shares."""
        self.ok(self.root, "checkout", "-q", "-b", name,
                "refs/remotes/aspublic/main")
        self.write(self.root, "%s.md" % name, "public-side work\n")
        return self.commit(self.root, "public-side work")

    def join_on_public_main(self, name="joined"):
        """F2a: a branch of the public main that merges the private lane in.
        It shares history with the public main, and it carries the whole
        private history with it."""
        self.ok(self.root, "checkout", "-q", "-b", name,
                "refs/remotes/aspublic/main")
        self.ok(self.root, "merge", "-q", "--no-verify", "--no-edit",
                "--allow-unrelated-histories", "-m", "join the lane", "lane")
        return self.ok(self.root, "rev-parse", "HEAD")

    def merge_public_main_into_lane(self):
        """F2b: the `git pull aspublic main` reflex, run in the lane."""
        self.ok(self.root, "checkout", "-q", "lane")
        self.ok(self.root, "merge", "-q", "--no-verify", "--no-edit",
                "--allow-unrelated-histories", "-m", "pull the public main",
                "refs/remotes/aspublic/main")
        return self.ok(self.root, "rev-parse", "HEAD")

    def cli_in(self, repo, *args):
        """The shipped `helm work` dispatcher, on another repository."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", repo])
        return rc, out.getvalue(), err.getvalue()

    def assertRefused(self, r, count, roots=1):
        """The refusal: rc 1, the count of new roots, the unshared count, the
        owner's one door, and no word that would hand a seat a bypass."""
        ours = rung_lines(r.stderr)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("%s REFUSED:" % RUNG, ours)
        self.assertIn("carries %d new root commit(s) that" % roots if roots
                      else "carries no new root commit", ours)
        self.assertIn("it would publish %d commit(s) that" % count, ours)
        self.assertIn(DOOR, ours)
        for word in BYPASSES:
            self.assertNotIn(word, ours)
        return ours


class TheIncidentTest(_Sandbox):
    """destination x {shares history, disjoint, brand-new, unreadable} for an
    ordinary pusher with the hook installed."""

    def test_pushing_the_private_lane_to_the_disjoint_public_repo_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the public main pinned to its non-empty seed sha and the pinned refusal
        r = self.push("aspublic", "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("REFUSED: this push shares no history with the default "
                      "branch main of 'aspublic' (tip %s)"
                      % self.public_main[:12], ours)
        self.assertIn("(pushed tip %s)" % self.lane[:12], ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "",
                         "the private lane must not reach the public repo")
        self.assertEqual(self.ref(self.public, "refs/heads/main"),
                         self.public_main)
        # The refusal is counted under the rung's own name.
        with open(self.counted) as f:
            self.assertIn("friction record shared-history", f.read())

    def test_the_same_push_by_url_names_no_byte_of_the_url(self):  # noqa: VACUOUS_ASSERTION — the absent path pieces are read against the refusal pinned in the same lines
        r = self.push(self.public, "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("default branch main of a URL remote", ours)
        for piece in (self.public, self.tmp, "public.git"):
            self.assertNotIn(piece, ours)

    def test_an_ordinary_push_to_the_private_origin_passes(self):  # noqa: VACUOUS_ASSERTION — the absent warning is read beside the pinned pass line and the pushed sha
        self.ok(self.root, "checkout", "-q", "main")
        self.write(self.root, "train.md", "a train\n")
        head = self.commit(self.root, "a train")
        r = self.push("origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.origin, "refs/heads/main"), head)
        ours = rung_lines(r.stderr)
        self.assertIn("'origin': shares history with its default branch main, "
                      "and carries no new root commit", ours)
        self.assertNotIn("WARNING", ours)

    def test_a_new_lane_to_the_private_origin_passes(self):  # noqa: VACUOUS_ASSERTION — the returncode 0 is read beside the origin branch pinned to the non-empty lane sha
        r = self.push("origin", "lane")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.origin, "refs/heads/lane"), self.lane)

    def test_a_push_sharing_the_public_main_passes(self):  # noqa: VACUOUS_ASSERTION — the returncode 0 is read beside the public branch pinned to the non-empty pushed sha and the pinned pass line
        """The shape of a push to a public repository this repo shares
        history with, as a trusted seat's push to an upstream it forked."""
        head = self.feature()
        r = self.push("aspublic", "feature")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.public, "refs/heads/feature"), head)
        self.assertIn("'aspublic': shares history with its default branch "
                      "main", rung_lines(r.stderr))

    def test_a_brand_new_empty_repository_takes_its_first_push(self):  # noqa: VACUOUS_ASSERTION — the returncode 0 is read beside the fresh branch pinned to the non-empty lane sha and the pinned pass line
        fresh = self.bare("fresh.git")
        self.ok(self.root, "remote", "add", "fresh", fresh)
        r = self.push("fresh", "lane")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(fresh, "refs/heads/lane"), self.lane)
        self.assertIn("'fresh' has no branch yet (a brand-new repository), "
                      "and it is not a GitHub remote: passed",
                      rung_lines(r.stderr))

    def test_a_disjoint_branch_the_destination_already_holds_passes_with_a_warning(self):  # noqa: VACUOUS_ASSERTION — the returncode 0 is read beside the copy pinned to the non-empty side sha, the emptied tracking refs and the pinned warning
        """Nothing new is published: the destination already holds every
        commit the push carries, on another of its branches. Its own
        advertisement says so: every remote-tracking ref of it is deleted
        first, and the push still passes."""
        orphan = os.path.join(self.tmp, "orphan")
        self.ok(self.tmp, "init", "-q", "-b", "side", orphan)
        self.ident(orphan)
        self.write(orphan, "side.md", "a side line\n")
        side = self.commit(orphan, "a side line")
        self.ok(orphan, "push", "-q", self.public, "side")
        self.ok(self.root, "fetch", "-q", "aspublic")
        self.ok(self.root, "branch", "side", "refs/remotes/aspublic/side")
        for ref in self.ok(self.root, "for-each-ref", "--format=%(refname)",
                           "refs/remotes/aspublic/").split():
            self.ok(self.root, "update-ref", "--no-deref", "-d", ref)
        self.assertEqual(self.ok(self.root, "for-each-ref",
                                 "refs/remotes/aspublic/"), "")
        r = self.push("aspublic", "side:refs/heads/copy")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.public, "refs/heads/copy"), side)
        self.assertIn("WARNING: this push shares no history with the default "
                      "branch main of 'aspublic', but 'aspublic' already "
                      "holds every commit it carries", rung_lines(r.stderr))

    def test_a_tag_of_the_private_lane_is_judged_by_its_commit(self):  # noqa: VACUOUS_ASSERTION — the absent public tag is read beside the pinned refusal
        self.ok(self.root, "tag", "-a", "-m", "a private tag", "v9", "lane")
        r = self.push("aspublic", "v9")
        self.assertRefused(r, LANE_COMMITS)
        self.assertEqual(self.ref(self.public, "refs/tags/v9"), "")

    def test_a_tag_of_a_tree_is_refused_until_the_destination_holds_it(self):  # noqa: VACUOUS_ASSERTION — the absent public tag and tree are read beside the pinned refusal, and the same push passes once the destination holds the object
        """A tag that peels to no commit carries the tree's every blob with
        no history to judge it by: refused, unless the destination already
        advertises that very object (then nothing new is published)."""
        tree = self.ok(self.root, "rev-parse", "main^{tree}")
        self.ok(self.root, "tag", "-a", "-m", "a tree", "snapshot", tree)
        tag = self.ok(self.root, "rev-parse", "snapshot")
        r = self.push("aspublic", "snapshot")
        ours = rung_lines(r.stderr)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("REFUSED: %s carries no commit (a tag of a tree or a "
                      "blob): its content has no history to judge by, and "
                      "'aspublic' does not hold it" % tag[:12], ours)
        self.assertIn(DOOR, ours)
        for word in BYPASSES:
            self.assertNotIn(word, ours)
        self.assertEqual(self.ref(self.public, "refs/tags/snapshot"), "")
        self.assertNotEqual(self.git(self.public, "cat-file", "-e",
                                     tree).returncode, 0)
        # The destination comes to hold that object by another clone's
        # push (the release repo carries no hook), and the same push passes.
        self.ok(self.release, "fetch", "-q", self.root, "refs/tags/snapshot:"
                "refs/tags/snapshot")
        self.ok(self.release, "push", "-q", self.public, "refs/tags/snapshot")
        r = self.push("aspublic", "refs/tags/snapshot:refs/tags/snapshot2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("%s carries no commit: 'aspublic' already holds it"
                      % tag[:12], rung_lines(r.stderr))
        self.assertEqual(self.ref(self.public, "refs/tags/snapshot2"), tag)


class UnreadableDefaultBranchTest(_Sandbox):
    """The default branch cannot be read: the rung names why and refuses
    every push the destination's own advertisement does not show it holds in
    full. No remote-tracking ref stands in for that branch, and a side ref
    the destination advertises says nothing of the default branch's history,
    so neither passes a push that adds a commit (helm-codex, cross-family
    door read of 7411626e6ba: the fallback those refs fed passed a push its
    destination could not be read for). A push whose every commit the
    destination already advertises publishes nothing and passes with a
    warning."""

    DETAIL = ("so this push cannot be checked against that branch: no "
              "remote-tracking ref stands in for it, and only a push whose "
              "every commit %s advertises passes")

    def no_default_branch(self):
        """The public repository keeps its branches, and its HEAD names a
        branch that does not exist, so it advertises no default branch."""
        self.ok(self.public, "symbolic-ref", "HEAD", "refs/heads/gone")

    def test_a_destination_naming_no_default_branch_is_named_and_the_lane_refused(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the pinned refusal
        self.no_default_branch()
        r = self.push("aspublic", "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("REFUSED: the default branch of 'aspublic' cannot be "
                      "read (it advertises no default branch), and this push "
                      "carries 1 new root commit(s) that 'aspublic' is not "
                      "known to hold", ours)
        self.assertIn("the destination advertises no default branch, "
                      + self.DETAIL % "'aspublic'", ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")

    def test_a_branch_of_its_main_is_refused_while_no_default_branch_is_named(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the pinned refusal, and the copy pinned to the non-empty public main is the pass control
        """The public main is advertised and here, and the tracking ref names
        it, so every root of a branch of it is held: the fallback this arm
        replaces passed it with a warning. Its link to the default branch
        cannot be checked and it adds a commit, so it is refused. The
        control: a push of a commit the destination already advertises,
        with every tracking ref of it deleted, adds none and passes."""
        self.no_default_branch()
        self.feature()
        r = self.push("aspublic", "feature")
        ours = self.assertRefused(r, 1, roots=0)
        self.assertIn("REFUSED: the default branch of 'aspublic' cannot be "
                      "read (it advertises no default branch), and this push "
                      "carries no new root commit, but carries commits "
                      "'aspublic' is not known to hold", ours)
        self.assertNotIn("shares history with those refs", ours)
        self.assertEqual(self.ref(self.public, "refs/heads/feature"), "")
        for ref in self.ok(self.root, "for-each-ref", "--format=%(refname)",
                           "refs/remotes/aspublic/").split():
            self.ok(self.root, "update-ref", "--no-deref", "-d", ref)
        self.assertEqual(self.ok(self.root, "for-each-ref",
                                 "refs/remotes/aspublic/"), "")
        r = self.push("aspublic", "%s:refs/heads/copy" % self.public_main)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.public, "refs/heads/copy"),
                         self.public_main)
        self.assertIn("WARNING: the default branch of 'aspublic' cannot be "
                      "read (it advertises no default branch), but 'aspublic' "
                      "already holds every commit this push carries: passed",
                      rung_lines(r.stderr))

    def test_a_side_ref_holding_the_private_root_proves_nothing_of_the_default_branch(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the pinned refusal and the leaked branch pinned to the non-empty private main
        """An earlier leak left the private main on a side branch of the
        public repository, whose HEAD then names no branch. Every root of the
        lane is held there, so a judgement by roots alone passed the one
        private commit the lane adds. The default branch's history cannot be
        read, and the push is refused."""
        self.ok(self.origin, "push", "-q", self.public,
                "main:refs/heads/leaked")
        self.assertEqual(self.ref(self.public, "refs/heads/leaked"), self.main)
        self.no_default_branch()
        r = self.push("aspublic", "lane")
        ours = self.assertRefused(r, 1, roots=0)
        self.assertIn("(it advertises no default branch), and this push "
                      "carries no new root commit", ours)
        self.assertIn("(pushed tip %s)" % self.lane[:12], ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")

    def test_a_destination_not_proven_to_be_the_queried_one_is_refused_as_steered(self):  # noqa: VACUOUS_ASSERTION — the absent public branches are read beside the pinned refusal, for the lane and for a branch of the public main alike
        """A pushurl that differs from the url: what ls-remote would read is
        not proven to be where the pack goes, and the remote-tracking refs
        describe the fetch URL, not the pushurl. Nothing is evidence, so a
        branch of the public main is refused like the lane."""
        self.ok(self.root, "config", "remote.aspublic.pushurl",
                "file://" + self.public)
        r = self.push("aspublic", "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("cannot be read (push destination not proven to be the "
                      "queried one (a pushurl that differs from url))", ours)
        self.assertNotIn("judged by the remote-tracking refs", ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")
        self.feature()
        r = self.push("aspublic", "feature")
        ours = self.assertRefused(r, 2)
        self.assertIn("the remote-tracking refs of 'aspublic' describe its "
                      "fetch URL, not where this push goes", ours)
        self.assertEqual(self.ref(self.public, "refs/heads/feature"), "")

    def test_nothing_to_judge_by_refuses(self):  # noqa: VACUOUS_ASSERTION — the absent branch is read beside the pinned refusal
        """A URL remote whose HEAD names no branch, and none of what it
        advertises is here: nothing could show a shared history."""
        other = self.bare("other.git")
        seed = os.path.join(self.tmp, "seed")
        self.ok(self.tmp, "init", "-q", "-b", "main", seed)
        self.ident(seed)
        self.write(seed, "x.md", "another project\n")
        self.commit(seed, "another project")
        self.ok(seed, "push", "-q", other, "main")
        self.ok(other, "symbolic-ref", "HEAD", "refs/heads/gone")
        r = self.push(other, "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("cannot be read (it advertises no default branch), and "
                      "this push carries 1 new root commit(s) that a URL "
                      "remote is not known to hold", ours)
        self.assertIn("the destination advertises no default branch, "
                      + self.DETAIL % "a URL remote", ours)
        self.assertEqual(self.ref(other, "refs/heads/lane"), "")

    def test_a_default_branch_tip_not_here_refuses_until_it_is_fetched(self):  # noqa: VACUOUS_ASSERTION — the absent public branches are read beside the pinned refusals, and the feature pinned to its non-empty sha after the fetch is the pass control
        """The public main moved on in another clone and was never fetched
        here. Its tracking ref, as last fetched, is no evidence of what the
        destination holds now: a branch of the old main is refused like the
        lane, and passes once the new tip is fetched."""
        self.write(self.release, "README", "helm 0.1.1\n")
        moved = self.commit(self.release, "helm 0.1.1")
        self.ok(self.release, "push", "-q", self.public, "main")
        self.assertNotEqual(self.git(self.root, "cat-file", "-e",
                                     moved).returncode, 0)
        r = self.push("aspublic", "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("REFUSED: the default branch of 'aspublic' cannot be "
                      "read (its tip is not in this repo), and this push "
                      "carries 1 new root commit(s) that 'aspublic' is not "
                      "known to hold", ours)
        self.assertIn("the tip of that branch is not in this repo until it is "
                      "fetched, " + self.DETAIL % "'aspublic'", ours)
        self.assertNotIn("as last fetched", ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")
        head = self.feature()
        r = self.push("aspublic", "feature")
        ours = self.assertRefused(r, 2)
        self.assertNotIn("as last fetched", ours)
        self.assertEqual(self.ref(self.public, "refs/heads/feature"), "")
        self.ok(self.root, "fetch", "-q", "aspublic")
        r = self.push("aspublic", "feature")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.public, "refs/heads/feature"), head)
        self.assertIn("'aspublic': shares history with its default branch "
                      "main, and carries no new root commit",
                      rung_lines(r.stderr))


class JoinedHistoryTest(_Sandbox):
    """F2: a push that JOINS the private history onto the public main shares
    history with that main, so an ancestor check passes it, and it still
    publishes the whole private history. What it adds is a root commit the
    destination does not hold."""

    def test_a_merge_of_the_lane_onto_the_public_main_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the public main pinned to its non-empty seed sha and the pinned refusal
        joined = self.join_on_public_main()
        r = self.push("aspublic", "joined")
        ours = self.assertRefused(r, LANE_COMMITS + 1)
        self.assertIn("REFUSED: this push carries 1 new root commit(s) that "
                      "'aspublic' does not hold", ours)
        self.assertIn("it joins that history to the default branch main of "
                      "'aspublic' (tip %s) through a merge"
                      % self.public_main[:12], ours)
        self.assertIn("(pushed tip %s)" % joined[:12], ours)
        self.assertEqual(self.ref(self.public, "refs/heads/joined"), "")
        self.assertEqual(self.ref(self.public, "refs/heads/main"),
                         self.public_main)
        self.assertNotEqual(self.git(self.public, "cat-file", "-e",
                                     self.lane).returncode, 0,
                            "no private commit reached the public repo")

    def test_a_lane_that_merged_the_public_main_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the pinned refusal
        merged = self.merge_public_main_into_lane()
        r = self.push("aspublic", "lane")
        ours = self.assertRefused(r, LANE_COMMITS + 1)
        self.assertIn("REFUSED: this push carries 1 new root commit(s) that "
                      "'aspublic' does not hold", ours)
        self.assertIn("(pushed tip %s)" % merged[:12], ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")

    def test_several_refs_are_refused_whole_when_one_starts_a_history(self):  # noqa: VACUOUS_ASSERTION — the absent public branches are read beside the pinned refusal naming the one offending tip
        clean = self.feature()
        joined = self.join_on_public_main()
        r = self.push("aspublic", "feature", "joined")
        ours = self.assertRefused(r, LANE_COMMITS + 1)
        self.assertIn("(pushed tip %s)" % joined[:12], ours)
        self.assertNotIn(clean[:12], ours,
                         "the tip that starts no history is not named")
        self.assertEqual(self.ref(self.public, "refs/heads/feature"), "")
        self.assertEqual(self.ref(self.public, "refs/heads/joined"), "")

    def test_a_joined_push_under_a_pushurl_is_refused_as_steered(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the pinned refusal
        """A pushurl: the default branch cannot be read, the push is steered,
        and nothing is evidence: both roots of the merged lane are new and
        every commit it carries counts, the public main included."""
        self.ok(self.root, "config", "remote.aspublic.pushurl",
                "file://" + self.public)
        self.merge_public_main_into_lane()
        r = self.push("aspublic", "lane")
        ours = self.assertRefused(r, LANE_COMMITS + 2, roots=2)
        self.assertIn("REFUSED: the default branch of 'aspublic' cannot be "
                      "read (push destination not proven to be the queried "
                      "one (a pushurl that differs from url)), and this push "
                      "carries 2 new root commit(s) that 'aspublic' is not "
                      "known to hold", ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")

    def test_an_ls_remote_that_fails_refuses_even_a_train(self):  # noqa: VACUOUS_ASSERTION — each verdict is a pinned bool read beside the pinned lines of the same call
        """ls-remote cannot read a proven destination (a path with no
        repository behind it): nothing says what it holds, and no
        remote-tracking ref stands in for it, so a train is refused like the
        incident shape and the joined shape, and every commit counts, the
        public main's too."""
        missing = os.path.join(self.tmp, "missing.git")
        self.ok(self.root, "checkout", "-q", "main")
        self.write(self.root, "train.md", "a train\n")
        train = self.commit(self.root, "a train")
        lane = self.lane
        merged = self.merge_public_main_into_lane()
        with mock.patch.object(hostpath_guard, "_push_argv",
                               return_value=["git", "push"]):
            for name, tip, count, roots in (
                    ("origin", train, PRIVATE_COMMITS + 1, 1),
                    ("aspublic", lane, LANE_COMMITS, 1),
                    ("aspublic", merged, LANE_COMMITS + 2, 2)):
                with self.subTest(name=name, tip=tip):
                    refused, lines = hostpath_guard.judge_push(
                        self.root, name, missing, [tip])
                    self.assertTrue(refused, lines)
                    text = "\n".join(lines)
                    self.assertIn("REFUSED: the default branch of '%s' cannot "
                                  "be read (not reachable), and this push "
                                  "carries %d new root commit(s) that '%s' is "
                                  "not known to hold" % (name, roots, name),
                                  text)
                    self.assertIn("the destination cannot be read, so this "
                                  "push cannot be checked against that "
                                  "branch", text)
                    self.assertIn("it would publish %d commit(s) that" % count,
                                  text)
                    self.assertNotIn("WARNING", text)
                    self.assertNotIn(missing, text)


class OrdinaryPushTest(_Sandbox):
    """The pushes a train and a lane make, and the pushes the rung refuses
    whatever it reads: each row of the rung's surface that is not the
    incident or a join."""

    def test_a_fresh_clone_commits_and_pushes_to_origin(self):  # noqa: VACUOUS_ASSERTION — the absent warning is read beside the pinned pass line and origin pinned to the non-empty pushed sha
        clone = os.path.join(self.tmp, "clone")
        self.ok(self.tmp, "clone", "-q", self.origin, clone)
        self.ident(clone)
        rc, _out, err = self.cli_in(clone, "install-guard", "--apply",
                                    "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.access(_guard.hook_path(clone, "pre-push"),
                                  os.X_OK))
        self.write(clone, "fresh.md", "work from a fresh clone\n")
        head = self.commit(clone, "work from a fresh clone")
        r = self.git(clone, "push", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.origin, "refs/heads/main"), head)
        ours = rung_lines(r.stderr)
        self.assertIn("'origin': shares history with its default branch main",
                      ours)
        self.assertNotIn("WARNING", ours)

    def test_a_train_push_of_head_to_main_is_proven_and_passes(self):  # noqa: VACUOUS_ASSERTION — the absent warning is read beside the pinned pass line and origin pinned to the non-empty pushed sha, with no pushurl and with one equal to url
        """The no-false-refusal line of the proof: `git push origin
        HEAD:main` with its argv readable, its config readable, and no
        pushurl or a pushurl equal to url, is proven and passes on the
        destination's own default branch, with no warning."""
        self.ok(self.root, "checkout", "-q", "main")
        for pushurl in (None, self.origin):
            with self.subTest(pushurl=pushurl):
                if pushurl:
                    self.ok(self.root, "config", "remote.origin.pushurl",
                            pushurl)
                self.write(self.root, "train.md", "a train, %s\n" % pushurl)
                head = self.commit(self.root, "a train")
                r = self.push("origin", "HEAD:main")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.ref(self.origin, "refs/heads/main"), head)
                ours = rung_lines(r.stderr)
                self.assertIn("'origin': shares history with its default "
                              "branch main, and carries no new root commit",
                              ours)
                self.assertNotIn("WARNING", ours)
                self.assertNotIn("REFUSED", r.stderr)

    def test_a_stale_clone_lane_to_origin_refuses_until_origin_is_fetched(self):  # noqa: VACUOUS_ASSERTION — the absent origin lane is read beside the pinned refusal, and origin's lane pinned to the non-empty lane sha after the fetch is the pass control
        """origin's main moved on in another clone and was never fetched
        here. refs/remotes/origin/main, as last fetched, is no evidence of
        what origin holds now, so the lane is refused; a fetch brings the new
        tip here, and the same push passes on origin's own default branch."""
        other = os.path.join(self.tmp, "other")
        self.ok(self.tmp, "clone", "-q", self.origin, other)
        self.ident(other)
        self.write(other, "other.md", "another seat's train\n")
        moved = self.commit(other, "another seat's train")
        self.ok(other, "push", "-q", "origin", "main")
        self.assertNotEqual(self.git(self.root, "cat-file", "-e",
                                     moved).returncode, 0)
        r = self.push("origin", "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("REFUSED: the default branch of 'origin' cannot be read "
                      "(its tip is not in this repo), and this push carries 1 "
                      "new root commit(s) that 'origin' is not known to hold",
                      ours)
        self.assertNotIn("as last fetched", ours)
        self.assertEqual(self.ref(self.origin, "refs/heads/lane"), "")
        self.ok(self.root, "fetch", "-q", "origin")
        r = self.push("origin", "lane")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.origin, "refs/heads/lane"), self.lane)
        ours = rung_lines(r.stderr)
        self.assertIn("'origin': shares history with its default branch main, "
                      "and carries no new root commit", ours)
        self.assertNotIn("WARNING", ours)

    def test_a_train_with_a_pushurl_is_refused_as_steered(self):  # noqa: VACUOUS_ASSERTION — the unmoved origin main is read beside the pinned refusal, and the same push passes through the remote's own url
        """A pushurl that differs from url steers the push away from the
        fetch URL origin's tracking refs describe, so they are no evidence,
        even when the two spellings name one repository; the same train
        passes through the remote's own url."""
        self.ok(self.root, "config", "remote.origin.pushurl",
                "file://" + self.origin)
        self.ok(self.root, "checkout", "-q", "main")
        self.write(self.root, "train.md", "a train\n")
        head = self.commit(self.root, "a train")
        r = self.push("origin", "main")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("REFUSED: the default branch of 'origin' cannot be read "
                      "(push destination not proven to be the queried one (a "
                      "pushurl that differs from url)), and this push carries "
                      "1 new root commit(s) that 'origin' is not known to "
                      "hold", ours)
        self.assertIn("the remote-tracking refs of 'origin' describe its fetch "
                      "URL, not where this push goes, and are no evidence of "
                      "what its destination holds", ours)
        self.assertNotIn("judged by the remote-tracking refs", ours)
        self.assertEqual(self.ref(self.origin, "refs/heads/main"), self.main)
        self.ok(self.root, "config", "--unset", "remote.origin.pushurl")
        r = self.push("origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.origin, "refs/heads/main"), head)

    def test_a_pushurl_naming_the_public_repository_publishes_nothing(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the pinned refusal, for the configured pushurl and for the argv override alike
        """The bypass a Fable door read measured on the tip before this arm:
        remote.origin.pushurl, or `-c remote.origin.pushurl=`, names the
        public repository, and `git push origin lane` was judged by origin's
        own tracking refs, which reach every private commit, and passed."""
        self.ok(self.root, "config", "remote.origin.pushurl", self.public)
        r = self.push("origin", "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("(a pushurl that differs from url)), and this push "
                      "carries 1 new root commit(s) that 'origin' is not "
                      "known to hold", ours)
        self.assertNotIn(self.public, ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")
        self.ok(self.root, "config", "--unset", "remote.origin.pushurl")
        r = self.git(self.root, "-c", "remote.origin.pushurl=" + self.public,
                     "push", "origin", "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("(a config override in the push argv)), and this push "
                      "carries 1 new root commit(s) that 'origin' is not "
                      "known to hold", ours)
        self.assertNotIn(self.public, ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")

    def test_a_deletion_passes(self):  # noqa: VACUOUS_ASSERTION — the deleted branch is read absent after the control pinned it to the non-empty seed sha
        self.ok(self.release, "push", "-q", self.public, "main:refs/heads/old")
        self.assertEqual(self.ref(self.public, "refs/heads/old"),
                         self.public_main)
        r = self.push("aspublic", ":refs/heads/old")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.public, "refs/heads/old"), "")

    def test_a_forced_push_of_the_lane_over_the_public_main_is_refused(self):  # noqa: VACUOUS_ASSERTION — the public main is read pinned to its non-empty seed sha beside the pinned refusal
        r = self.push("--force", "aspublic", "lane:main")
        self.assertRefused(r, LANE_COMMITS)
        self.assertEqual(self.ref(self.public, "refs/heads/main"),
                         self.public_main)

    def test_an_orphan_branch_to_the_private_origin_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent origin branch is read beside the pinned refusal
        """The gh-pages shape: a KNOWN, DELIBERATE refusal. An orphan branch
        starts a history the private origin does not hold, and the release
        publish is the only door the rung names."""
        self.ok(self.root, "checkout", "-q", "--orphan", "pages")
        self.write(self.root, "index.html", "<p>pages</p>\n")
        pages = self.commit(self.root, "pages")
        r = self.push("origin", "pages")
        ours = self.assertRefused(r, 1)
        self.assertIn("(pushed tip %s)" % pages[:12], ours)
        self.assertEqual(self.ref(self.origin, "refs/heads/pages"), "")

    def test_a_public_repo_still_holding_a_leaked_private_branch_refuses_more(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the pinned refusal and the leaked branch pinned to the non-empty private main
        """The destination already holds the private root, on a branch that is
        not its default (an earlier leak left in place). The push adds no
        root, and it still shares no history with the default branch: the
        ancestor check refuses the one commit it would add."""
        self.ok(self.origin, "push", "-q", self.public,
                "main:refs/heads/leaked")
        self.assertEqual(self.ref(self.public, "refs/heads/leaked"), self.main)
        self.ok(self.root, "fetch", "-q", "aspublic")
        r = self.push("aspublic", "lane")
        ours = self.assertRefused(r, 1, roots=0)
        self.assertIn("REFUSED: this push shares no history with the default "
                      "branch main of 'aspublic' (tip %s)"
                      % self.public_main[:12], ours)
        self.assertIn("'aspublic' holds its root on a ref other than its "
                      "default branch", ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")


class UnreadablePushArgvTest(_Sandbox):
    """The rung reads the argv of the `git push` above its hook from /proc,
    and finds that process by its program name. A git run under another name
    (here a renamed symlink to the real git) is not found, so the argv cannot
    be read. MEASURED by a cross-family door read of 5a37623106135: the proof
    returned "push argv unreadable" before it read the config, that cause
    kept the tracking-ref judgement, and a pushurl or a pushInsteadOf naming
    the public repository passed `git push origin <lane>` on origin's own
    tracking refs, the leak the steered refusal had closed. A -c override
    reaches the config through GIT_CONFIG_PARAMETERS, so the config names it;
    a --receive-pack is in the argv alone, so an argv that cannot be read
    refuses the push."""

    def setUp(self):
        super().setUp()
        self.renamed = os.path.join(self.tmp, "bin", "gitx")
        os.symlink(shutil.which("git"), self.renamed)

    def renamed_push(self, *args):
        """`git push`, run through a git of another name."""
        return subprocess.run((self.renamed,) + args, cwd=self.root,
                              capture_output=True, text=True, timeout=120)

    def assertNoPath(self, ours):
        for piece in (self.tmp, self.public, self.origin, "public.git"):
            self.assertNotIn(piece, ours)

    def test_a_pushurl_naming_the_public_repository_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the pinned refusal naming the pushurl, for the configured pushurl and the -c override alike
        for how in ("config", "-c"):
            with self.subTest(how=how):
                # Each row starts from a public repository without the lane,
                # whatever the row before it did.
                self.git(self.public, "update-ref", "-d", "refs/heads/lane")
                if how == "config":
                    self.ok(self.root, "config", "remote.origin.pushurl",
                            self.public)
                    r = self.renamed_push("push", "origin", "lane")
                    self.ok(self.root, "config", "--unset",
                            "remote.origin.pushurl")
                else:
                    r = self.renamed_push("-c", "remote.origin.pushurl="
                                          + self.public, "push", "origin",
                                          "lane")
                ours = self.assertRefused(r, LANE_COMMITS)
                self.assertIn("REFUSED: the default branch of 'origin' cannot "
                              "be read (push destination not proven to be the "
                              "queried one (a pushurl that differs from "
                              "url)), and this push carries 1 new root "
                              "commit(s) that 'origin' is not known to hold",
                              ours)
                self.assertNotIn("judged by the remote-tracking refs", ours)
                self.assertNoPath(ours)
                self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")

    def test_a_push_insteadof_naming_the_public_repository_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the pinned refusal naming the rewrite, for the configured rewrite and the -c override alike
        key = "url.%s.pushInsteadOf" % self.public
        for how in ("config", "-c"):
            with self.subTest(how=how):
                # Each row starts from a public repository without the lane,
                # whatever the row before it did.
                self.git(self.public, "update-ref", "-d", "refs/heads/lane")
                if how == "config":
                    self.ok(self.root, "config", key, self.origin)
                    r = self.renamed_push("push", "origin", "lane")
                    self.ok(self.root, "config", "--unset", key)
                else:
                    r = self.renamed_push("-c", "%s=%s" % (key, self.origin),
                                          "push", "origin", "lane")
                ours = self.assertRefused(r, LANE_COMMITS)
                self.assertIn("(push destination not proven to be the queried "
                              "one (a pushInsteadOf rewrite)), and this push "
                              "carries 1 new root commit(s) that 'origin' is "
                              "not known to hold", ours)
                self.assertNotIn("judged by the remote-tracking refs", ours)
                self.assertNoPath(ours)
                self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")

    def test_an_unreadable_argv_with_no_steering_configured_refuses(self):  # noqa: VACUOUS_ASSERTION — the absent branches are read beside the pinned refusal, and the same push through the git of its own name passes
        """Fail closed: nothing configured steers the push, and an argv that
        cannot be read can still carry a --receive-pack that writes the pack
        into another repository. The same push through the git of its own
        name is read, proven, and passes."""
        b = self.bare("b.git")
        for extra in ((), ("--receive-pack=git receive-pack %s #"
                           % shlex.quote(b),)):
            with self.subTest(extra=extra):
                r = self.renamed_push("push", *extra + ("origin", "lane"))
                ours = self.assertRefused(r, LANE_COMMITS)
                self.assertIn("REFUSED: the default branch of 'origin' cannot "
                              "be read (push destination not proven to be the "
                              "queried one (push argv unreadable)), and this "
                              "push carries 1 new root commit(s) that "
                              "'origin' is not known to hold", ours)
                self.assertIn("the argv of the git push this hook runs under "
                              "cannot be read", ours)
                self.assertNotIn("judged by the remote-tracking refs", ours)
                self.assertNoPath(ours)
                self.assertNotIn(b, ours)
                self.assertEqual(self.ref(self.origin, "refs/heads/lane"), "")
                self.assertEqual(self.ref(b, "refs/heads/lane"), "")
        r = self.push("origin", "lane")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.origin, "refs/heads/lane"), self.lane)
        self.assertNotIn("push argv unreadable", r.stderr)


class UnprovenDestinationTest(_Sandbox):
    """THE CLASS behind the unreadable argv (helm-codex, cross-family door
    read of 5a37623106135): every way the rung can fail to prove where a push
    goes refuses it before any remote-tracking ref is read, because nothing
    is known of the repository the pack reaches. A push run under a git of
    another name, a remote config the rung cannot read, a URL git did not
    pass, and every steering (a pushurl, a pushInsteadOf, a -c override, a
    receive-pack) are each such a failure. A destination PROVEN to be the
    one ls-remote reads, whose default branch then cannot be read, is
    refused as well, and no remote-tracking ref is read for it either
    (helm-codex, cross-family door read of 7411626e6ba): a proof binds only
    the URL, and a tracking ref describes what the fetch URL held at the
    last fetch, not what the destination holds now."""

    def assertNoPath(self, ours):
        for piece in (self.tmp, self.public, self.origin, "public.git"):
            self.assertNotIn(piece, ours)

    def train(self):
        """A train on main: origin's tracking refs reach every commit it
        carries but its own, so they would pass it if they were consulted."""
        self.ok(self.root, "checkout", "-q", "main")
        self.write(self.root, "train.md", "a train\n")
        return self.commit(self.root, "a train")

    def test_every_proof_failure_refuses_a_train_its_tracking_refs_would_pass(self):  # noqa: VACUOUS_ASSERTION — each refusal row pins its non-empty reason and count, and the control row pins the pass line on the same train
        train = self.train()
        config = hostpath_guard._remote_config(self.root)
        argv = ["git", "push", "origin", "main"]
        for url, cfg, arg, label, why, blind in (
                (self.origin, config, None, "'origin'",
                 "push destination not proven to be the queried one (push "
                 "argv unreadable)",
                 "the argv of the git push this hook runs under cannot be "
                 "read"),
                (self.origin, None, argv, "a URL remote",
                 "push destination not proven to be the queried one (remote "
                 "config unreadable)",
                 "the remote config cannot be read here"),
                (None, config, argv, "'origin'",
                 "git did not pass this push's URL (a hand-run, or a wrapper "
                 "older than v2)",
                 "git did not pass this push's URL")):
            with self.subTest(why=why), \
                    mock.patch.object(hostpath_guard, "_remote_config",
                                      return_value=cfg), \
                    mock.patch.object(hostpath_guard, "_push_argv",
                                      return_value=arg):
                refused, lines = hostpath_guard.judge_push(
                    self.root, "origin", url, [train])
                text = "\n".join(lines)
                self.assertTrue(refused, text)
                self.assertIn("REFUSED: the default branch of %s cannot be "
                              "read (%s), and this push carries 1 new root "
                              "commit(s) that %s is not known to hold"
                              % (label, why, label), text)
                self.assertIn("%s, so where this push goes is not proven, and "
                              "no remote-tracking ref is evidence of what its "
                              "destination holds" % blind, text)
                self.assertIn("it would publish %d commit(s) that %s is not "
                              "known to hold" % (PRIVATE_COMMITS + 1, label),
                              text)
                self.assertNotIn("WARNING", text)
                self.assertNoPath(text)
        # The same train, proven, to a destination ls-remote cannot read, is
        # refused too: origin's tracking refs are not consulted.
        with mock.patch.object(hostpath_guard, "_push_argv",
                               return_value=argv):
            refused, lines = hostpath_guard.judge_push(
                self.root, "origin", os.path.join(self.tmp, "missing.git"),
                [train])
            text = "\n".join(lines)
            self.assertTrue(refused, text)
            self.assertIn("REFUSED: the default branch of 'origin' cannot be "
                          "read (not reachable), and this push carries 1 new "
                          "root commit(s) that 'origin' is not known to hold",
                          text)
            self.assertIn("the destination cannot be read, so this push "
                          "cannot be checked against that branch", text)
            self.assertNotIn("WARNING", text)
            self.assertNoPath(text)
            # The control: the same train, proven, to origin itself passes
            # on origin's own default branch.
            refused, lines = hostpath_guard.judge_push(
                self.root, "origin", self.origin, [train])
        self.assertFalse(refused, lines)
        self.assertIn("%s 'origin': shares history with its default branch "
                      "main, and carries no new root commit" % RUNG,
                      "\n".join(lines))

    def test_git_config_in_the_environment_hides_no_receive_pack(self):  # noqa: VACUOUS_ASSERTION — the absent branches are read beside the pinned refusal naming the receive-pack, for each GIT_CONFIG
        """GIT_CONFIG is read by `git config` alone: git push, and every other
        git call the rung makes, ignores it. Under GIT_CONFIG=/dev/null the
        rung read no remote entry at all and proved the push by the URL git
        passed, while a remote.origin.receivepack wrote the pack into the
        public repository; under a GIT_CONFIG that does not parse it read the
        config as unreadable. The rung reads the config git push reads."""
        bad = os.path.join(self.tmp, "bad.config")
        self.write(self.tmp, "bad.config", "[core\n")
        self.ok(self.root, "config", "remote.origin.receivepack",
                "git receive-pack %s #" % shlex.quote(self.public))
        for where in (bad, os.devnull):
            with self.subTest(GIT_CONFIG=where):
                # Each row starts from a public repository without the lane,
                # whatever the row before it did.
                self.git(self.public, "update-ref", "-d", "refs/heads/lane")
                r = self.push("origin", "lane", env={"GIT_CONFIG": where})
                ours = self.assertRefused(r, LANE_COMMITS)
                self.assertIn("REFUSED: the default branch of 'origin' cannot "
                              "be read (push destination not proven to be the "
                              "queried one (a custom receive-pack)), and this "
                              "push carries 1 new root commit(s) that "
                              "'origin' is not known to hold", ours)
                self.assertNoPath(ours)
                self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")
                self.assertEqual(self.ref(self.origin, "refs/heads/lane"), "")

    def break_the_config_in_a_user_hook(self, shell):
        """A composed user pre-push hook runs before the rung, after git
        resolved where the push goes, runs `shell` on the config, and
        returns 0. The config is restored when the arm ends."""
        cfg = os.path.join(self.root, ".git", "config")
        with open(cfg) as f:
            good = f.read()

        def restore():
            os.chmod(cfg, 0o644)
            with open(cfg, "w") as f:
                f.write(good)
        self.addCleanup(restore)
        user = self.hook + ".helm-user"
        with open(user, "w") as f:
            f.write("#!/bin/sh\ncat >/dev/null\n%s\nexit 0\n"
                    % (shell % shlex.quote(cfg)))
        os.chmod(user, 0o755)

    def assertFailedCheck(self, r):
        """Every git call the rung makes reads the same config and dies, so
        it refuses as a failed check, in fixed words, before any proof."""
        ours = rung_lines(r.stderr)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("%s REFUSED: history check failed — object listing "
                      "failed (exited 128)" % RUNG, ours)
        self.assertNoPath(ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")
        self.assertEqual(self.ref(self.origin, "refs/heads/lane"), "")

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_a_config_a_user_hook_makes_unreadable_refuses(self):  # noqa: VACUOUS_ASSERTION — the absent branches are read beside the pinned refusal
        self.ok(self.root, "config", "remote.origin.pushurl", self.public)
        self.break_the_config_in_a_user_hook("chmod 000 %s")
        self.assertFailedCheck(self.push("origin", "lane"))

    def test_a_config_a_user_hook_makes_malformed_refuses(self):  # noqa: VACUOUS_ASSERTION — the absent branches are read beside the pinned refusal
        self.ok(self.root, "config", "remote.origin.pushurl", self.public)
        self.break_the_config_in_a_user_hook("printf '[core\\n' >> %s")
        self.assertFailedCheck(self.push("origin", "lane"))

    def test_a_valid_config_rewrite_cannot_lend_fetch_tracking_refs_to_the_destination(self):  # noqa: VACUOUS_ASSERTION — the absent lane on the moved push destination is read beside the pinned refusal
        """Parent git resolves the pushurl and opens receive-pack before the
        composed user hook. That hook then removes the pushurl from a still
        valid config and moves the destination, so the scanner's independent
        ls-remote fails while the open receive-pack can continue. Origin's
        tracking refs describe its fetch URL, never that destination."""
        moved = self.public + ".moved"
        self.ok(self.root, "config", "remote.origin.pushurl", self.public)
        user = self.hook + ".helm-user"
        with open(user, "w") as f:
            f.write("#!/bin/sh\ncat >/dev/null\n"
                    "git config --unset remote.origin.pushurl\n"
                    "mv %s %s\nexit 0\n"
                    % (shlex.quote(self.public), shlex.quote(moved)))
        os.chmod(user, 0o755)
        r = self.push("origin", "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("the destination cannot be read", ours)
        self.assertNotIn("judged by the remote-tracking refs", ours)
        self.assertEqual(self.ref(moved, "refs/heads/lane"), "")

    def test_a_push_insteadof_naming_the_public_repository_publishes_nothing(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the pinned refusal, for the configured rewrite and the argv override alike
        key = "url.%s.pushInsteadOf" % self.public
        for how, cause in (("config", "a pushInsteadOf rewrite"),
                           ("-c", "a config override in the push argv")):
            with self.subTest(how=how):
                if how == "config":
                    self.ok(self.root, "config", key, self.origin)
                    r = self.push("origin", "lane")
                    self.ok(self.root, "config", "--unset", key)
                else:
                    r = self.git(self.root, "-c", "%s=%s" % (key, self.origin),
                                 "push", "origin", "lane")
                ours = self.assertRefused(r, LANE_COMMITS)
                self.assertIn("(push destination not proven to be the queried "
                              "one (%s)), and this push carries 1 new root "
                              "commit(s) that 'origin' is not known to hold"
                              % cause, ours)
                self.assertIn("the remote-tracking refs of 'origin' describe "
                              "its fetch URL, not where this push goes", ours)
                self.assertNoPath(ours)
                self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")


class EmptyDestinationTest(_Sandbox):
    """F3: a destination with no ref at all is a brand-new repository. It
    takes a first push when it is private or not on GitHub (a bare mirror, a
    peek room), and refuses one when it is PUBLIC or when it is on GitHub and
    its visibility cannot be read.

    The GitHub remote is `git@github.com:<slug>.git`, and GIT_SSH_COMMAND
    runs its every connection against a bare repository under the sandbox,
    so both git's push and the rung's ls-remote reach that repository and
    nothing reaches a network. The `gh` on PATH is a stub that records its
    arguments and answers from a file (no file: it exits 1)."""

    SLUG = "example-owner/example-repo"

    def setUp(self):
        super().setUp()
        hosted = os.path.join(self.tmp, "hosted")
        self.hosted = os.path.join(hosted, self.SLUG + ".git")
        os.makedirs(os.path.dirname(self.hosted))
        self.ok(self.tmp, "init", "-q", "--bare", "-b", "main", self.hosted)
        bin_dir = os.path.join(self.tmp, "bin")
        ssh = os.path.join(bin_dir, "fake-ssh")
        with open(ssh, "w") as f:
            f.write('#!/bin/sh\ncd %s || exit 1\nexec sh -c "$2"\n'
                    % shlex.quote(hosted))
        os.chmod(ssh, 0o755)
        self.gh_calls = os.path.join(self.tmp, "gh-calls.txt")
        self.gh_answer = os.path.join(self.tmp, "gh-answer.json")
        with open(os.path.join(bin_dir, "gh"), "w") as f:
            f.write('#!/bin/sh\necho "$@" >> %s\n[ -f %s ] || exit 1\n'
                    'cat %s\n' % (shlex.quote(self.gh_calls),
                                  shlex.quote(self.gh_answer),
                                  shlex.quote(self.gh_answer)))
        os.chmod(os.path.join(bin_dir, "gh"), 0o755)
        # set INSIDE the sandbox's patch, so stopping it removes them
        os.environ["GIT_SSH_COMMAND"] = ssh
        os.environ["GIT_SSH_VARIANT"] = "simple"
        self.addCleanup(os.environ.pop, "GIT_SSH_COMMAND", None)
        self.addCleanup(os.environ.pop, "GIT_SSH_VARIANT", None)
        self.ok(self.root, "remote", "add", "hosted",
                "git@github.com:%s.git" % self.SLUG)

    def answer(self, private):
        with open(self.gh_answer, "w") as f:
            f.write('{"isPrivate": %s}\n' % ("true" if private else "false"))

    def asked(self):
        """-> the gh calls, one line of arguments each."""
        if not os.path.exists(self.gh_calls):
            return []
        with open(self.gh_calls) as f:
            return f.read().splitlines()

    def assertNoUrlByte(self, ours):
        for piece in ("github.com", "example-owner", "example-repo",
                      self.tmp):
            self.assertNotIn(piece, ours)

    def test_an_empty_public_github_repository_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent hosted branch is read beside the pinned refusal and the must-hit gh call
        self.answer(private=False)
        r = self.push("hosted", "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("REFUSED: 'hosted' has no branch yet (a brand-new "
                      "repository), and it is PUBLIC", ours)
        self.assertIn("carries 1 new root commit(s) that 'hosted' does not "
                      "hold", ours)
        self.assertIn("(pushed tip %s)" % self.lane[:12], ours)
        self.assertIn("repo view github.com/%s --json isPrivate" % self.SLUG,
                      self.asked())
        self.assertNoUrlByte(ours)
        self.assertEqual(self.ref(self.hosted, "refs/heads/lane"), "")

    def test_an_empty_github_repository_of_unreadable_visibility_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent hosted branch is read beside the pinned refusal and the two must-hit gh calls
        r = self.push("hosted", "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("REFUSED: 'hosted' has no branch yet (a brand-new "
                      "repository), and its visibility is UNKNOWN (gh repo "
                      "view exited 1, twice), treated as public", ours)
        self.assertEqual(len(self.asked()), 2, "one probe, one retry")
        self.assertNoUrlByte(ours)
        self.assertEqual(self.ref(self.hosted, "refs/heads/lane"), "")

    def test_an_empty_private_github_repository_takes_its_first_push(self):  # noqa: VACUOUS_ASSERTION — the returncode 0 is read beside the hosted branch pinned to the non-empty lane sha and the must-hit gh call
        self.answer(private=True)
        r = self.push("hosted", "lane")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.hosted, "refs/heads/lane"), self.lane)
        self.assertIn("'hosted' has no branch yet (a brand-new repository), "
                      "and it is private: passed", rung_lines(r.stderr))
        self.assertIn("repo view github.com/%s --json isPrivate" % self.SLUG,
                      self.asked())

    def test_a_url_whose_path_climbs_into_the_empty_repository_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent hosted branch is read beside the pinned refusal and the must-hit --no-verify push that lands there
        """task/3413, RED on abeca0b48ac: the URL reads as
        github.com/example-owner/private, which gh reads PRIVATE, while its
        path climbs out of that repository into the empty example-repo, so
        the first push passed as "private". MEASURED: ssh carries such a
        path to the server as written, and over https curl removes the dot
        segments before the request; the fake ssh here resolves it as a
        filesystem does. A URL that is not a plain GitHub repository URL is
        UNKNOWN: gh is not asked, and the first push is refused."""
        self.answer(private=True)
        os.makedirs(os.path.join(os.path.dirname(self.hosted), "private"))
        climbing = "git@github.com:example-owner/private/../example-repo.git"
        r = self.push(climbing, "lane")
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("REFUSED: a URL remote has no branch yet (a brand-new "
                      "repository), and its visibility is UNKNOWN (not a "
                      "plain GitHub repository URL), treated as public", ours)
        self.assertEqual(self.asked(), [])
        self.assertNoUrlByte(ours)
        self.assertEqual(self.ref(self.hosted, "refs/heads/lane"), "")
        r = self.push("--no-verify", climbing, "lane")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.hosted, "refs/heads/lane"), self.lane,
                         "must-hit: the URL reaches example-repo")


class ReleaseMarkerTest(_Sandbox):
    """pusher x {ordinary, the release tool's publish marker}."""

    def test_the_marker_passes_exactly_the_commit_it_names(self):  # noqa: VACUOUS_ASSERTION — the returncode 0 is read beside the public branch pinned to the non-empty lane sha and the pinned marker line
        r = self.push("aspublic", "lane", env={MARKER: self.lane})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), self.lane)
        self.assertIn("the release publish of %s" % self.lane[:12],
                      rung_lines(r.stderr))

    def test_the_marker_passes_a_tag_that_peels_to_its_commit(self):  # noqa: VACUOUS_ASSERTION — the returncode 0 is read beside the public tag pinned to the non-empty lane sha
        self.ok(self.root, "tag", "-a", "-m", "release", "v9", "lane")
        r = self.push("aspublic", "v9", env={MARKER: self.lane})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.public, "refs/tags/v9^{commit}"),
                         self.lane)

    def test_a_marker_naming_another_commit_is_an_ordinary_push(self):  # noqa: VACUOUS_ASSERTION — the absent branch is read beside the pinned refusal
        for marker in (self.main, "not-a-sha", ""):
            with self.subTest(marker=marker):
                r = self.push("aspublic", "lane", env={MARKER: marker})
                self.assertRefused(r, LANE_COMMITS)
                self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")

    def test_a_marker_covers_no_second_commit_in_the_same_push(self):  # noqa: VACUOUS_ASSERTION — the absent branches are read beside the pinned refusal
        r = self.push("aspublic", "lane", "main:refs/heads/other",
                      env={MARKER: self.lane})
        self.assertRefused(r, LANE_COMMITS)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")
        self.assertEqual(self.ref(self.public, "refs/heads/other"), "")

    def test_the_marker_is_the_name_the_release_tool_sets(self):
        self.assertEqual(hostpath_guard.RELEASE_MARKER, MARKER)
        with open(trt.TOOL, encoding="utf-8") as f:
            self.assertIn('RELEASE_MARKER = "%s"' % MARKER, f.read())


class HookStateTest(_Sandbox):
    """hook x {installed, missing}, and the rung beside the host-path skip."""

    def test_without_the_hook_the_incident_push_goes_through(self):  # noqa: VACUOUS_ASSERTION — the returncode 0 is read beside the MISSING row and the public branch pinned to the non-empty lane sha
        """The control for every refusal above: the fixture's push is not
        refused by anything but the installed hook."""
        os.unlink(self.hook)
        self.assertIn(("MISSING", "pre-push"),
                      [(s, n) for s, n, _w in _guard.stale_guard_hooks(
                          self.root)])
        r = self.push("aspublic", "lane")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), self.lane)

    def test_the_host_path_skip_does_not_disarm_the_rung(self):  # noqa: VACUOUS_ASSERTION — the absent branch is read beside the pinned refusal
        r = self.push("aspublic", "lane", env={"HELM_HOSTPATH_SKIP": "1"})
        self.assertRefused(r, LANE_COMMITS)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")

    def test_a_missing_snapshot_is_named_and_reported(self):
        snapshot = next(p for p in _guard._scanner_assets(self.root, "leak")
                        if p.endswith("/hostpath_guard.py"))
        os.unlink(snapshot)
        self.assertIn(("MISSING", "scanner:hostpath_guard.py"),
                      [(s, n) for s, n, _w in _guard.stale_guard_hooks(
                          self.root)])
        r = self.push("aspublic", "lane")
        self.assertIn("%s WARNING: rung missing at %s" % (RUNG, snapshot),
                      r.stderr)
        self.assertIn("reinstall: helm work install-guard --apply --profile "
                      "leak", r.stderr)

    def test_the_installed_hook_carries_the_rung_on_its_refusal_path(self):
        """The census credits the installed hook, and a copy whose rung line
        cannot refuse is not credited."""
        hooks = os.path.dirname(self.hook)
        units = os.path.join(self.tmp, "no-units")
        os.makedirs(units)
        got = wiring.actuator_census(hook_dir=hooks, unit_dir=units,
                                     crontab="", settings_paths=[])
        self.assertIn("shared-history-pre-push", got["wired"])
        with open(self.hook) as f:
            text = f.read()
        line = ('helm_push_refs | python3 "$shared_history" '
                '--shared-history "$@" || exit $?')
        self.assertEqual(text.count(line), 1)
        demoted = os.path.join(self.tmp, "demoted-hooks")
        os.makedirs(demoted)
        with open(os.path.join(demoted, "pre-push"), "w") as f:
            f.write(text.replace(line, line.replace("|| exit $?",
                                                    "|| true")))
        os.chmod(os.path.join(demoted, "pre-push"), 0o755)
        got = wiring.actuator_census(hook_dir=demoted, unit_dir=units,
                                     crontab="", settings_paths=[])
        self.assertIn("shared-history-pre-push", got["missing"])


class AutoLandPinnedPushTest(_Sandbox):
    """THE TRAIN415 SEAM. Auto-land pushes through a one-time
    name that its own environment maps to the vetted URL (helm/autoland.py
    `_pinned`, task/3265 races R1), so no config written after its checks can
    redirect the push. The rung read that pin as "a pushInsteadOf rewrite":
    it refused a fast-forward of trunk as 8965 commits a URL remote is not
    known to hold, and the same tip pushed by hand through origin passed.
    A pin of the push name to exactly the URL git passes is proof
    (`hostpath_guard._pin`); a pin git does not follow is not. And the rung
    reads that URL through a pin of its own (`hostpath_guard._pinned`), so
    no remote named as the URL can answer for it (helm-codex's door read of
    the pinned push, finding 1).

    Each arm drives the shipped `autoland.Ops.push`, or git under the very
    environment `_pinned` builds, through the installed hook into the
    sandbox's bare origin, which holds trunk (`self.main`), or into the
    public repository."""

    def setUp(self):
        super().setUp()
        self.ok(self.root, "checkout", "-q", "main")
        self.write(self.root, "train.md", "a train\n")
        self.train = self.commit(self.root, "a train")
        self.target = autoland.PushTarget(self.origin, "refs/heads/main",
                                          "origin")

    def land(self, head):
        """The auto-land push of `head`, leased on trunk."""
        return autoland.Ops().push(self.root, head, self.target,
                                   lease=self.main)

    def test_a_pinned_push_of_a_fast_forward_of_trunk_is_proven_and_lands(self):  # noqa: VACUOUS_ASSERTION — the push positively answers True, origin's main positively reads the train, and the rung's pass line is pinned
        ok, detail = self.land(self.train)
        self.assertIs(ok, True, detail)
        self.assertEqual(self.ref(self.origin, "refs/heads/main"), self.train)
        self.assertIn("%s a URL remote: shares history with its default "
                      "branch main, and carries no new root commit" % RUNG,
                      detail)
        self.assertNotIn("not proven", detail)

    def test_a_pin_git_does_not_follow_is_refused(self):  # noqa: VACUOUS_ASSERTION — the unmoved mirror and origin mains are read beside the pinned refusal, and the same pinned push lands once the competing rule is gone
        """A repository rule on the same name beats the pin's command-line
        one (git follows the rule it read first on a tie), so git pushes to
        a mirror that holds trunk too, and passes the mirror as $2. The pin
        to origin is then a rewrite git did not follow, and the push is
        refused as not proven, though a judgement of the mirror would pass
        it. The control: without the competing rule, the same push under
        the same environment lands on origin."""
        mirror = os.path.join(self.tmp, "mirror.git")
        self.ok(self.tmp, "clone", "-q", "--bare", self.origin, mirror)
        name, env = autoland._pinned(dict(os.environ), self.origin)
        key = "url.%s.pushInsteadOf" % mirror
        self.ok(self.root, "config", key, name)
        argv = ("push", "--force-with-lease=refs/heads/main:%s" % self.main,
                name, "%s:refs/heads/main" % self.train)
        r = self.git(self.root, *argv, env=env)
        ours = self.assertRefused(r, PRIVATE_COMMITS + 1)
        self.assertIn("REFUSED: the default branch of a URL remote cannot be "
                      "read (push destination not proven to be the queried "
                      "one (a pushInsteadOf rewrite))", ours)
        self.assertEqual(self.ref(mirror, "refs/heads/main"), self.main)
        self.assertEqual(self.ref(self.origin, "refs/heads/main"), self.main)
        self.ok(self.root, "config", "--unset", key)
        r = self.git(self.root, *argv, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ref(self.origin, "refs/heads/main"), self.train)
        self.assertEqual(self.ref(mirror, "refs/heads/main"), self.main)

    def test_a_pinned_push_carrying_a_new_root_is_refused(self):  # noqa: VACUOUS_ASSERTION — the push positively answers False quoting the rung's refusal, beside origin's main pinned to the unmoved trunk
        """The pin is proof of where the push goes, and nothing more: a
        train that merges the public release line in carries its root, and
        the rung refuses it on origin's own default branch. auto-land reads
        the failed push as it reads any (False: AMBIGUOUS, settled by a read
        of trunk), with git's answer carrying the refusal."""
        self.ok(self.root, "merge", "-q", "--no-verify", "--no-edit",
                "--allow-unrelated-histories", "-m", "join the release line",
                "refs/remotes/aspublic/main")
        joined = self.ok(self.root, "rev-parse", "HEAD")
        ok, detail = self.land(joined)
        self.assertIs(ok, False, detail)
        self.assertIn("%s REFUSED: this push carries 1 new root commit(s) "
                      "that a URL remote does not hold" % RUNG, detail)
        self.assertEqual(self.ref(self.origin, "refs/heads/main"), self.main)

    def test_a_remote_named_as_the_pinned_url_cannot_answer_for_it(self):  # noqa: VACUOUS_ASSERTION — the absent public branch is read beside the named refusal, and the control's refusal on the public repository's own history is pinned on the same pinned push
        """HELM-CODEX'S DOOR READ OF THE PINNED PUSH (finding 1), in the
        incident's shape: a pinned push of the private lane (the name and
        environment `autoland._pinned` builds) writes exactly the public
        repository's URL, and a remote section NAMED as that URL string,
        pointing at the private origin, answered the rung's `git ls-remote
        <url>` with origin's refs, where the lane shares history and carries
        no new root. `Ops.push` vets that section before it pushes
        (`autoland._rewritten`), but config written after its checks is what
        the pin is for, and the rung stands on its own. RED before the query
        was pinned, MEASURED on git 2.53: the push passed and the public
        repository held the lane, the whole
        private history. The push is refused as not proven. The control,
        with no such remote: the rung reads the public repository itself and
        refuses the lane on its own history. The URL is a file:// URL: git
        ignores a remote section whose name begins with '/'."""
        url = "file://" + self.public
        name, env = autoland._pinned(dict(os.environ), url)
        refspec = "%s:refs/heads/lane" % self.lane
        self.ok(self.root, "config", "remote.%s.url" % url, self.origin)
        r = self.git(self.root, "push", name, refspec, env=env)
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("(push destination not proven to be the queried one "
                      "(a remote is configured under the push URL's own "
                      "name))", ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")
        self.ok(self.root, "config", "--unset", "remote.%s.url" % url)
        r = self.git(self.root, "push", name, refspec, env=env)
        ours = self.assertRefused(r, LANE_COMMITS)
        self.assertIn("REFUSED: this push shares no history with the default "
                      "branch main of a URL remote (tip %s)"
                      % self.public_main[:12], ours)
        self.assertEqual(self.ref(self.public, "refs/heads/lane"), "")

    def test_the_rungs_own_query_reads_exactly_the_url(self):
        """The rung's `ls-remote --symref` goes through its own pin
        (`hostpath_guard._pinned`), so even with the proof's refusal of a
        remote named as the URL set aside, that remote cannot answer for
        the URL: the destination read is the public repository's own main.
        RED before the query was pinned: it read origin's main."""
        url = "file://" + self.public
        self.ok(self.root, "config", "remote.%s.url" % url, self.origin)
        config = hostpath_guard._remote_config(self.root)
        with mock.patch.object(hostpath_guard, "_proof_failure",
                               return_value=None):
            state, branch, tip, advertised, why = hostpath_guard._destination(
                self.root, "helm-autoland-push/" + "5" * 32, url, config)
        self.assertEqual((state, branch, tip, why),
                         (hostpath_guard.READ, "main", self.public_main,
                          None))
        self.assertNotIn(self.main, advertised)
        bare = self.ok(self.root, "ls-remote", "--heads", url)
        self.assertIn(self.main, bare, "must-hit: the remote steers a bare "
                      "read of the URL")


class ReleasePublishThroughTheRungTest(trt.ReleaseFixture):
    """The release tool's publish passes the rung by its marker, and only its
    publish does.

    The private repository's main is the trunk's own history, as in the real
    estate, so the stage write (a release commit on the public line, pushed to
    the private repository as release/<version>) shares no history with the
    private main. Every git call the command makes runs with core.hooksPath
    naming a directory holding the managed pre-push, so every push it makes
    meets the rung. A composed user hook records each push the managed hook
    ran for, which is the must-hit that the rung was on the path."""

    def setUp(self):
        super().setUp()
        _git = trt._git
        _git(self.src, "push", "-q", self.private, "main:refs/heads/main")
        self.private_main = _git(self.private, "rev-parse", "refs/heads/main")
        self.hooks = os.path.join(self.tmp, "hooks")
        os.makedirs(self.hooks)
        self.pushes = os.path.join(self.tmp, "pushes.txt")
        user = os.path.join(self.hooks, "pre-push.helm-user")
        hook = _guard.HOSTPATH_PUSH_HOOK % {
            "scanner": shlex.quote(os.path.join(REPO, "helm",
                                                "hostpath_guard.py")),
            "user_hook": shlex.quote(user), "profile": "leak"}
        trt._write(os.path.join(self.hooks, "pre-push"), hook, 0o755)
        trt._write(user, '#!/bin/sh\ncat >/dev/null\necho "$2" >> %s\n'
                   % shlex.quote(self.pushes), 0o755)
        trt._write(os.path.join(self.stubs, "helm"), "#!/bin/sh\nexit 0\n",
                   0o755)
        self.armed = {"GIT_CONFIG_COUNT": "1",
                      "GIT_CONFIG_KEY_0": "core.hooksPath",
                      "GIT_CONFIG_VALUE_0": self.hooks}

    def pushed(self):
        if not os.path.exists(self.pushes):
            return []
        with open(self.pushes) as f:
            return [os.path.realpath(u) for u in f.read().split()]

    def test_publish_passes_the_rung_on_its_marker(self):  # noqa: VACUOUS_ASSERTION — the returncode 0 is read beside the staged branch pinned to the non-empty candidate sha and the three recorded pushes
        rc, out, _work = self.release("--publish", env=self.armed)
        self.assertEqual(rc, 0, out)
        cand = trt._git(self.public, "rev-parse", "refs/heads/main")
        self.assertEqual(trt._git(self.private, "rev-parse",
                                  "refs/heads/release/" + trt.VERSION), cand)
        self.assertEqual(trt._git(self.private, "rev-parse", "refs/heads/main"),
                         self.private_main)
        private, public = map(os.path.realpath, (self.private, self.public))
        self.assertEqual(self.pushed(), [private, public, public],
                         "the rung ran for the stage, main and tag writes")

    def test_the_public_main_write_passes_without_the_marker(self):  # noqa: VACUOUS_ASSERTION — the returncode 0 is read beside the public main pinned to the non-empty candidate sha and the must-hit push record
        """The candidate is a child of the public main: pushed there without
        the marker, it shares history with the default branch and starts no
        history, so the rung passes it on its own terms."""
        rc, out, work = self.release(env=self.armed)
        self.assertEqual(rc, 0, out)
        repo = os.path.join(work, "release.git")
        cand = trt._git(repo, "rev-parse", "refs/heads/main")
        env = dict(self.fixture_env, PATH=self.stubs + os.pathsep
                   + os.environ.get("PATH", ""), **self.armed)
        env.pop(MARKER, None)
        p = subprocess.run(("git", "-C", repo, "push", self.public,
                            "%s:refs/heads/main" % cand),
                           capture_output=True, text=True, timeout=120,
                           env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(trt._git(self.public, "rev-parse", "refs/heads/main"),
                         cand)
        self.assertIn("a URL remote: shares history with its default branch "
                      "main", rung_lines(p.stderr))
        self.assertEqual(self.pushed(), [os.path.realpath(self.public)])

    def test_the_same_stage_write_without_the_marker_is_refused(self):  # noqa: VACUOUS_ASSERTION — the absent staged branch is read beside the pinned refusal and the must-hit push record
        rc, out, work = self.release(env=self.armed)
        self.assertEqual(rc, 0, out)
        repo = os.path.join(work, "release.git")
        cand = trt._git(repo, "rev-parse", "refs/heads/main")
        spec = "%s:refs/heads/release/%s" % (cand, trt.VERSION)
        env = dict(self.fixture_env, PATH=self.stubs + os.pathsep
                   + os.environ.get("PATH", ""), **self.armed)
        for marker in (None, self.private_main):
            with self.subTest(marker=marker):
                p = subprocess.run(
                    ("git", "-C", repo, "push", self.private, spec),
                    capture_output=True, text=True, timeout=120,
                    env=dict(env, **({MARKER: marker} if marker else {})))
                ours = rung_lines(p.stderr)
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertIn("REFUSED: this push shares no history with the "
                              "default branch main of a URL remote", ours)
                self.assertIn("it would publish 2 commit(s) that", ours)
                self.assertIn(DOOR, ours)
                self.assertNotIn(MARKER, ours)
                self.assertEqual(trt._git(self.private, "for-each-ref",
                                          "refs/heads/release"), "")
        self.assertEqual(self.pushed(), [os.path.realpath(self.private)] * 2)


if __name__ == "__main__":
    unittest.main()
