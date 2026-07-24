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
        homes.LEGACY_ARCHIVE_ROOT = j("legacy-home-archive")
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
    def _plant_claude_home(self, name, email, authed=True, token=None):
        """A fake home: identity METADATA only (.claude.json oauthAccount) plus an
        empty auth-file placeholder — never real token contents. `token` plants a
        FAKE refresh token for the shared-family (content-hash) tests."""
        d = os.path.join(homes.ROOTS["claude"], name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": email}}, f)
        if authed:
            with open(os.path.join(d, ".credentials.json"), "w") as f:
                if token:
                    json.dump({"claudeAiOauth": {"refreshToken": token}}, f)
        return d

    def _plant_codex_home(self, name, token):
        d = os.path.join(homes.ROOTS["codex"], name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "auth.json"), "w") as f:
            json.dump({"tokens": {"refresh_token": token}}, f)
        return d

    # -- canon: name folding ----------------------------------------------
    def test_canonical_name_folding(self):
        self.assertEqual(homes.canonical_name("Owner@example.com"), "owner-example-com")
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

    def test_default_home_identity_sharing_is_by_design(self):
        # the orchestrator compromise: the provider DEFAULT carries managed
        # cred for env-less launches; the SAME identity may hold a named home
        # for pinned processes. Described, never flagged as a violation.
        d = homes.DEFAULTS["claude"]
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "shared@user.dev"}}, f)
        open(os.path.join(d, ".credentials.json"), "w").close()
        # create: a default-seated identity may still get a named home
        res = homes.home_create("claude", "shared@user.dev")
        self.assertNotIn("error", res)
        # seat it (fresh login simulated), then: described, not flagged
        self._plant_claude_home("shared-user-dev", "shared@user.dev")
        rows = {r["name"]: r for r in homes.homes_list()}
        named = homes._hygiene_flags(rows["shared-user-dev"])
        self.assertTrue(any("by design" in f for f in named), named)
        self.assertFalse(any(f.startswith("dup:") for f in named), named)
        # verify: no survivor demand for the default pattern
        res = homes.home_verify("shared-user-dev")
        self.assertFalse(any("picks a survivor" in f for f in res["fixes"]), res["fixes"])

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

    # -- legacy archives: read both, write new ------------------------
    def test_legacy_archive_listed_and_restorable(self):
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

    # -- shared-family: the revocation-bomb content-hash audit ---------------
    def test_shared_family_flags_byte_copies_never_distinct_logins(self):
        tok = "fake-refresh-token-abc123"
        self._plant_claude_home("copy-a", "a@x.com", token=tok)
        self._plant_claude_home("copy-b", "b@y.com", token=tok)
        self._plant_claude_home("solo-c", "c@z.com", token="a-different-family")
        rows = {r["name"]: r for r in homes.homes_list()}
        self.assertEqual(rows["copy-a"]["shared_family"], ["copy-b"])
        self.assertEqual(rows["copy-b"]["shared_family"], ["copy-a"])
        self.assertNotIn("shared_family", rows["solo-c"])
        # verify surfaces it as a fix; verdict demoted
        res = homes.home_verify("copy-a")
        self.assertEqual(res["verdict"], "issues")
        self.assertEqual(res["checks"]["shared_family"], ["copy-b"])
        self.assertTrue(any("BYTE-COPIES" in f and "copy-b" in f for f in res["fixes"]))
        # the hygiene flag is the loud one
        self.assertIn("SHARED-FAMILY:copy-b", homes._hygiene_flags(rows["copy-a"]))

    def test_shared_family_codex_and_cross_provider_isolation(self):
        # same bytes on claude AND codex homes: families group PER PROVIDER —
        # a cross-provider byte-coincidence must never merge
        tok = "fake-codex-refresh-token"
        self._plant_codex_home("cx-a", tok)
        self._plant_codex_home("cx-b", tok)
        self._plant_claude_home("cl-x", "x@x.com", token=tok)
        rows = {r["name"]: r for r in homes.homes_list()}
        self.assertEqual(rows["cx-a"]["shared_family"], ["cx-b"])
        self.assertNotIn("shared_family", rows["cl-x"])

    def test_token_bytes_never_surface_only_the_digest_prefix(self):
        tok = "super-secret-refresh-token-value"
        self._plant_claude_home("leak-a", "a@x.com", token=tok)
        self._plant_claude_home("leak-b", "b@y.com", token=tok)
        rows = [r for r in homes.homes_list() if not r["archived"]]
        dumped = json.dumps(rows)
        self.assertNotIn(tok, dumped)
        fam = next(r["family"] for r in rows if r["name"] == "leak-a")
        self.assertRegex(fam, r"^[0-9a-f]{10}$")
        self.assertNotIn(tok, json.dumps(homes.home_verify("leak-a")))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            homes.cmd_homes([])
        self.assertNotIn(tok, out.getvalue())
        self.assertIn("SHARED-FAMILY", out.getvalue())

    def test_doctor_check_cred_families(self):
        from helm import doctor
        # distinct families -> one OK line
        self._plant_claude_home("ok-a", "a@x.com", token="family-one")
        self._plant_claude_home("ok-b", "b@y.com", token="family-two")
        res = doctor.check_cred_families()
        self.assertEqual([lvl for lvl, _ in res], [doctor.OK])
        self.assertIn("2 distinct across 2 authed homes", res[0][1])
        # a byte-copy pair -> one FAIL per family group, not per home
        self._plant_claude_home("bomb-a", "c@x.com", token="copied-family")
        self._plant_claude_home("bomb-b", "d@y.com", token="copied-family")
        res = doctor.check_cred_families()
        fails = [m for lvl, m in res if lvl == doctor.FAIL]
        self.assertEqual(len(fails), 1)
        self.assertIn("bomb-a, bomb-b", fails[0])
        self.assertIn("BYTE-COPIES", fails[0])
        self.assertNotIn("copied-family", json.dumps(res))  # never the bytes

    def test_doctor_check_cred_families_degrades_to_warn(self):
        from helm import doctor
        with mock.patch.object(homes, "homes_list", side_effect=OSError("boom")):
            res = doctor.check_cred_families()
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("unavailable", res[0][1])

    # -- identity readers: metadata only, malformed input degrades to None --
    def _plant_codex_identity(self, name, id_token=None, nested=True):
        d = os.path.join(homes.ROOTS["codex"], name)
        os.makedirs(d, exist_ok=True)
        auth = {}
        if id_token is not None:
            auth = {"tokens": {"id_token": id_token}} if nested else {"id_token": id_token}
        with open(os.path.join(d, "auth.json"), "w") as f:
            json.dump(auth, f)
        return d

    def _jwt(self, claims):
        import base64
        seg = lambda o: base64.urlsafe_b64encode(
            json.dumps(o).encode()).decode().rstrip("=")
        return seg({"alg": "none"}) + "." + seg(claims) + ".sig"

    def test_codex_identity_reads_email_claim_nested_and_flat(self):
        tok = self._jwt({"email": "cx@user.dev", "sub": "x"})
        self._plant_codex_identity("nested-cx", tok, nested=True)
        self._plant_codex_identity("flat-cx", tok, nested=False)
        rows = {r["name"]: r for r in homes.homes_list()}
        self.assertEqual(rows["nested-cx"]["identity"], "cx@user.dev")
        self.assertEqual(rows["flat-cx"]["identity"], "cx@user.dev")

    def test_codex_identity_malformed_degrades_to_none_never_raises(self):
        for name, tok in (("garbage", "not-a-jwt"),
                          ("bad-b64", "a.!!!.c"),
                          ("no-email", self._jwt({"sub": "x"})),
                          ("empty-email", self._jwt({"email": ""})),
                          ("non-dict-claims", "a." + __import__("base64").urlsafe_b64encode(b"[1,2]").decode() + ".c")):
            self._plant_codex_identity(name, tok)
        rows = {r["name"]: r for r in homes.homes_list()}
        for name in ("garbage", "bad-b64", "no-email", "empty-email", "non-dict-claims"):
            self.assertIsNone(rows[name]["identity"], name)

    def test_claude_identity_requires_string_email(self):
        d = os.path.join(homes.ROOTS["claude"], "bad-ident")
        os.makedirs(d)
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": 42}}, f)
        open(os.path.join(d, ".credentials.json"), "w").close()
        row = next(r for r in homes.homes_list() if r["name"] == "bad-ident")
        self.assertIsNone(row["identity"])

    # -- duplicate identity: named-vs-named only demands a survivor ---------
    def test_duplicate_named_homes_flag_and_verify_demands_survivor(self):
        self._plant_claude_home("dup-one", "same@user.dev")
        self._plant_claude_home("dup-two", "same@user.dev")
        rows = {r["name"]: r for r in homes.homes_list()}
        self.assertEqual(sorted(rows["dup-one"]["duplicate_identity"]), ["dup-two"])
        self.assertEqual(sorted(rows["dup-two"]["duplicate_identity"]), ["dup-one"])
        res = homes.home_verify("dup-one")
        self.assertEqual(res["verdict"], "issues")
        self.assertTrue(any("picks a survivor" in f and "dup-two" in f
                            for f in res["fixes"]))
        self.assertIn("dup:dup-two", homes._hygiene_flags(rows["dup-one"]))
        # and home_create refuses a THIRD seat for the same identity
        res = homes.home_create("claude", "same@user.dev")
        self.assertIn("already seated", res["error"])

    def test_unauthed_homes_never_join_dup_scan(self):
        # no auth file => no identity claim counts: three empty homes with the
        # same PLANTED .claude.json email but no credentials must not flag
        for n in ("ua-1", "ua-2"):
            self._plant_claude_home(n, "same@user.dev", authed=False)
        rows = homes.homes_list()
        for r in rows:
            self.assertNotIn("duplicate_identity", r)

    # -- prepare: an existing home holding a DIFFERENT identity refuses -----
    def test_prepare_refuses_home_holding_another_identity(self):
        self._plant_claude_home("taken-user-dev", "taken@user.dev", authed=True)
        res = homes.home_create("claude", "other@user.dev")
        # the canonical name for other@user.dev is free — the collision is on
        # a DIFFERENT email folding to an occupied dir
        self.assertNotIn("error", res)
        # now the real case: dir for the email exists and holds someone else
        d = os.path.join(homes.ROOTS["claude"], "clash-user-dev")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "squatter@elsewhere.io"}}, f)
        res = homes.home_create("claude", "clash@user.dev")
        self.assertIn("already holds squatter@elsewhere.io", res["error"])

    # -- _resolve: name, alias, path; ambiguity demands a provider ----------
    def test_resolve_by_alias_and_path(self):
        d = self._plant_claude_home("real-user-dev", "real@user.dev")
        os.symlink(d, os.path.join(homes.ROOTS["claude"], "shortcut"))
        row, err = homes._resolve("shortcut")
        self.assertIsNone(err)
        self.assertEqual(row["name"], "real-user-dev")
        row2, err2 = homes._resolve(d)  # by absolute path
        self.assertIsNone(err2)
        self.assertEqual(row2["name"], "real-user-dev")

    def test_resolve_same_name_two_providers_is_ambiguous(self):
        self._plant_claude_home("twin", "a@x.com")
        self._plant_codex_home("twin", "tok")
        row, err = homes._resolve("twin")
        self.assertIsNone(row)
        self.assertIn("ambiguous", err["error"])
        row, err = homes._resolve("twin", "codex")
        self.assertIsNone(err)
        self.assertEqual(row["provider"], "codex")

    def test_resolve_unknown_and_empty_names(self):
        _, err = homes._resolve("ghost")
        self.assertIn("unknown home", err["error"])
        _, err = homes._resolve("   ")
        self.assertIn("need a home name", err["error"])
        _, err = homes._resolve("x", "not-a-provider")
        self.assertIn("unknown provider", err["error"])

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
