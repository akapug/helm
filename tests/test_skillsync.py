import os
import shutil
import tempfile
import time
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import skillsync  # noqa: E402


def _mk_skill(root, name, content, age=0):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "SKILL.md")
    with open(p, "w") as f:
        f.write(content)
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return d


class FakeEstate(unittest.TestCase):
    """A tmp estate mirroring the live shapes: a canonical hub, a per-skill
    symlink-farm credhome with a stranded real skill, an alias symlink, an
    already-canonical home, a home with no skills at all, the default
    ~/.claude with a stray symlink skill, and seat + seat-instance dirs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-skillsync-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        j = os.path.join
        self.canon = j(self.tmp, "skill-hub", "skills")
        _mk_skill(self.canon, "build", "the build skill", age=9000)
        os.makedirs(j(self.canon, ".system"))          # hidden: stays private
        self.backup = j(self.tmp, "premerge-backup")
        self.croot = j(self.tmp, "claude-homes")

        # alias: symlink-farm home + STRANDED real skills (the stranded-personal-skill class)
        alias = j(self.croot, "member-example-com", "skills")
        os.makedirs(alias)
        os.symlink(j(self.canon, "build"), j(alias, "build"))
        _mk_skill(alias, "personal-skill", "stranded", age=100)
        _mk_skill(alias, "orca-cli", "orca newest", age=50)
        os.symlink(j(self.croot, "member-example-com"), j(self.croot, "member-example"))

        # ops: already a whole-dir symlink to canonical (idempotent no-op)
        ops = j(self.croot, "admin-example-com")
        os.makedirs(ops)
        os.symlink(self.canon, j(ops, "skills"))

        # fresh: a credhome with no skills dir at all
        os.makedirs(j(self.croot, "fresh-com"))

        # default ~/.claude: real dir w/ a stray SYMLINK skill + older dupe
        self.agents = j(self.tmp, "agents-skills")
        _mk_skill(self.agents, "orca-cli", "orca older", age=5000)
        self.default = j(self.tmp, "dot-claude")
        dsk = j(self.default, "skills")
        os.makedirs(dsk)
        os.symlink(j(self.agents, "orca-cli"), j(dsk, "orca-cli"))
        _mk_skill(dsk, "team-metrics", "team metrics", age=200)
        # and a skill NEWER than canonical's copy (newest-wins replacement)
        _mk_skill(dsk, "build", "the build skill, newer edit", age=10)

        # seats: family dir linked at another home's dir (the live chain
        # shape), one instance with nothing
        self.seats = j(self.tmp, "seats")
        cdx = j(self.seats, "codex", "claude")
        os.makedirs(cdx)
        os.symlink(alias, j(cdx, "skills"))
        os.makedirs(j(self.seats, "codex", "instances", "codex-2", "claude"))
        os.makedirs(j(self.seats, "codex", "smoke-claude"))  # must be skipped

        self.dirs = skillsync.config_dirs(
            claude_root=self.croot, default_claude=self.default,
            seats_root=self.seats)

    def _sync(self, apply):
        return skillsync.sync(canon=self.canon, dirs=self.dirs,
                              backup_root=self.backup, apply=apply)

    def test_discovery_dedupes_aliases_and_skips_smoke(self):
        labels = [l for l, _ in self.dirs]
        self.assertEqual(labels.count("member-example-com"), 1)
        self.assertNotIn("member-example", labels)          # alias folded
        self.assertIn("default-claude", labels)
        self.assertIn("seat:codex", labels)
        self.assertTrue(any("codex-2" in l for l in labels))
        self.assertFalse(any("smoke" in l for l in labels))

    def test_dry_run_touches_nothing(self):
        r = self._sync(apply=False)
        self.assertEqual(r["failed"], [])
        names = {n for n, _ in r["merged"]}
        self.assertEqual(names, {"personal-skill", "orca-cli", "team-metrics", "build"})
        self.assertFalse(os.path.exists(os.path.join(self.canon, "personal-skill")))
        self.assertFalse(os.path.islink(
            os.path.join(self.croot, "member-example-com", "skills")))
        self.assertFalse(os.path.exists(self.backup))

    def test_apply_unions_and_wires_everything(self):
        r = self._sync(apply=True)
        self.assertEqual(r["failed"], [])
        # union: the stranded skill is canonical now
        self.assertTrue(os.path.isfile(
            os.path.join(self.canon, "personal-skill", "SKILL.md")))
        # newest-wins: alias's orca-cli (newer) beat the default home's symlink
        with open(os.path.join(self.canon, "orca-cli", "SKILL.md")) as f:
            self.assertEqual(f.read(), "orca newest")
        # newest-wins vs canonical itself: the newer 'build' replaced it,
        # the displaced copy is in the backup shelf
        with open(os.path.join(self.canon, "build", "SKILL.md")) as f:
            self.assertEqual(f.read(), "the build skill, newer edit")
        shelf = os.path.join(self.backup, "canonical-displaced")
        self.assertTrue(any(e.startswith("build-") for e in os.listdir(shelf)))
        # stray symlink skill adopted (as a copy or link, name present)
        self.assertIn("team-metrics", os.listdir(self.canon))
        # every config dir is now a DIRECT symlink to canonical
        for _label, cdir in self.dirs:
            s = os.path.join(cdir, "skills")
            self.assertTrue(os.path.islink(s), s)
            self.assertEqual(os.path.realpath(s), os.path.realpath(self.canon), s)
        # superset: every skill visible in alias before is visible after
        view = set(os.listdir(os.path.join(self.croot, "member-example-com", "skills")))
        self.assertLessEqual({"build", "personal-skill", "orca-cli"}, view)
        # the original real dirs survive whole in the backup root
        saved = os.path.join(self.backup, "member-example-com")
        snap = os.listdir(saved)
        self.assertEqual(len(snap), 1)
        self.assertTrue(os.path.isfile(os.path.join(
            saved, snap[0], "personal-skill", "SKILL.md")))
        # hidden canonical internals untouched
        self.assertTrue(os.path.isdir(os.path.join(self.canon, ".system")))

    def test_idempotent_rerun_reports_zero_changes(self):
        self._sync(apply=True)
        r = self._sync(apply=True)
        self.assertEqual(r["merged"], [])
        self.assertEqual(r["failed"], [])
        self.assertTrue(all(a == "ok" for _l, a, _d in r["wired"]))

    def test_new_home_wired_by_rerun(self):
        """The durability contract: a credhome minted AFTER the first sync
        gets canonical skills from one re-run."""
        self._sync(apply=True)
        newborn = os.path.join(self.croot, "newborn-com")
        os.makedirs(newborn)
        dirs = skillsync.config_dirs(claude_root=self.croot,
                                     default_claude=self.default,
                                     seats_root=self.seats)
        r = skillsync.sync(canon=self.canon, dirs=dirs,
                           backup_root=self.backup, apply=True)
        self.assertEqual(r["failed"], [])
        s = os.path.join(newborn, "skills")
        self.assertTrue(os.path.islink(s))
        self.assertIn("personal-skill", os.listdir(s))

    def test_missing_canonical_is_a_loud_error(self):
        r = skillsync.sync(canon=os.path.join(self.tmp, "gone"),
                           dirs=self.dirs, backup_root=self.backup, apply=True)
        self.assertIn("error", r)
        # and nothing moved
        self.assertFalse(os.path.islink(
            os.path.join(self.croot, "member-example-com", "skills")))

    def test_wire_refuses_to_shadow_an_unmerged_skill(self):
        """wire() alone (no merge) must refuse a swap that would hide a
        skill canonical lacks — the never-lose-a-skill floor."""
        cdir = os.path.join(self.croot, "member-example-com")
        action, detail = skillsync.wire("member-example-com", cdir, self.canon,
                                        self.backup, apply=True)
        self.assertEqual(action, "FAIL")
        self.assertIn("personal-skill", detail)
        self.assertFalse(os.path.islink(os.path.join(cdir, "skills")))

    def test_indirect_symlink_normalized(self):
        """A chain (seat -> home dir -> ... ) is rewritten to a DIRECT link,
        so archiving the intermediate home can't strand the seat."""
        self._sync(apply=True)
        seat_s = os.path.join(self.seats, "codex", "claude", "skills")
        self.assertEqual(os.readlink(seat_s), os.path.realpath(self.canon))


