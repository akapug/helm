import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import seat  # noqa: E402,F401 — the facade seeds the late-bound names before seat_launch_assets is imported
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
        self.canon = j(self.tmp, "canon-hub", "skills")
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
        _mk_skill(dsk, "fleet-usage", "fleet usage", age=200)
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

    @unittest.skipIf(os.geteuid() == 0, "root lists a mode-000 dir; the witness needs EACCES")
    def test_census_names_an_unlistable_subtree_instead_of_omitting_it(self):
        """The class review witnessed: a glob-based census swallowed the
        OSError on a subtree it could not list and returned a SHORTER list
        that read exactly like a smaller estate. Now the census carries what
        it could not enumerate, with the errno name, and the sync report
        carries it through — a run over it never claims a wired estate."""
        inst = os.path.join(self.seats, "codex", "instances")
        readable = skillsync.config_dirs(claude_root=self.croot,
                                         default_claude=self.default,
                                         seats_root=self.seats)
        self.assertEqual(readable.unlisted, [])                 # the control
        under = lambda census: [p for _l, p in census if p.startswith(inst + os.sep)]
        self.assertEqual(len(under(readable)), 1)      # the instance dir is censused
        os.chmod(inst, 0)
        self.addCleanup(os.chmod, inst, 0o755)
        hidden = skillsync.config_dirs(claude_root=self.croot,
                                       default_claude=self.default,
                                       seats_root=self.seats)
        self.assertEqual(under(hidden), [])            # omitted from the list …
        self.assertEqual(hidden.unlisted, [(inst, "EACCES")])   # … and SAID
        self.assertIn("seat:codex", [l for l, _ in hidden])   # the rest still found
        r = skillsync.sync(canon=self.canon, dirs=hidden,
                           backup_root=self.backup, apply=False)
        self.assertEqual(r["unlisted"], [(inst, "EACCES")])
        r = skillsync.sync(canon=self.canon, dirs=readable,
                           backup_root=self.backup, apply=False)
        self.assertEqual(r["unlisted"], [])

    def test_dry_run_touches_nothing(self):
        r = self._sync(apply=False)
        self.assertEqual(r["failed"], [])
        names = {n for n, _ in r["merged"]}
        self.assertEqual(names, {"personal-skill", "orca-cli", "fleet-usage", "build"})
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
        self.assertIn("fleet-usage", os.listdir(self.canon))
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


class DanglingCanonicalLinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-skillsync-dangling-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.canon = os.path.join(self.tmp, "canonical")
        self.backup = os.path.join(self.tmp, "backup")
        os.makedirs(self.canon)

    def _home(self, name):
        cdir = os.path.join(self.tmp, "homes", name)
        os.makedirs(os.path.join(cdir, "skills"))
        return cdir

    def test_unique_home_link_repairs_canonical_as_absolute(self):  # noqa: VACUOUS_ASSERTION — the absolute readlink, live target directory, and displaced backup positively control repair
        target = _mk_skill(os.path.join(self.tmp, "sources"), "orca-cli",
                           "external owner")
        cdir = self._home("one")
        os.symlink(target, os.path.join(cdir, "skills", "orca-cli"))
        broken = os.path.join(self.canon, "orca-cli")
        os.symlink("../gone/orca-cli", broken)

        r = skillsync.sync(canon=self.canon, dirs=[("one", cdir)],
                           backup_root=self.backup, apply=True)
        self.assertEqual(r["failed"], [])
        self.assertTrue(os.path.islink(broken))
        self.assertEqual(os.readlink(broken), os.path.realpath(target))
        self.assertTrue(os.path.isdir(os.path.realpath(broken)))
        shelf = os.path.join(self.backup, "canonical-displaced")
        self.assertTrue(any(name.startswith("orca-cli-")
                            for name in os.listdir(shelf)))

    def test_relative_link_reanchors_under_known_skills_path(self):  # noqa: VACUOUS_ASSERTION — the pre-repair broken control and final absolute readlink positively control re-anchoring
        target = _mk_skill(os.path.join(self.tmp, "legacy"), "only-here",
                           "historical owner")
        cdir = self._home("one")
        raw = os.path.relpath(target, os.path.join(cdir, "skills"))
        broken = os.path.join(self.canon, "only-here")
        os.symlink(raw, broken)
        self.assertFalse(os.path.exists(os.path.realpath(broken)),
                         "control: raw link must be broken from canonical")

        with mock.patch.object(skillsync, "_newest",
                               side_effect=AssertionError("repair walked target")):
            r = skillsync.sync(canon=self.canon, dirs=[("one", cdir)],
                               backup_root=self.backup, apply=True)
        self.assertEqual(r["failed"], [])
        self.assertEqual(os.readlink(broken), os.path.realpath(target))

    def test_managed_home_target_is_copied_before_wire_moves_it(self):  # noqa: VACUOUS_ASSERTION — real copied content and both final whole-dir links positively control the managed-target branch
        owner = self._home("owner")
        target = _mk_skill(os.path.join(owner, "skills"), "local", "owned here")
        alias = self._home("alias")
        os.symlink(target, os.path.join(alias, "skills", "local"))
        broken = os.path.join(self.canon, "local")
        os.symlink("../gone/local", broken)

        r = skillsync.sync(canon=self.canon,
                           dirs=[("owner", owner), ("alias", alias)],
                           backup_root=self.backup, apply=True)
        self.assertEqual(r["failed"], [])
        self.assertFalse(os.path.islink(broken))
        with open(os.path.join(broken, "SKILL.md")) as f:
            self.assertEqual(f.read(), "owned here")
        self.assertTrue(os.path.islink(os.path.join(owner, "skills")))
        self.assertTrue(os.path.islink(os.path.join(alias, "skills")))

    def test_reanchored_canonical_target_is_refused_as_recursive(self):  # noqa: VACUOUS_ASSERTION — the intentionally broken raw link and exact zero-target refusal positively control no mutation
        cdir = self._home("one")
        raw = os.path.relpath(self.canon, os.path.join(cdir, "skills"))
        broken = os.path.join(self.canon, "recursive")
        os.symlink(raw, broken)
        self.assertFalse(os.path.exists(os.path.realpath(broken)),
                         "control: link is broken from the canonical anchor")

        r = skillsync.sync(canon=self.canon, dirs=[("one", cdir)],
                           backup_root=self.backup, apply=True)
        self.assertEqual(len(r["failed"]), 1)
        self.assertIn("0 resolvable targets", r["failed"][0][2])
        self.assertEqual(os.readlink(broken), raw)
        self.assertFalse(os.path.exists(self.backup))

    def test_reanchored_canonical_ancestor_is_refused_as_recursive(self):  # noqa: VACUOUS_ASSERTION — the broken-from-canonical control and zero-target refusal positively control ancestor exclusion
        target = os.path.join(self.tmp, "estate")
        canon = os.path.join(target, "canonical")
        os.makedirs(canon)
        cdir = os.path.join(self.tmp, "homes", "deep", "one")
        os.makedirs(os.path.join(cdir, "skills"))
        raw = os.path.relpath(target, os.path.join(cdir, "skills"))
        broken = os.path.join(canon, "recursive-parent")
        os.symlink(raw, broken)
        self.assertFalse(os.path.exists(os.path.realpath(broken)),
                         "control: link is broken from the canonical anchor")

        backup = os.path.join(self.tmp, "ancestor-backup")
        r = skillsync.sync(canon=canon, dirs=[("one", cdir)],
                           backup_root=backup, apply=True)
        self.assertEqual(len(r["failed"]), 1)
        self.assertIn("0 resolvable targets", r["failed"][0][2])
        self.assertEqual(os.readlink(broken), raw)
        self.assertFalse(os.path.exists(backup))

    def test_absolute_dangling_owner_is_not_reassigned_by_name(self):  # noqa: VACUOUS_ASSERTION — the preserved absolute readlink and named absolute-owner refusal positively control no reassignment
        cdir = self._home("one")
        unrelated = _mk_skill(os.path.join(self.tmp, "other-owner"), "lost",
                              "wrong owner")
        os.symlink(unrelated, os.path.join(cdir, "skills", "lost"))
        broken = os.path.join(self.canon, "lost")
        owner = os.path.join(self.tmp, "offline-owner", "lost")
        os.symlink(owner, broken)

        r = skillsync.sync(canon=self.canon, dirs=[("one", cdir)],
                           backup_root=self.backup, apply=True)
        self.assertEqual(len(r["failed"]), 1)
        self.assertIn("absolute owner", r["failed"][0][2])
        self.assertEqual(os.readlink(broken), owner)
        self.assertFalse(os.path.exists(self.backup))

    def test_unresolvable_dangling_link_fails_without_mutation(self):
        cdir = self._home("one")
        broken = os.path.join(self.canon, "lost")
        os.symlink("../gone/lost", broken)

        r = skillsync.sync(canon=self.canon, dirs=[("one", cdir)],
                           backup_root=self.backup, apply=True)
        self.assertEqual(len(r["failed"]), 1)
        self.assertIn("0 resolvable targets", r["failed"][0][2])
        self.assertEqual(os.readlink(broken), "../gone/lost")
        self.assertFalse(os.path.islink(os.path.join(cdir, "skills")))
        self.assertFalse(os.path.exists(self.backup))
        with mock.patch.object(skillsync, "sync", return_value=r), \
                mock.patch("builtins.print"):
            self.assertEqual(skillsync.cmd_sync(["--apply"]), 1)

    def test_ambiguous_dangling_link_fails_instead_of_guessing(self):
        dirs = []
        for name in ("one", "two"):
            target = _mk_skill(os.path.join(self.tmp, "sources-" + name),
                               "shared", name)
            cdir = self._home(name)
            os.symlink(target, os.path.join(cdir, "skills", "shared"))
            dirs.append((name, cdir))
        broken = os.path.join(self.canon, "shared")
        os.symlink("../gone/shared", broken)

        r = skillsync.sync(canon=self.canon, dirs=dirs,
                           backup_root=self.backup, apply=True)
        self.assertEqual(len(r["failed"]), 1)
        self.assertIn("2 resolvable targets", r["failed"][0][2])
        self.assertEqual(os.readlink(broken), "../gone/shared")
        self.assertFalse(os.path.exists(self.backup))


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


