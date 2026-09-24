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


class HomesBase(unittest.TestCase):
    """A scratch estate for the homes module: the claude and codex home
    roots, their defaults, the shared projects dir and both archive roots
    point into a tmp dir, HELM_HOME is isolated and the skill-deck env is
    cleared. `_plant_claude_home` and `_plant_codex_home` create homes in
    it.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-homes-")
        j = lambda *p: os.path.join(self.tmp, *p)
        self._orig = {k: getattr(homes, k) for k in
                      ("ROOTS", "DEFAULTS", "SHARED_PROJECTS",
                       "ARCHIVE_ROOT", "LEGACY_ARCHIVE_ROOT", "LEGACY_MARKER",
                       "_agent_procs")}
        homes.ROOTS = {"claude": j("claude-homes"), "codex": j("codex-homes")}
        homes.DEFAULTS = {"claude": j("default-claude"), "codex": j("default-codex")}
        homes.SHARED_PROJECTS = j("default-claude", "projects")
        homes.ARCHIVE_ROOT = j("helm-home-archive")
        # a declared predecessor's archive: what homes.py reads where this
        # host's local names declare one (the name here is made up)
        homes.LEGACY_ARCHIVE_ROOT = j("oldtool-home-archive")
        homes.LEGACY_MARKER = ".oldtool-archive.json"
        homes._agent_procs = lambda: []
        for r in homes.ROOTS.values():
            os.makedirs(r)
        # hermetic: a real HELM_SKILL_DECK on the host must not leak into
        # tests, and neither must a real skills hub — canonical() falls back
        # to the host's authored layer, so HELM_HOME is isolated alongside
        # the env (patch.dict restores every key at tearDown)
        self._envpatch = mock.patch.dict(os.environ, {"HELM_HOME": j("helm-home")})
        self._envpatch.start()
        for k in ("HELM_SKILL_DECK", "MELD_SKILL_DECK", "HELM_SKILLS_CANONICAL",
                  "MELD_SKILLS_CANONICAL", "MELD_HOME"):
            os.environ.pop(k, None)

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


class HomesTest(HomesBase):
    """The homes arms, on HomesBase's scratch estate.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses HomesBase."""

    # -- canon: name folding ----------------------------------------------
    def test_canonical_name_folding(self):
        self.assertEqual(homes.canonical_name("Ann@x.example"), "ann-x-example")
        self.assertEqual(homes.canonical_name("a.b+c@d-e.example"), "a-b-c-d-e-example")
        self.assertEqual(homes.canonical_name(""), "")

    # -- prepare: home + projects symlink, login printed never run ---------
    def test_prepare_creates_claude_home_with_projects_symlink(self):
        res = homes.home_create("claude", "new@user.example")
        self.assertNotIn("error", res)
        self.assertEqual(res["name"], "new-user-example")
        self.assertTrue(os.path.isdir(res["home"]))
        pl = os.path.join(res["home"], "projects")
        self.assertTrue(os.path.islink(pl))
        self.assertEqual(os.path.realpath(pl), os.path.realpath(homes.SHARED_PROJECTS))
        self.assertIn("claude /login", res["login_cmd"])       # printed, not run
        self.assertIn(res["home"], res["login_cmd"])
        # no credentials were minted or seated
        self.assertFalse(os.path.exists(os.path.join(res["home"], ".credentials.json")))
        # idempotent re-prepare
        self.assertTrue(homes.home_create("claude", "new@user.example")["existing"])

    def test_prepare_links_skill_deck_additively(self):
        deck = os.path.join(self.tmp, "deck")
        os.makedirs(os.path.join(deck, "learn"))
        with open(os.path.join(deck, "learn", "SKILL.md"), "w") as f:
            f.write("# learn")
        os.makedirs(os.path.join(deck, "not-a-skill"))  # no SKILL.md — never linked
        os.environ["HELM_SKILL_DECK"] = deck
        res = homes.home_create("claude", "deck@user.example")
        self.assertNotIn("error", res)
        sd = os.path.join(res["home"], "skills")
        link = os.path.join(sd, "learn")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(deck, "learn")))
        self.assertFalse(os.path.lexists(os.path.join(sd, "not-a-skill")))
        self.assertIn("skill deck: linked 1", res["note"])
        # additive idempotence: re-create leaves the link, reports it existing
        res2 = homes.home_create("claude", "deck@user.example")
        self.assertIn("left 1 existing", res2["note"])
        self.assertTrue(os.path.islink(link))
        # a home-local entry is never touched: plant one, add a deck twin
        local = os.path.join(sd, "local-skill")
        os.makedirs(local)
        os.makedirs(os.path.join(deck, "local-skill"))
        with open(os.path.join(deck, "local-skill", "SKILL.md"), "w") as f:
            f.write("# deck twin")
        homes.home_create("claude", "deck@user.example")
        self.assertFalse(os.path.islink(local))  # real dir survives, link not forced

    def test_default_home_identity_sharing_is_by_design(self):
        # the orchestrator compromise: the provider DEFAULT carries managed
        # cred for env-less launches; the SAME identity may hold a named home
        # for pinned processes. Described, never flagged as a violation.
        d = homes.DEFAULTS["claude"]
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "shared@user.example"}}, f)
        open(os.path.join(d, ".credentials.json"), "w").close()
        # create: a default-seated identity may still get a named home
        res = homes.home_create("claude", "shared@user.example")
        self.assertNotIn("error", res)
        # seat it (fresh login simulated), then: described, not flagged
        self._plant_claude_home("shared-user-example", "shared@user.example")
        rows = {r["name"]: r for r in homes.homes_list()}
        named = homes._hygiene_flags(rows["shared-user-example"])
        self.assertTrue(any("by design" in f for f in named), named)
        self.assertFalse(any(f.startswith("dup:") for f in named), named)
        # verify: no survivor demand for the default pattern
        res = homes.home_verify("shared-user-example")
        self.assertFalse(any("picks a survivor" in f for f in res["fixes"]), res["fixes"])

    def test_prepare_without_deck_env_provisions_nothing(self):
        res = homes.home_create("claude", "nodeck@user.example")
        self.assertNotIn("error", res)
        self.assertFalse(os.path.lexists(os.path.join(res["home"], "skills")))

    # -- prepare: a credhome links the skills hub the moment it is made ------
    def _hub(self):
        """A canonical skills hub in the tmp estate, configured the way the
        host configures it (HELM_SKILLS_CANONICAL); one skill so a session
        launched on a linked home would actually see something."""
        hub = os.path.join(self.tmp, "skills-hub")
        os.makedirs(os.path.join(hub, "learn"))
        with open(os.path.join(hub, "learn", "SKILL.md"), "w") as f:
            f.write("# learn")
        os.environ["HELM_SKILLS_CANONICAL"] = hub
        return hub

    def test_prepare_links_the_skills_hub_through_the_shipped_verb(self):
        """The class: a credhome prepared without `skills -> hub` runs every
        seat launched on it with NO skills, silently. Driven through the
        shipped verb, not home_create, so the door the owner types is the
        door under test; the deck-less control above proves the link is the
        hub's and not something prepare always mints."""
        hub = self._hub()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = homes.cmd_homes(["prepare", "claude", "hub@user.example"])
        self.assertEqual(rc, 0, out.getvalue())
        link = os.path.join(homes.ROOTS["claude"], "hub-user-example", "skills")
        self.assertTrue(os.path.islink(link), out.getvalue())
        self.assertEqual(os.path.realpath(link), os.path.realpath(hub))
        self.assertIn("skills -> " + hub, out.getvalue())
        # the session's view: the hub's skill is reachable through the home
        self.assertTrue(os.path.isfile(os.path.join(link, "learn", "SKILL.md")))
        # idempotent: a second prepare leaves the link and says it is there
        res = homes.home_create("claude", "hub@user.example")
        self.assertTrue(res["existing"])
        self.assertIn("skills already -> " + hub, res["note"])
        self.assertEqual(os.readlink(link), hub)

    def test_prepare_names_a_foreign_skills_link_and_never_rewires_it(self):
        """An existing home whose skills/ points elsewhere is REPORTED, not
        rewired — prepare promises 'nothing was overwritten' on an existing
        home; `helm skills sync --apply` is the deliberate normalizer."""
        hub = self._hub()
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        home = self._plant_claude_home("kept-user-example", "kept@user.example", authed=False)
        os.symlink(elsewhere, os.path.join(home, "skills"))
        res = homes.home_create("claude", "kept@user.example")
        self.assertNotIn("error", res)
        self.assertEqual(os.readlink(os.path.join(home, "skills")), elsewhere)
        self.assertIn("NOT the skills hub", res["note"])
        self.assertIn("helm skills sync --apply", res["note"])
        self.assertNotIn(hub, os.readlink(os.path.join(home, "skills")))

    def test_prepare_says_the_hub_is_unavailable_and_never_silently_continues(self):
        """A hub the host NAMES but that cannot be used is not the quiet
        unconfigured case: the note says UNAVAILABLE with the path, and the
        deck-less control above proves the quiet case stays quiet."""
        gone = os.path.join(self.tmp, "no-such-hub")
        os.environ["HELM_SKILLS_CANONICAL"] = gone
        res = homes.home_create("claude", "gone@user.example")
        self.assertNotIn("error", res)
        self.assertFalse(os.path.lexists(os.path.join(res["home"], "skills")))
        self.assertIn("skills hub UNAVAILABLE", res["note"])
        self.assertIn(gone + " is configured but MISSING", res["note"])
        self.assertIn("NO skills", res["note"])

    def test_prepare_never_farms_the_deck_through_a_skills_link(self):
        """A deck entry symlinked THROUGH a skills link lands inside whatever
        the link names — a foreign target or, via an indirect link, the
        shared hub. Any symlink at skills/ means no deck; the link is kept,
        its target gains nothing, and the note names the link."""
        hub = self._hub()
        deck = os.path.join(self.tmp, "deck")
        os.makedirs(os.path.join(deck, "deck-only"))
        with open(os.path.join(deck, "deck-only", "SKILL.md"), "w") as f:
            f.write("# deck")
        os.environ["HELM_SKILL_DECK"] = deck
        foreign = os.path.join(self.tmp, "foreign-target")
        os.makedirs(foreign)
        home = self._plant_claude_home("far-user-example", "far@user.example", authed=False)
        os.symlink(foreign, os.path.join(home, "skills"))
        res = homes.home_create("claude", "far@user.example")
        self.assertIn("NOT the skills hub", res["note"])
        self.assertNotIn("skill deck", res["note"])
        self.assertEqual(os.listdir(foreign), [])
        self.assertEqual(os.readlink(os.path.join(home, "skills")), foreign)
        # an indirect link that RESOLVES to the hub: named as such, kept,
        # and the hub gains no deck entry through it
        via = os.path.join(self.tmp, "via")
        os.makedirs(via)
        os.symlink(hub, os.path.join(via, "skills"))
        home = self._plant_claude_home("near-user-example", "near@user.example", authed=False)
        os.symlink(os.path.join(via, "skills"), os.path.join(home, "skills"))
        res = homes.home_create("claude", "near@user.example")
        self.assertIn("resolves to the hub through another path", res["note"])
        self.assertNotIn("skill deck", res["note"])
        self.assertEqual(sorted(os.listdir(hub)), ["learn"])
        self.assertEqual(os.readlink(os.path.join(home, "skills")),
                         os.path.join(via, "skills"))

    def test_prepare_prefers_the_hub_and_never_writes_the_deck_into_it(self):
        """With BOTH a hub and a deck configured the hub wins: skills/ is the
        hub link and the deck is not farmed — a deck entry symlinked through
        that link would land INSIDE the shared hub, a write into every
        home on the host from one prepare."""
        hub = self._hub()
        deck = os.path.join(self.tmp, "deck")
        os.makedirs(os.path.join(deck, "deck-only"))
        with open(os.path.join(deck, "deck-only", "SKILL.md"), "w") as f:
            f.write("# deck")
        os.environ["HELM_SKILL_DECK"] = deck
        res = homes.home_create("claude", "both@user.example")
        self.assertNotIn("error", res)
        self.assertEqual(os.readlink(os.path.join(res["home"], "skills")), hub)
        self.assertEqual(sorted(os.listdir(hub)), ["learn"])
        self.assertNotIn("skill deck", res["note"])

    def test_prepare_codex_home_no_symlink(self):
        res = homes.home_create("codex", "cx@user.example")
        self.assertNotIn("error", res)
        self.assertIn("codex login --device-auth", res["login_cmd"])
        self.assertFalse(os.path.lexists(os.path.join(res["home"], "projects")))

    # -- verify: the name-vs-login mismatch audit --------------------------
    def test_verify_flags_name_identity_mismatch(self):
        self._plant_claude_home("wrong-name", "real@person.example")
        res = homes.home_verify("wrong-name")
        self.assertNotIn("error", res)
        self.assertEqual(res["verdict"], "issues")
        self.assertIs(res["checks"]["canonical"], False)
        self.assertTrue(any("real-person-example" in f for f in res["fixes"]))
        # the row-level audit flags it too
        row = next(r for r in homes.homes_list() if r["name"] == "wrong-name")
        self.assertIn("name-lies(want real-person-example)", homes._hygiene_flags(row))

    def test_verify_ok_when_canonical_and_linked(self):
        d = self._plant_claude_home("ok-person-example", "ok@person.example")
        os.makedirs(homes.SHARED_PROJECTS, exist_ok=True)
        os.symlink(homes.SHARED_PROJECTS, os.path.join(d, "projects"))
        self.assertEqual(homes.home_verify("ok-person-example")["verdict"], "ok")

    # -- archive: reversible move, refused while live ----------------------
    def test_archive_refuses_live_then_moves_and_restores(self):
        d = os.path.join(homes.ROOTS["codex"], "busy-user-example")
        os.makedirs(d)
        homes._agent_procs = lambda: [(4242, "codex", os.path.realpath(d))]
        res = homes.home_archive("busy-user-example")
        self.assertIn("REFUSED", res["error"])
        self.assertTrue(os.path.isdir(d))                      # untouched
        homes._agent_procs = lambda: []
        res = homes.home_archive("busy-user-example")
        self.assertTrue(res["ok"])
        self.assertFalse(os.path.lexists(d))                   # moved, not copied
        self.assertTrue(res["archived_to"].startswith(homes.ARCHIVE_ROOT))
        with open(os.path.join(res["archived_to"], homes.MARKER)) as f:
            meta = json.load(f)
        self.assertEqual(meta["name"], "busy-user-example")
        self.assertEqual(meta["from"], os.path.realpath(d))
        # reversible: restore lands it back where it came from, marker gone
        back = homes.home_unarchive("busy-user-example")
        self.assertTrue(back["ok"])
        self.assertEqual(back["restored_to"], os.path.realpath(d))
        self.assertTrue(os.path.isdir(d))
        self.assertFalse(os.path.exists(os.path.join(d, homes.MARKER)))

    # -- a predecessor's archives: read both, write new ---------------------
    def test_a_predecessors_archive_is_listed_and_restorable(self):
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
        self._plant_claude_home("copy-a", "a@x.example", token=tok)
        self._plant_claude_home("copy-b", "b@y.example", token=tok)
        self._plant_claude_home("solo-c", "c@z.example", token="a-different-family")
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
        self._plant_claude_home("cl-x", "x@x.example", token=tok)
        rows = {r["name"]: r for r in homes.homes_list()}
        self.assertEqual(rows["cx-a"]["shared_family"], ["cx-b"])
        self.assertNotIn("shared_family", rows["cl-x"])

    def test_token_bytes_never_surface_only_the_digest_prefix(self):
        tok = "super-secret-refresh-token-value"
        self._plant_claude_home("leak-a", "a@x.example", token=tok)
        self._plant_claude_home("leak-b", "b@y.example", token=tok)
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
        self._plant_claude_home("ok-a", "a@x.example", token="family-one")
        self._plant_claude_home("ok-b", "b@y.example", token="family-two")
        res = doctor.check_cred_families()
        self.assertEqual([lvl for lvl, _ in res], [doctor.OK])
        self.assertIn("2 distinct across 2 authed homes", res[0][1])
        # a byte-copy pair -> one FAIL per family group, not per home
        self._plant_claude_home("bomb-a", "c@x.example", token="copied-family")
        self._plant_claude_home("bomb-b", "d@y.example", token="copied-family")
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
        tok = self._jwt({"email": "cx@user.example", "sub": "x"})
        self._plant_codex_identity("nested-cx", tok, nested=True)
        self._plant_codex_identity("flat-cx", tok, nested=False)
        rows = {r["name"]: r for r in homes.homes_list()}
        self.assertEqual(rows["nested-cx"]["identity"], "cx@user.example")
        self.assertEqual(rows["flat-cx"]["identity"], "cx@user.example")

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
        self._plant_claude_home("dup-one", "same@user.example")
        self._plant_claude_home("dup-two", "same@user.example")
        rows = {r["name"]: r for r in homes.homes_list()}
        self.assertEqual(sorted(rows["dup-one"]["duplicate_identity"]), ["dup-two"])
        self.assertEqual(sorted(rows["dup-two"]["duplicate_identity"]), ["dup-one"])
        res = homes.home_verify("dup-one")
        self.assertEqual(res["verdict"], "issues")
        self.assertTrue(any("picks a survivor" in f and "dup-two" in f
                            for f in res["fixes"]))
        self.assertIn("dup:dup-two", homes._hygiene_flags(rows["dup-one"]))
        # and home_create refuses a THIRD seat for the same identity
        res = homes.home_create("claude", "same@user.example")
        self.assertIn("already seated", res["error"])

    def test_unauthed_homes_never_join_dup_scan(self):
        # no auth file => no identity claim counts: three empty homes with the
        # same PLANTED .claude.json email but no credentials must not flag
        for n in ("ua-1", "ua-2"):
            self._plant_claude_home(n, "same@user.example", authed=False)
        rows = homes.homes_list()
        for r in rows:
            self.assertNotIn("duplicate_identity", r)

    # -- prepare: an existing home holding a DIFFERENT identity refuses -----
    def test_prepare_refuses_home_holding_another_identity(self):
        self._plant_claude_home("taken-user-example", "taken@user.example", authed=True)
        res = homes.home_create("claude", "other@user.example")
        # the canonical name for other@user.example is free — the collision is on
        # a DIFFERENT email folding to an occupied dir
        self.assertNotIn("error", res)
        # now the real case: dir for the email exists and holds someone else
        d = os.path.join(homes.ROOTS["claude"], "clash-user-example")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "squatter@elsewhere.example"}}, f)
        res = homes.home_create("claude", "clash@user.example")
        self.assertIn("already holds squatter@elsewhere.example", res["error"])

    # -- _resolve: name, alias, path; ambiguity demands a provider ----------
    def test_resolve_by_alias_and_path(self):
        d = self._plant_claude_home("real-user-example", "real@user.example")
        os.symlink(d, os.path.join(homes.ROOTS["claude"], "shortcut"))
        row, err = homes._resolve("shortcut")
        self.assertIsNone(err)
        self.assertEqual(row["name"], "real-user-example")
        row2, err2 = homes._resolve(d)  # by absolute path
        self.assertIsNone(err2)
        self.assertEqual(row2["name"], "real-user-example")

    def test_resolve_same_name_two_providers_is_ambiguous(self):
        self._plant_claude_home("twin", "a@x.example")
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
        self._plant_claude_home("wrong-name", "real@person.example")
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


