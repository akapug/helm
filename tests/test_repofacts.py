#!/usr/bin/env python3
"""`repofacts.slug_of`: a remote names a GitHub repository only when its URL
is a PLAIN GitHub repository URL (task/3410).

THE DEFECT. `slug_of` read `https://evil.example#@github.com/<owner/repo>`,
its `?` form, a port before the `#`, and `file://github.com/<owner/repo>` as
github.com/<owner/repo>. task/3413 MEASURED where git takes each (git 2.53,
libcurl 8.18, through a local logging proxy and a local path): a `#` or a `?`
before the `@` ends the host for curl, so git reaches the host before it;
file:// drops the host, so git reads a local path. The slug is what gh is
asked about, what the or-free privacy door (`dispatches._commit_public`)
pairs with the refs that remote's fetch wrote, and what the owner's board
links to, so each of these URLs borrowed another repository's visibility and
link.

THE CURE READS ONE RULE: `hostpath_guard._plain_slug`, the rule the push
destination's visibility and a PUBLIC remote's advertisement are read under
(https, ssh or scp form, owner/repo and nothing more). Any other URL, one
that merely reads as GitHub's included, names no repository, so its
visibility is unknown and it links nowhere.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import hostpath_guard, repofacts  # noqa: E402
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_hostpath_guard as _hostpath  # noqa: E402

#: URLs git reaches another host or repository through, beyond task/3413's
#: measured list: a dot segment as the whole owner or repo part.
_DOTS = ("https://github.com/owner/..", "https://github.com/./repo",
         "git@github.com:owner/...git")

#: Plain GitHub repository URLs and the owner/repo each names.
_PLAIN = (("git@github.com:akapug/alpha.git", "akapug/alpha"),
          ("https://github.com/emberian/alpha", "emberian/alpha"),
          ("https://github.com/owner/repo.git/", "owner/repo"),
          ("ssh://git@github.com/owner/repo.git", "owner/repo"),
          ("https://someone:not-a-real-secret@github.com/akapug/alpha.git",
           "akapug/alpha"))


class ASlugIsReadOnlyFromAPlainGitHubUrlTest(unittest.TestCase):

    def test_a_url_git_takes_elsewhere_names_no_repository(self):  # noqa: VACUOUS_ASSERTION — each URL is asserted to read as None, and the control below asserts the same reader names a plain URL's repository
        """RED on trunk for the `#`, `?`, port-then-`#`, http `?` and
        file:// URLs, and for the dot-only owner and repo parts: each read
        as a slug."""
        for url in [u for u, _where in _hostpath._ELSEWHERE] + list(_DOTS):
            with self.subTest(url=url):
                self.assertIsNone(repofacts.slug_of(url))

    def test_a_plain_github_url_still_names_its_repository(self):  # noqa: VACUOUS_ASSERTION — each plain URL's slug is asserted EQUAL to its owner/repo, from both readers
        """CONTROL: the plain https, ssh:// and scp spellings, with a
        trailing `.git/` or a credential, name their owner/repo, as the
        host-path guard's rule reads them."""
        for url, slug in _PLAIN:
            with self.subTest(url=url[:40]):
                self.assertEqual(repofacts.slug_of(url), slug)
                self.assertEqual(hostpath_guard._plain_slug(url), slug)

    def test_a_url_that_is_not_githubs_names_nothing(self):  # noqa: VACUOUS_ASSERTION — each URL is asserted to read as None beside the plain-URL control above
        for url in ("/srv/backup/widget.git", "https://gitlab.example/o/n",
                    "https://notgithub.com/o/n", "", None):
            with self.subTest(url=url):
                self.assertIsNone(repofacts.slug_of(url))


class ARemoteRowNamesOnlyAPlainRepositoryTest(unittest.TestCase):
    """Through the checkout's own config, the read the board and the or-free
    door share (`repofacts.remotes`)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-repofacts-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(repofacts._REMOTES_MEMO.clear)
        self.repo = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "-q", self.repo], check=True,
                       capture_output=True)

    def remote(self, name, url):
        subprocess.run(["git", "-C", self.repo, "remote", "add", name, url],
                       check=True, capture_output=True)

    def test_a_url_git_takes_elsewhere_is_a_row_with_no_repository(self):
        """RED on trunk: the row named acme/widget-public and linked to it.
        Its URL is still never shown."""
        self.remote("origin", "git@github.com:acme/widget.git")
        self.remote("aspublic", "https://evil.example#@github.com/acme/"
                                "widget-public")
        rows, why = repofacts.remotes(self.repo)
        self.assertIsNone(why, why)
        self.assertEqual([(r["remote"], r["slug"], r["url"]) for r in rows],
                         [("origin", "acme/widget",
                           "https://github.com/acme/widget"),
                          ("aspublic", None, None)])
        self.assertNotIn("evil.example", repr(rows))


if __name__ == "__main__":
    unittest.main()