FAIL = "the canonical skills registry is unusable"
FB = "skills come from the host fallback"


class LinkCanonicalDegradedTest(unittest.TestCase):
    """link_canonical carries the canonical read failure BESIDE a successful
    fallback link. The class: the reason lived only on the failure branch, so
    a corrupt authored registry plus a usable host fallback linked the seat
    and said nothing — a degraded canonical source nobody hears about."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-skillsync-degraded-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.host = os.path.join(self.tmp, "host-config")
        _mk_skill(os.path.join(self.host, "skills"), "learn", "host learn")
        self.cdir = os.path.join(self.tmp, "seat-under-test", "claude")
        os.makedirs(self.cdir)
        # no env canonical: the authored host block is the only source
        self.envp = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "helm-home"),
            "CLAUDE_CONFIG_DIR": self.host})
        self.envp.start()
        self.addCleanup(self.envp.stop)
        os.environ.pop("MELD_HOME", None)
        os.environ.pop("HELM_SKILLS_CANONICAL", None)
        os.environ.pop("MELD_SKILLS_CANONICAL", None)

    def _authored(self, text):
        from helm import home as _h
        os.makedirs(os.path.dirname(_h.authored_path()), exist_ok=True)
        with open(_h.authored_path(), "w") as f:
            f.write(text)

    def test_corrupt_authored_with_a_usable_fallback_links_AND_names_the_failure(self):
        self._authored("{not json")
        res = skillsync.link_canonical(self.cdir, relink=True, host_fallback=True)
        self.assertEqual(res.action, "linked")               # the must-hit: it linked
        link = os.path.join(self.cdir, "skills")
        self.assertEqual(os.readlink(link), os.path.join(self.host, "skills"))
        self.assertIn("authored layer unreadable", res.degraded)
        self.assertIn("authored layer unreadable", res.source_failure)
        # BOTH lines: the failure on its own, and the fallback delivered
        # because of it — neither claims the seat has no skills
        self.assertIn("authored layer unreadable", skillsync.failure_line(res))
        line = skillsync.degraded_line(res)
        self.assertIn("%s (%s)" % (FB, os.path.join(self.host, "skills")), line)
        self.assertNotIn("NO skills", skillsync.failure_line(res) + line)
        # the SAME estate on a second call: found, and still named, twice
        res = skillsync.link_canonical(self.cdir, relink=True, host_fallback=True)
        self.assertEqual(res.action, "ok")
        self.assertIn("authored layer unreadable", res.degraded)
        self.assertIn("authored layer unreadable", res.source_failure)

    def test_a_real_skills_dir_with_a_corrupt_authored_registry_claims_no_fallback(self):
        """Attribution control: nothing was delivered, so nothing came "from
        the fallback". The real dir is left alone, the answer is `real`, and
        the degraded line is None — the wrapper then says only that the real
        dir was left untouched. The corrupt registry is real (the linked arm
        above proves the same registry state does degrade a delivery)."""
        import contextlib
        import io
        from helm import seat_launch_assets
        self._authored("{not json")
        real = os.path.join(self.cdir, "skills")
        os.makedirs(real)
        open(os.path.join(real, "own.md"), "w").close()
        res = skillsync.link_canonical(self.cdir, relink=True, host_fallback=True)
        self.assertEqual(res.action, "real")
        self.assertIsNone(res.degraded)
        self.assertIsNone(skillsync.degraded_line(res))
        # the registry failure is a fact about the SOURCE and is still said
        # when the destination delivered nothing — the failure line alone
        self.assertIn("authored layer unreadable", res.source_failure)
        self.assertIn("authored layer unreadable", skillsync.failure_line(res))
        self.assertFalse(os.path.islink(real))
        self.assertTrue(os.path.exists(os.path.join(real, "own.md")))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            seat_launch_assets._link_skills(self.cdir)
        self.assertIn("is a REAL dir — left untouched", err.getvalue())
        self.assertIn(FAIL, err.getvalue())
        self.assertNotIn(FB, err.getvalue())

    def test_a_link_that_cannot_be_made_prints_no_success_line(self):
        """`error` delivered nothing: the config dir path is a FILE, so the
        symlink cannot be made; the degraded line is None and the wrapper
        says only that skills were not linked."""
        import contextlib
        import io
        from helm import seat_launch_assets
        self._authored("{not json")
        blocked = os.path.join(self.tmp, "blocked-config")
        open(blocked, "w").close()                      # a file where a dir must be
        res = skillsync.link_canonical(blocked, relink=True, host_fallback=True)
        self.assertEqual(res.action, "error")
        self.assertIsNone(res.degraded)
        self.assertIsNone(skillsync.degraded_line(res))
        self.assertIn("authored layer unreadable", res.source_failure)
        self.assertFalse(os.path.lexists(os.path.join(blocked, "skills")))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            seat_launch_assets._link_skills(blocked)
        self.assertIn("skills not linked into", err.getvalue())
        self.assertIn(FAIL, err.getvalue())                 # the failure, on its own
        self.assertNotIn(FB, err.getvalue())

    def test_a_foreign_link_relinked_to_the_fallback_names_the_fallback_as_source(self):
        """`relinked` delivered the fallback: the line names what the entry
        NOW points at (the host skills), never the old target that the
        detail records as "was -> old"."""
        self._authored("{not json")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        link = os.path.join(self.cdir, "skills")
        os.symlink(elsewhere, link)
        res = skillsync.link_canonical(self.cdir, relink=True, host_fallback=True)
        self.assertEqual(res.action, "relinked")
        self.assertEqual(res.detail, "was -> " + elsewhere)
        self.assertEqual(os.readlink(link), os.path.join(self.host, "skills"))
        self.assertIn("authored layer unreadable", res.degraded)
        self.assertIn("authored layer unreadable", skillsync.failure_line(res))
        line = skillsync.degraded_line(res)
        self.assertIn("host fallback (%s)" % os.path.join(self.host, "skills"), line)
        self.assertNotIn(elsewhere, line)

    def test_a_healthy_canonical_is_not_degraded(self):
        canon = os.path.join(self.tmp, "hub")
        _mk_skill(canon, "learn", "canonical learn")
        self._authored('{"host": {"skills_canonical": "%s"}}' % canon)
        res = skillsync.link_canonical(self.cdir, relink=True, host_fallback=True)
        self.assertEqual(res.action, "linked")
        self.assertEqual(res.detail, os.path.realpath(canon))
        self.assertIsNone(res.degraded)
        self.assertIsNone(res.source_failure)
        self.assertIsNone(skillsync.degraded_line(res))
        self.assertIsNone(skillsync.failure_line(res))

    def test_deliberately_unconfigured_with_a_fallback_stays_quiet(self):
        res = skillsync.link_canonical(self.cdir, relink=True, host_fallback=True)
        self.assertEqual(res.action, "linked")
        self.assertEqual(res.detail, os.path.join(self.host, "skills"))
        self.assertIsNone(res.degraded)
        self.assertIsNone(res.source_failure)
        self.assertIsNone(skillsync.degraded_line(res))
        self.assertIsNone(skillsync.failure_line(res))

    def test_corrupt_authored_with_no_fallback_is_unavailable_and_loud(self):
        self._authored("{not json")
        res = skillsync.link_canonical(self.cdir, relink=False)
        self.assertEqual(res.action, "unavailable")
        self.assertIn("authored layer unreadable", res.detail)
        self.assertIn("authored layer unreadable", res.source_failure)
        self.assertIsNone(res.degraded)
        # unavailable's own line carries the reason: one report, not two
        self.assertIsNone(skillsync.failure_line(res))
        self.assertFalse(os.path.lexists(os.path.join(self.cdir, "skills")))
        # and with the fallback asked for but absent on the host: the same
        shutil.rmtree(os.path.join(self.host, "skills"))
        res = skillsync.link_canonical(self.cdir, relink=True, host_fallback=True)
        self.assertEqual(res.action, "unavailable")
        self.assertIn("authored layer unreadable", res.detail)

