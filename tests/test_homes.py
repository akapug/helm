"""Hermetic tests for helm.homes — every root points at tmp dirs; the real
~/.claude-homes / ~/.codex-homes / archives are never touched, no login runs."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import homes


class HomesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-homes-")
        j = lambda *p: os.path.join(self.tmp, *p)
        self._orig = {k: getattr(homes, k) for k in
                      ("ROOTS", "DEFAULTS", "SHARED_PROJECTS",
                       "ARCHIVE_ROOT", "LEGACY_ARCHIVE_ROOT", "_agent_procs")}
        homes.ROOTS = {"claude": j("claude-homes"), "codex": j("codex-homes")}
        homes.DEFAULTS = {"claude": j("default-claude"), "codex": j("default-codex")}
        homes.SHARED_PROJECTS = j("default-claude", "projects")
        homes.ARCHIVE_ROOT = j("helm-home-archive")
        homes.LEGACY_ARCHIVE_ROOT = j("sesh-home-archive")
        homes._agent_procs = lambda: []
        for r in homes.ROOTS.values():
            os.makedirs(r)
        # hermetic: a real HELM_SKILL_DECK on the host must not leak into tests
        self._envpatch = mock.patch.dict(os.environ)
        self._envpatch.start()
        os.environ.pop("HELM_SKILL_DECK", None)
        os.environ.pop("MELD_SKILL_DECK", None)

    def tearDown(self):
        self._envpatch.stop()
        for k, v in self._orig.items():
            setattr(homes, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def _plant_claude_home(self, name, email, authed=True):
        """A fake home: identity METADATA only (.claude.json oauthAccount) plus an
        empty auth-file placeholder — never real token contents."""
        d = os.path.join(homes.ROOTS["claude"], name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": email}}, f)
        if authed:
            open(os.path.join(d, ".credentials.json"), "w").close()
        return d

    # -- canon: name folding ----------------------------------------------
    def test_canonical_name_folding(self):
        self.assertEqual(homes.canonical_name("David@X.com"), "david-x-com")
        self.assertEqual(homes.canonical_name("a.b+c@d-e.io"), "a-b-c-d-e-io")
        self.assertEqual(homes.canonical_name(""), "")

    # -- prepare: home + projects symlink, login printed never run ---------
    def test_prepare_creates_claude_home_with_projects_symlink(self):
        res = homes.home_create("claude", "new@user.dev")
        self.assertNotIn("error", res)
        self.assertEqual(res["name"], "new-user-dev")
        self.assertTrue(os.path.isdir(res["home"]))
        pl = os.path.join(res["home"], "projects")
        self.assertTrue(os.path.islink(pl))
        self.assertEqual(os.path.realpath(pl), os.path.realpath(homes.SHARED_PROJECTS))
        self.assertIn("claude /login", res["login_cmd"])       # printed, not run
        self.assertIn(res["home"], res["login_cmd"])
        # no credentials were minted or seated
        self.assertFalse(os.path.exists(os.path.join(res["home"], ".credentials.json")))
        # idempotent re-prepare
        self.assertTrue(homes.home_create("claude", "new@user.dev")["existing"])

    def test_prepare_links_skill_deck_additively(self):
        deck = os.path.join(self.tmp, "deck")
        os.makedirs(os.path.join(deck, "learn"))
        with open(os.path.join(deck, "learn", "SKILL.md"), "w") as f:
            f.write("# learn")
        os.makedirs(os.path.join(deck, "not-a-skill"))  # no SKILL.md — never linked
        os.environ["HELM_SKILL_DECK"] = deck
        res = homes.home_create("claude", "deck@user.dev")
        self.assertNotIn("error", res)
        sd = os.path.join(res["home"], "skills")
        link = os.path.join(sd, "learn")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(deck, "learn")))
        self.assertFalse(os.path.lexists(os.path.join(sd, "not-a-skill")))
        self.assertIn("skill deck: linked 1", res["note"])
        # additive idempotence: re-create leaves the link, reports it existing
        res2 = homes.home_create("claude", "deck@user.dev")
        self.assertIn("left 1 existing", res2["note"])
        self.assertTrue(os.path.islink(link))
        # a home-local entry is never touched: plant one, add a deck twin
        local = os.path.join(sd, "local-skill")
        os.makedirs(local)
        os.makedirs(os.path.join(deck, "local-skill"))
        with open(os.path.join(deck, "local-skill", "SKILL.md"), "w") as f:
            f.write("# deck twin")
        homes.home_create("claude", "deck@user.dev")
        self.assertFalse(os.path.islink(local))  # real dir survives, link not forced

    def test_prepare_without_deck_env_provisions_nothing(self):
        res = homes.home_create("claude", "nodeck@user.dev")
        self.assertNotIn("error", res)
        self.assertFalse(os.path.lexists(os.path.join(res["home"], "skills")))

    def test_prepare_codex_home_no_symlink(self):
        res = homes.home_create("codex", "cx@user.dev")
        self.assertNotIn("error", res)
        self.assertIn("codex login --device-auth", res["login_cmd"])
        self.assertFalse(os.path.lexists(os.path.join(res["home"], "projects")))

    # -- verify: the name-vs-login mismatch audit --------------------------
    def test_verify_flags_name_identity_mismatch(self):
        self._plant_claude_home("wrong-name", "real@person.io")
        res = homes.home_verify("wrong-name")
        self.assertNotIn("error", res)
        self.assertEqual(res["verdict"], "issues")
        self.assertIs(res["checks"]["canonical"], False)
        self.assertTrue(any("real-person-io" in f for f in res["fixes"]))
        # the row-level audit flags it too
        row = next(r for r in homes.homes_list() if r["name"] == "wrong-name")
        self.assertIn("name-lies(want real-person-io)", homes._hygiene_flags(row))

    def test_verify_ok_when_canonical_and_linked(self):
        d = self._plant_claude_home("ok-person-io", "ok@person.io")
        os.makedirs(homes.SHARED_PROJECTS, exist_ok=True)
        os.symlink(homes.SHARED_PROJECTS, os.path.join(d, "projects"))
        self.assertEqual(homes.home_verify("ok-person-io")["verdict"], "ok")

    # -- archive: reversible move, refused while live ----------------------
    def test_archive_refuses_live_then_moves_and_restores(self):
        d = os.path.join(homes.ROOTS["codex"], "busy-user-dev")
        os.makedirs(d)
        homes._agent_procs = lambda: [(4242, "codex", os.path.realpath(d))]
        res = homes.home_archive("busy-user-dev")
        self.assertIn("REFUSED", res["error"])
        self.assertTrue(os.path.isdir(d))                      # untouched
        homes._agent_procs = lambda: []
        res = homes.home_archive("busy-user-dev")
        self.assertTrue(res["ok"])
        self.assertFalse(os.path.lexists(d))                   # moved, not copied
        self.assertTrue(res["archived_to"].startswith(homes.ARCHIVE_ROOT))
        with open(os.path.join(res["archived_to"], homes.MARKER)) as f:
            meta = json.load(f)
        self.assertEqual(meta["name"], "busy-user-dev")
        self.assertEqual(meta["from"], os.path.realpath(d))
        # reversible: restore lands it back where it came from, marker gone
        back = homes.home_unarchive("busy-user-dev")
        self.assertTrue(back["ok"])
        self.assertEqual(back["restored_to"], os.path.realpath(d))
        self.assertTrue(os.path.isdir(d))
        self.assertFalse(os.path.exists(os.path.join(d, homes.MARKER)))

    # -- legacy sesh archives: read both, write new ------------------------
    def test_legacy_sesh_archive_listed_and_restorable(self):
        origin = os.path.join(homes.ROOTS["codex"], "old-codex")
        legacy = os.path.join(homes.LEGACY_ARCHIVE_ROOT, "old-codex-20250101")
        os.makedirs(legacy)
        with open(os.path.join(legacy, homes.LEGACY_MARKER), "w") as f:
            json.dump({"name": "old-codex", "provider": "codex", "from": origin,
                       "archived_at": "2025-01-01T00:00:00", "aliases": []}, f)
        rows = [r for r in homes.homes_list() if r["archived"]]
        self.assertEqual([r["name"] for r in rows], ["old-codex"])
        self.assertEqual(rows[0]["provider"], "codex")
        res = homes.home_unarchive("old-codex")
        self.assertTrue(res["ok"])
        self.assertEqual(res["restored_to"], origin)
        self.assertFalse(os.path.exists(os.path.join(origin, homes.LEGACY_MARKER)))

    # -- CLI leg smoke ------------------------------------------------------
    def test_cmd_homes_list_and_archives(self):
        self._plant_claude_home("wrong-name", "real@person.io")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = homes.cmd_homes([])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("helm homes:"))
        self.assertIn("name-lies", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = homes.cmd_homes(["archives"])
        self.assertEqual(rc, 0)
        self.assertIn("no archives", out.getvalue())


if __name__ == "__main__":
    unittest.main()