class HomeMcpProvisionTest(HomesBase):
    """EVERY HOME LOADS THE SAME MCP SET, whatever cred it carries.

    `home_create` already provisions the shared session store, the skills hub
    link and the skill deck under one stated rule — every home loads the same
    best setup. The MCP server set was never a member of that list, so five of
    the six distinct credhomes on this host came up with ZERO servers while the
    default home had six. Nothing errored; the tools were simply absent.
    """

    # helm's PROVIDER token, which is also a rostered seat name; this is the
    # provider key of ROOTS/DEFAULTS and names no seat
    PROVIDER = "claude"  # noqa: SEAT_NAME — provider key, not a seat

    SRC = {"bench-dev": {"command": "bd", "args": ["--serve"]},
           "cv": {"command": "cv-real"},
           "prism": {"command": "pa"}}

    def _seed_default(self, servers=None):
        d = homes.DEFAULTS[self.PROVIDER]
        os.makedirs(d, exist_ok=True)
        body = {"oauthAccount": {"emailAddress": "owner@user.example"}}
        if servers is not None:
            body["mcpServers"] = servers
        with open(os.path.join(d, ".claude.json"), "w") as fh:
            json.dump(body, fh)

    @staticmethod
    def _servers(home):
        with open(os.path.join(home, ".claude.json")) as fh:
            return json.load(fh).get("mcpServers") or {}

    def test_a_home_born_before_its_first_login_still_gets_the_whole_set(self):
        """The case the owner hit: a home has NO .claude.json until someone
        logs in there, so provisioning must CREATE the file, not skip it."""
        self._seed_default(self.SRC)
        res = homes.home_create(self.PROVIDER, "fresh@user.example")
        self.assertNotIn("error", res)
        self.assertEqual(self._servers(res["home"]), self.SRC)
        self.assertIn("mcp servers added", res["note"] or "")

    def test_the_set_comes_from_the_default_home_and_not_from_a_constant(self):
        """CONTROL for the arm above: a name no helm source contains proves the
        value was READ from the default home rather than baked in."""
        self._seed_default({"only-in-this-test-xyzzy": {"command": "q"}})
        res = homes.home_create(self.PROVIDER, "src@user.example")
        self.assertEqual(list(self._servers(res["home"])), ["only-in-this-test-xyzzy"])

    def test_an_entry_the_home_already_defines_is_never_overwritten(self):
        """A home may legitimately pin a different command for the same server.
        This function PROVISIONS; it does not reconcile."""
        self._seed_default(self.SRC)
        home = os.path.join(homes.ROOTS[self.PROVIDER], "pinned-user-example")
        os.makedirs(home)
        with open(os.path.join(home, ".claude.json"), "w") as fh:
            json.dump({"mcpServers": {"cv": {"command": "cv-PINNED"}},
                       "theme": "dark"}, fh)
        res = homes.home_create(self.PROVIDER, "pinned@user.example")
        got = self._servers(home)
        self.assertEqual(got["cv"], {"command": "cv-PINNED"},
                         "the home's own entry was clobbered")
        self.assertEqual(sorted(got), ["bench-dev", "cv", "prism"])
        with open(os.path.join(home, ".claude.json")) as fh:
            self.assertEqual(json.load(fh)["theme"], "dark",
                             "unrelated keys must survive the rewrite")
        self.assertIn("mcp servers added: bench-dev, prism",
                      (res["note"] or ""))

    def test_an_empty_source_writes_nothing_rather_than_stripping_the_home(self):
        """Propagating emptiness would silently DELETE a home's own servers.
        The negative control is the home that already has one."""
        self._seed_default({})
        home = os.path.join(homes.ROOTS[self.PROVIDER], "keeps-user-example")
        os.makedirs(home)
        with open(os.path.join(home, ".claude.json"), "w") as fh:
            json.dump({"mcpServers": {"mine": {"command": "m"}}}, fh)
        res = homes.home_create(self.PROVIDER, "keeps@user.example")
        self.assertEqual(self._servers(home), {"mine": {"command": "m"}})
        self.assertIn("declares none", (res["note"] or ""))

    def test_an_unreadable_home_config_is_left_byte_identical(self):
        """Fail closed: a file helm cannot parse is never rewritten from a
        guess at what it held."""
        self._seed_default(self.SRC)
        home = os.path.join(homes.ROOTS[self.PROVIDER], "broken-user-example")
        os.makedirs(home)
        path = os.path.join(home, ".claude.json")
        with open(path, "w") as fh:
            fh.write("{not json at all")
        res = homes.home_create(self.PROVIDER, "broken@user.example")
        with open(path) as fh:
            self.assertEqual(fh.read(), "{not json at all")
        self.assertIn("unreadable or not an object", (res["note"] or ""))

    def test_provisioning_twice_adds_nothing_the_second_time(self):
        self._seed_default(self.SRC)
        first = homes.home_create(self.PROVIDER, "twice@user.example")
        before = self._servers(first["home"])
        second = homes.home_create(self.PROVIDER, "twice@user.example")
        self.assertEqual(self._servers(first["home"]), before)
        self.assertIn("mcp servers already complete", (second["note"] or ""))
        self.assertNotIn("mcp servers added", (second["note"] or ""))


