import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

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
        self.canon = j(self.tmp, "mc-hub", "skills")
        _mk_skill(self.canon, "build", "the build skill", age=9000)
        os.makedirs(j(self.canon, ".system"))          # hidden: stays private
        self.backup = j(self.tmp, "premerge-backup")
        self.croot = j(self.tmp, "claude-homes")

        # simbi: symlink-farm home + STRANDED real skills (the audhd class)
        simbi = j(self.croot, "team-example-com", "skills")
        os.makedirs(simbi)
        os.symlink(j(self.canon, "build"), j(simbi, "build"))
        _mk_skill(simbi, "i-have-audhd", "stranded", age=100)
        _mk_skill(simbi, "orca-cli", "orca newest", age=50)
        os.symlink(j(self.croot, "team-example-com"), j(self.croot, "team-example"))

        # cto: already a whole-dir symlink to canonical (idempotent no-op)
        cto = j(self.croot, "cto-example-com")
        os.makedirs(cto)
        os.symlink(self.canon, j(cto, "skills"))

        # fresh: a credhome with no skills dir at all
        os.makedirs(j(self.croot, "fresh-com"))

        # default ~/.claude: real dir w/ a stray SYMLINK skill + older dupe
        self.agents = j(self.tmp, "agents-skills")
        _mk_skill(self.agents, "orca-cli", "orca older", age=5000)
        self.default = j(self.tmp, "dot-claude")
        dsk = j(self.default, "skills")
        os.makedirs(dsk)
        os.symlink(j(self.agents, "orca-cli"), j(dsk, "orca-cli"))
        _mk_skill(dsk, "fleet-usage", "fleet usage", age=200)
        # and a skill NEWER than canonical's copy (newest-wins replacement)
        _mk_skill(dsk, "build", "the build skill, newer edit", age=10)

        # seats: family dir linked at another home's dir (the live chain
        # shape), one instance with nothing
        self.seats = j(self.tmp, "seats")
        cdx = j(self.seats, "codex", "claude")
        os.makedirs(cdx)
        os.symlink(simbi, j(cdx, "skills"))
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
        self.assertEqual(labels.count("team-example-com"), 1)
        self.assertNotIn("team-example", labels)          # alias folded
        self.assertIn("default-claude", labels)
        self.assertIn("seat:codex", labels)
        self.assertTrue(any("codex-2" in l for l in labels))
        self.assertFalse(any("smoke" in l for l in labels))

    def test_dry_run_touches_nothing(self):
        r = self._sync(apply=False)
        self.assertEqual(r["failed"], [])
        names = {n for n, _ in r["merged"]}
        self.assertEqual(names, {"i-have-audhd", "orca-cli", "fleet-usage", "build"})
        self.assertFalse(os.path.exists(os.path.join(self.canon, "i-have-audhd")))
        self.assertFalse(os.path.islink(
            os.path.join(self.croot, "team-example-com", "skills")))
        self.assertFalse(os.path.exists(self.backup))

    def test_apply_unions_and_wires_everything(self):
        r = self._sync(apply=True)
        self.assertEqual(r["failed"], [])
        # union: the stranded skill is canonical now
        self.assertTrue(os.path.isfile(
            os.path.join(self.canon, "i-have-audhd", "SKILL.md")))
        # newest-wins: simbi's orca-cli (newer) beat the default home's symlink
        with open(os.path.join(self.canon, "orca-cli", "SKILL.md")) as f:
            self.assertEqual(f.read(), "orca newest")
        # newest-wins vs canonical itself: the newer 'build' replaced it,
        # the displaced copy is in the backup shelf
        with open(os.path.join(self.canon, "build", "SKILL.md")) as f:
            self.assertEqual(f.read(), "the build skill, newer edit")
        shelf = os.path.join(self.backup, "canonical-displaced")
        self.assertTrue(any(e.startswith("build-") for e in os.listdir(shelf)))
        # stray symlink skill adopted (as a copy or link, name present)
        self.assertIn("fleet-usage", os.listdir(self.canon))
        # every config dir is now a DIRECT symlink to canonical
        for _label, cdir in self.dirs:
            s = os.path.join(cdir, "skills")
            self.assertTrue(os.path.islink(s), s)
            self.assertEqual(os.path.realpath(s), os.path.realpath(self.canon), s)
        # superset: every skill visible in simbi before is visible after
        view = set(os.listdir(os.path.join(self.croot, "team-example-com", "skills")))
        self.assertLessEqual({"build", "i-have-audhd", "orca-cli"}, view)
        # the original real dirs survive whole in the backup root
        saved = os.path.join(self.backup, "team-example-com")
        snap = os.listdir(saved)
        self.assertEqual(len(snap), 1)
        self.assertTrue(os.path.isfile(os.path.join(
            saved, snap[0], "i-have-audhd", "SKILL.md")))
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
        self.assertIn("i-have-audhd", os.listdir(s))

    def test_missing_canonical_is_a_loud_error(self):
        r = skillsync.sync(canon=os.path.join(self.tmp, "gone"),
                           dirs=self.dirs, backup_root=self.backup, apply=True)
        self.assertIn("error", r)
        # and nothing moved
        self.assertFalse(os.path.islink(
            os.path.join(self.croot, "team-example-com", "skills")))

    def test_wire_refuses_to_shadow_an_unmerged_skill(self):
        """wire() alone (no merge) must refuse a swap that would hide a
        skill canonical lacks — the never-lose-a-skill floor."""
        cdir = os.path.join(self.croot, "team-example-com")
        action, detail = skillsync.wire("team-example-com", cdir, self.canon,
                                        self.backup, apply=True)
        self.assertEqual(action, "FAIL")
        self.assertIn("i-have-audhd", detail)
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
        strays there. Only HELM_SKILLS_CANONICAL steers the hub."""
        deck_old = os.environ.get("HELM_SKILL_DECK")
        os.environ["HELM_SKILL_DECK"] = "/somewhere/tracked/repo/skills"
        old = os.environ.pop("HELM_SKILLS_CANONICAL", None)
        try:
            # canonical() realpaths the default (mint vs sync must agree on the
            # path string); the deck still never steers it.
            self.assertEqual(
                skillsync.canonical(),
                os.path.realpath(skillsync.MC_CANONICAL))
        finally:
            if deck_old is None:
                del os.environ["HELM_SKILL_DECK"]
            else:
                os.environ["HELM_SKILL_DECK"] = deck_old
            if old is not None:
                os.environ["HELM_SKILLS_CANONICAL"] = old


if __name__ == "__main__":
    unittest.main()
