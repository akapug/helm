import os
import tempfile
import unittest

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import skills  # noqa: E402


def _mk_skill(home_dir, name, content):
    d = os.path.join(home_dir, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(content)


class SkillsTest(unittest.TestCase):
    def setUp(self):
        self.h1 = tempfile.mkdtemp(prefix="helm-skills1-")
        self.h2 = tempfile.mkdtemp(prefix="helm-skills2-")
        _mk_skill(self.h1, "alpha", "same content")
        _mk_skill(self.h2, "alpha", "same content")          # identical shadow
        _mk_skill(self.h1, "beta", "one version")
        _mk_skill(self.h2, "beta", "another version")        # diverged shadow
        _mk_skill(self.h1, "gamma", "unique")
        _mk_skill(self.h2, "gamma-renamed", "unique")        # same content, two names
        self._orig = skills._skill_homes
        skills._skill_homes = lambda: ([], [])

    def tearDown(self):
        skills._skill_homes = self._orig

    def test_census_and_dupes(self):
        found, bad = skills.census(extra_homes=(self.h1, self.h2))
        self.assertEqual(len(found), 6)
        self.assertEqual(bad, [])
        name_dupes, content_dupes = skills.dupes(found)
        self.assertEqual(set(name_dupes), {"alpha", "beta"})
        hashes = {frozenset(s["name"] for s in v) for v in content_dupes.values()}
        self.assertIn(frozenset({"gamma", "gamma-renamed"}), hashes)
        alpha = name_dupes["alpha"]
        self.assertEqual(len({s["hash"] for s in alpha}), 1)   # identical
        beta = name_dupes["beta"]
        self.assertEqual(len({s["hash"] for s in beta}), 2)    # diverged


if __name__ == "__main__":
    unittest.main()