class HomeBenefitListTest(HomesBase):
    """WHAT A HOME CARRIES IS ONE LIST (homes.BENEFITS), walked by one
    provisioning pass (homes.provision, which home_create runs) and by one
    drift report (homes.benefit_drift, which `helm doctor` prints). Adding a
    benefit is adding one entry: it reaches every new home and is reported
    missing on every old one, with no other code change."""

    PROVIDER = "claude"  # noqa: SEAT_NAME — provider key, not a seat
    MARK = "benefit-marker-xyzzy"

    def _marker_benefit(self):
        def prov(home):
            with open(os.path.join(home, self.MARK), "w") as fh:
                fh.write("1")
            return ["marker written"], None

        def missing(home):
            return None if os.path.exists(os.path.join(home, self.MARK)) \
                else "no marker file"
        return homes.Benefit(self.MARK, "this test", prov, missing, False,
                             "`helm homes provision {name}`")

    def _row(self, rows, path):
        real = os.path.realpath(path)
        return next(r for r in rows if os.path.realpath(r["path"]) == real)

    def test_a_benefit_added_to_the_list_reaches_a_new_home_and_is_reported_on_an_old_one(self):
        """THE KEY ARM. The entry is added by patching the list alone; nothing
        in home_create, the drift report or doctor is told about it."""
        from helm import doctor
        old = self._plant_claude_home("old-user-example", "old@user.example")
        with mock.patch.object(homes, "BENEFITS",
                               homes.BENEFITS + (self._marker_benefit(),)):
            res = homes.home_create(self.PROVIDER, "new@user.example")
            self.assertNotIn("error", res)
            self.assertTrue(os.path.exists(os.path.join(res["home"], self.MARK)),
                            "the new benefit never reached the new home")
            self.assertIn("marker written", res["note"] or "")
            self.assertFalse(os.path.exists(os.path.join(old, self.MARK)))
            rows = homes.benefit_drift()
            old_row, new_row = self._row(rows, old), self._row(rows, res["home"])
            self.assertIn(self.MARK, [m[0] for m in old_row["missing"]],
                          "the old home was not reported missing the new benefit")
            self.assertNotIn(self.MARK, [m[0] for m in new_row["missing"]])
            said = [msg for _lvl, msg in doctor.check_home_benefits()]
        hit = [m for m in said if "old-user-example" in m and self.MARK in m]
        self.assertEqual(len(hit), 1, said)
        self.assertIn("helm homes provision old-user-example", hit[0])
        self.assertFalse(any("new-user-example" in m and self.MARK in m for m in said))

    def test_the_list_names_every_benefit_home_create_provided_before_it(self):
        """The four benefits home_create carried by accident are now members,
        with the two the hook installer writes."""
        self.assertEqual([b.name for b in homes.BENEFITS],
                         ["shared session store", "skills hub", "skill deck",
                          "mcp servers", "opus xhigh + ultracode",
                          "hook contract", "auto-memory base"])
        for b in homes.BENEFITS:
            self.assertTrue(b.source and b.remedy and callable(b.missing), b)

    def test_a_new_home_is_told_the_hook_contract_is_not_yet_written(self):
        """The two benefits this pass does not write are checked instead, so
        the new home's note names the gap and its command, not silence."""
        res = homes.home_create(self.PROVIDER, "hooks@user.example")
        self.assertIn("hook contract not written here", res["note"] or "")
        self.assertIn("`helm hooks install` writes it", res["note"] or "")

    def test_the_mcp_source_is_the_default_homes_sibling_state_file(self):
        """The default home run without CLAUDE_CONFIG_DIR keeps its state in
        the SIBLING ~/.claude.json. Measured on the live host: the inside
        file held zero servers while the sibling held six, so reading only the
        inside file provisioned nothing."""
        base = os.path.join(self.tmp, "userhome")
        dflt = os.path.join(base, ".claude")
        os.makedirs(dflt)
        with open(os.path.join(dflt, ".claude.json"), "w") as fh:
            json.dump({"projects": {}}, fh)
        with open(os.path.join(base, ".claude.json"), "w") as fh:
            json.dump({"projects": {"/a": {}}, "mcpServers": {
                "sib-only": {"command": "s"}}}, fh)
        homes.DEFAULTS = dict(homes.DEFAULTS, claude=dflt)
        homes.SHARED_PROJECTS = os.path.join(dflt, "projects")
        res = homes.home_create(self.PROVIDER, "sib@user.example")
        with open(os.path.join(res["home"], ".claude.json")) as fh:
            self.assertEqual(json.load(fh).get("mcpServers"),
                             {"sib-only": {"command": "s"}})
        old = self._plant_claude_home("bare-user-example", "bare@user.example")
        row = self._row(homes.benefit_drift(), old)
        self.assertIn(("mcp servers", "servers absent: sib-only",
                       "`helm homes provision bare-user-example`"), row["missing"])

    def test_a_rewritten_home_config_leaves_its_backup_beside_it(self):
        d = homes.DEFAULTS[self.PROVIDER]
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as fh:
            json.dump({"mcpServers": {"cv": {"command": "cv"}}}, fh)
        home = os.path.join(homes.ROOTS[self.PROVIDER], "bak-user-example")
        os.makedirs(home)
        path = os.path.join(home, ".claude.json")
        original = '{"theme": "dark"}'
        with open(path, "w") as fh:
            fh.write(original)
        homes.home_create(self.PROVIDER, "bak@user.example")
        baks = [n for n in os.listdir(home) if n.startswith(".claude.json.bak-mcp-")]
        self.assertEqual(len(baks), 1, os.listdir(home))
        with open(os.path.join(home, baks[0])) as fh:
            self.assertEqual(fh.read(), original)
        # a home born with no file has nothing to back up
        res = homes.home_create(self.PROVIDER, "fresh@user.example")
        self.assertEqual([n for n in os.listdir(res["home"]) if ".bak-" in n], [])

    def test_a_check_that_cannot_tell_is_unknown_never_missing_or_present(self):
        from helm import doctor
        d = homes.DEFAULTS[self.PROVIDER]
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as fh:
            json.dump({"mcpServers": {"cv": {"command": "cv"}}}, fh)
        home = os.path.join(homes.ROOTS[self.PROVIDER], "torn-user-example")
        os.makedirs(home)
        with open(os.path.join(home, ".claude.json"), "w") as fh:
            fh.write("{torn")
        row = self._row(homes.benefit_drift(), home)
        self.assertNotIn("mcp servers", [m[0] for m in row["missing"]])
        self.assertIn("mcp servers", [u[0] for u in row["unknown"]])
        said = [msg for _l, msg in doctor.check_home_benefits()]
        self.assertTrue(any("cannot tell" in m and "torn-user-example" in m
                            and "mcp servers" in m for m in said), said)

    def test_the_default_home_is_not_audited_for_what_it_is_the_source_of(self):
        """~/.claude/projects IS the store and its servers ARE the set; the
        default is audited only for what it must carry too."""
        d = homes.DEFAULTS[self.PROVIDER]
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as fh:
            json.dump({"mcpServers": {"cv": {"command": "cv"}}}, fh)
        row = self._row(homes.benefit_drift(), d)
        names = [m[0] for m in row["missing"]]
        self.assertNotIn("shared session store", names)
        self.assertNotIn("mcp servers", names)
        self.assertIn("hook contract", names)       # it must carry that one

    def test_provision_closes_the_gap_on_an_existing_home_additively(self):  # noqa: VACUOUS_ASSERTION — the absence after provision is paired with an assertIn of the same benefit in the same drift row before it
        """The door the drift report names: an existing authed home gains the
        servers it lacks, keeps its own entry, and the default is refused."""
        d = homes.DEFAULTS[self.PROVIDER]
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as fh:
            json.dump({"mcpServers": {"cv": {"command": "cv"},
                                      "pa": {"command": "pa"}}}, fh)
        home = self._plant_claude_home("have-user-example", "have@user.example")
        path = os.path.join(home, ".claude.json")
        with open(path) as fh:
            body = json.load(fh)
        body["mcpServers"] = {"cv": {"command": "cv-PINNED"}}
        with open(path, "w") as fh:
            json.dump(body, fh)
        before = self._row(homes.benefit_drift(), home)
        self.assertIn(("mcp servers", "servers absent: pa",
                       "`helm homes provision have-user-example`"), before["missing"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = homes.cmd_homes(["provision", "have-user-example"])
        self.assertEqual(rc, 0, out.getvalue())
        with open(path) as fh:
            got = json.load(fh)
        self.assertEqual(got["mcpServers"], {"cv": {"command": "cv-PINNED"},
                                             "pa": {"command": "pa"}})
        self.assertEqual(got["oauthAccount"], {"emailAddress": "have@user.example"})
        self.assertIn("mcp servers added: pa", out.getvalue())
        row = self._row(homes.benefit_drift(), home)
        self.assertNotIn("mcp servers", [m[0] for m in row["missing"]])
        self.assertIn("default home is the SOURCE",
                      homes.home_provision("(default-claude)").get("error", ""))

    def test_doctor_says_ok_only_when_no_home_lacks_anything(self):
        from helm import doctor
        clean = [{"label": "a", "path": "/x/a", "missing": [], "unknown": []}]
        rows = doctor.check_home_benefits(rows=clean)
        self.assertEqual([lvl for lvl, _m in rows], [doctor.OK])
        self.assertIn("1 claude home(s) carry every listed benefit", rows[0][1])
        self.assertEqual(doctor.check_home_benefits(rows=[]), [])
        self.assertIn("check_home_benefits", doctor.CHECKS)


class OpusDefaultsBenefitTest(HomesBase):
    """OWNER RULING (premise opus-agents-xhigh-ultracode-subagents-for-same-
    model): every Opus agent runs at xhigh effort with ultracode on.
    A claude credential home carries that as two settings.json keys, and it
    carries them because they are ONE ENTRY in homes.BENEFITS: the pass that
    makes a home writes them, the drift report names a home without them.
    Proxy seats for other families get neither key (their credentials are
    limited). The pass adds a key the home lacks and never changes one the
    home already sets.

    The expected values are LITERALS here and never read back from the
    module's own table, so a wrong value in the table fails these arms."""

    PROVIDER = "claude"  # noqa: SEAT_NAME — provider key, not a seat
    NAME = "opus xhigh + ultracode"

    @staticmethod
    def _settings(home):
        with open(os.path.join(home, "settings.json")) as fh:
            return json.load(fh)

    @staticmethod
    def _write_settings(home, body):
        with open(os.path.join(home, "settings.json"), "w") as fh:
            json.dump(body, fh)

    def _row(self, rows, path):
        real = os.path.realpath(path)
        return next(r for r in rows if os.path.realpath(r["path"]) == real)

    def test_a_new_claude_home_is_born_with_ultracode_and_opus_xhigh(self):  # noqa: VACUOUS_ASSERTION — the absent settings file is the PRE-state, and the same file's keys are asserted present right after, unconditionally
        """ARM (a): `helm homes prepare` (home_create) and `helm homes
        provision` (home_provision) both give a Claude home the two keys."""
        res = homes.home_create(self.PROVIDER, "fresh@user.example")
        self.assertNotIn("error", res)
        got = self._settings(res["home"])
        self.assertIs(got.get("ultracode"), True, got)
        self.assertEqual(got["modelSettings"]["claude-opus-5-5"]["effortLevel"],
                         "xhigh")
        self.assertIn("settings defaults set", res["note"] or "")
        bare = self._plant_claude_home("bare-user-example", "bare@user.example")
        self.assertFalse(os.path.exists(os.path.join(bare, "settings.json")))
        out = homes.home_provision("bare-user-example")
        self.assertNotIn("error", out)
        got = self._settings(bare)
        self.assertIs(got.get("ultracode"), True, got)
        self.assertEqual(got["modelSettings"]["claude-opus-5-5"]["effortLevel"],
                         "xhigh")

    def test_a_proxy_family_seat_home_gets_neither_key(self):  # noqa: VACUOUS_ASSERTION — every absence is paired with an unconditional positive control in the same call: the native home gets ultracode and is reported missing
        """ARM (b): a proxy family's seat config dir, seeded by the proxy
        seat's own settings writer, is walked by the list and comes out with
        neither key, and the drift report does not ask for them there. Both
        seat shapes the write gate knows: <family>/claude and
        <family>/instances/<seat>/claude. POSITIVE CONTROL in the same calls:
        a native home beside them gets the keys and is reported, so a list
        that did nothing would fail this arm."""
        from helm import seat, seat_catalog
        fam = next(f for f, v in seat_catalog.FAMILIES.items()
                   if v["mode"].startswith("proxy"))
        # the harness dir a seat mint writes under its seat dir
        harness = self.PROVIDER
        cdirs = [os.path.join(seat.seat_dir(fam), harness),
                 os.path.join(seat._instance_dir(fam, "seat-a"), harness)]
        native = self._plant_claude_home("native-user-example", "native@user.example")
        for cdir in cdirs:
            os.makedirs(cdir)
            seat._seed_seat_settings(cdir, fam)
            before = self._settings(cdir)
            homes.provision(cdir, at_launch=True)
            homes.provision(native, at_launch=True)
            after = self._settings(cdir)
            self.assertEqual(after, before)
            self.assertNotIn("ultracode", after)
            self.assertNotIn("modelSettings", after)
            self.assertIs(self._settings(native).get("ultracode"), True)
        os.remove(os.path.join(native, "settings.json"))
        rows = homes.benefit_drift(
            dirs=[("seat-a", c) for c in cdirs] + [("native", native)])
        for cdir in cdirs:
            row = self._row(rows, cdir)
            self.assertNotIn(self.NAME, [m[0] for m in row["missing"]])
            self.assertNotIn(self.NAME, [u[0] for u in row["unknown"]])
        self.assertIn(self.NAME,
                      [m[0] for m in self._row(rows, native)["missing"]])

    def test_an_existing_settings_file_keeps_every_other_key_and_its_own_values(self):
        """ARM (c): the pass ADDS what is absent. Every other key survives
        exactly, a different Opus effortLevel the home already sets is kept,
        and an explicit ultracode false is kept. The rewritten file leaves its
        backup beside it, named the way the hand cure named its backups."""
        home = self._plant_claude_home("keep-user-example", "keep@user.example")
        mine = {"theme": "dark", "effortLevel": "low",
                "hooks": {"Stop": [{"hooks": [{"type": "command",
                                               "command": "true"}]}]},
                "permissions": {"allow": ["Bash(ls:*)"]},
                "modelSettings": {"claude-opus-5-5": {"effortLevel": "high",
                                                      "other": 1},
                                  "claude-fable-5-1": {"effortLevel": "high"}}}
        self._write_settings(home, mine)
        with open(os.path.join(home, "settings.json")) as fh:
            original = fh.read()
        res = homes.home_provision("keep-user-example")
        self.assertNotIn("error", res)
        self.assertEqual(self._settings(home), dict(mine, ultracode=True))
        baks = [n for n in os.listdir(home)
                if n.startswith("settings.json.bak-ultracode-")]
        self.assertEqual(len(baks), 1, os.listdir(home))
        with open(os.path.join(home, baks[0])) as fh:
            self.assertEqual(fh.read(), original)
        off = self._plant_claude_home("off-user-example", "off@user.example")
        self._write_settings(off, {"ultracode": False, "model": "opus"})
        homes.home_provision("off-user-example")
        self.assertEqual(self._settings(off), {
            "ultracode": False, "model": "opus",
            "modelSettings": {"claude-opus-5-5": {"effortLevel": "xhigh"}}})
        # a value where an object belongs is never replaced to make room
        odd = self._plant_claude_home("odd-user-example", "odd@user.example")
        self._write_settings(odd, {"modelSettings": "mine"})
        homes.home_provision("odd-user-example")
        self.assertEqual(self._settings(odd),
                         {"modelSettings": "mine", "ultracode": True})
        # a symlinked file is left a symlink, its target unchanged: a replace
        # would cut the link
        linked = self._plant_claude_home("link-user-example", "link@user.example")
        target = os.path.join(self.tmp, "shared-settings.json")
        with open(target, "w") as fh:
            json.dump({"theme": "light"}, fh)
        os.symlink(target, os.path.join(linked, "settings.json"))
        res = homes.home_provision("link-user-example")
        self.assertTrue(os.path.islink(os.path.join(linked, "settings.json")))
        with open(target) as fh:
            self.assertEqual(json.load(fh), {"theme": "light"})
        self.assertTrue(any("is a symlink" in n for n in res["notes"]), res)
        # a file that will not parse is left byte-identical, and the drift
        # report says it cannot tell rather than calling the keys missing
        torn = self._plant_claude_home("torn-user-example", "torn@user.example")
        with open(os.path.join(torn, "settings.json"), "w") as fh:
            fh.write("{torn")
        res = homes.home_provision("torn-user-example")
        with open(os.path.join(torn, "settings.json")) as fh:
            self.assertEqual(fh.read(), "{torn")
        self.assertTrue(any("left untouched" in n for n in res["notes"]), res)
        row = self._row(homes.benefit_drift(), torn)
        self.assertIn(self.NAME, [u[0] for u in row["unknown"]])
        self.assertNotIn(self.NAME, [m[0] for m in row["missing"]])

    def test_doctor_names_a_claude_home_that_lacks_the_keys(self):
        """ARM (d): the doctor rung over the list names each home without the
        keys, says which key is absent and the command that closes it, and
        does not name a home that sets them (even to another value)."""
        from helm import doctor
        bare = self._plant_claude_home("old-user-example", "old@user.example")
        self._write_settings(bare, {"theme": "dark"})
        half = self._plant_claude_home("half-user-example", "half@user.example")
        self._write_settings(half, {"ultracode": True})
        done = self._plant_claude_home("done-user-example", "done@user.example")
        self._write_settings(done, {"ultracode": True, "modelSettings": {
            "claude-opus-5-5": {"effortLevel": "high"}}})
        rows = homes.benefit_drift()
        mine = lambda r: [m for m in r["missing"] if m[0] == self.NAME]
        self.assertEqual(mine(self._row(rows, bare)), [(
            self.NAME,
            "settings.json lacks ultracode, "
            "modelSettings.claude-opus-5-5.effortLevel",
            "`helm homes provision old-user-example` (the default home: a "
            "`helm launch` with neither --home nor --no-install)")])
        self.assertEqual([m[1] for m in mine(self._row(rows, half))],
                         ["settings.json lacks "
                          "modelSettings.claude-opus-5-5.effortLevel"])
        self.assertEqual(mine(self._row(rows, done)), [])
        odd = self._plant_claude_home("odd-user-example", "odd@user.example")
        self._write_settings(odd, {"ultracode": True, "modelSettings": "mine"})
        self.assertEqual(
            [m[1] for m in mine(self._row(homes.benefit_drift(), odd))],
            ["settings.json lacks modelSettings.claude-opus-5-5.effortLevel "
             "(its parent is not an object)"])
        said = [msg for _lvl, msg in doctor.check_home_benefits()]
        hit = [m for m in said if "old-user-example" in m and self.NAME in m]
        self.assertEqual(len(hit), 1, said)
        self.assertIn("helm homes provision old-user-example", hit[0])
        self.assertFalse(any("done-user-example" in m and self.NAME in m
                             for m in said), said)