class CanonicalResolutionTest(unittest.TestCase):
    def test_env_canonical_overrides_default(self):
        tmp = tempfile.mkdtemp(prefix="helm-deck-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = os.environ.get("HELM_SKILLS_CANONICAL")
        os.environ["HELM_SKILLS_CANONICAL"] = tmp
        try:
            self.assertEqual(skillsync.canonical(), os.path.realpath(tmp))
        finally:
            if old is None:
                del os.environ["HELM_SKILLS_CANONICAL"]
            else:
                os.environ["HELM_SKILLS_CANONICAL"] = old

    def test_deck_env_never_steers_sync(self):
        """HELM_SKILL_DECK (home_create's content-source knob) points at a
        git-tracked repo dir on the live host — sync must NOT merge local
        strays there. Only HELM_SKILLS_CANONICAL (or the authored host) steers
        the hub; the deck never does."""
        deck_old = os.environ.get("HELM_SKILL_DECK")
        os.environ["HELM_SKILL_DECK"] = "/somewhere/tracked/repo/skills"
        old = os.environ.pop("HELM_SKILLS_CANONICAL", None)
        try:
            # No canonical configured (env unset; the hermetic HELM_HOME carries
            # no authored `host.skills_canonical`) and a deck IS set: canonical()
            # is None — the deck is NEVER a fallback. If it wrongly steered the
            # hub, this would return the deck path instead of None.
            self.assertIsNone(skillsync.canonical())
            # and when a hub IS configured, THAT wins — still never the deck:
            os.environ["HELM_SKILLS_CANONICAL"] = "/x/real-hub"
            self.assertEqual(skillsync.canonical(), os.path.realpath("/x/real-hub"))
        finally:
            if deck_old is None:
                os.environ.pop("HELM_SKILL_DECK", None)
            else:
                os.environ["HELM_SKILL_DECK"] = deck_old
            os.environ.pop("HELM_SKILLS_CANONICAL", None)
            if old is not None:
                os.environ["HELM_SKILLS_CANONICAL"] = old


if __name__ == "__main__":
    unittest.main()
