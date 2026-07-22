"""Hermetic tests for helm.cred — a FAKE ~/.claude-homes estate in a tmpdir,
a tmp backup root, and a stubbed live-holder probe. The real credential homes
and the real ~/.cred-backups are never read or written, and every credential
byte in here is an obvious fake string ("FAKE-…"): no test fixture, output or
log line ever carries a real token."""
import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from helm import cred, doctor, homes

FAKE = "FAKE-REFRESH-TOKEN-not-a-secret"


class CredBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-cred-")
        j = lambda *p: os.path.join(self.tmp, *p)
        self._orig = {k: getattr(homes, k) for k in
                      ("ROOTS", "DEFAULTS", "SHARED_PROJECTS", "ARCHIVE_ROOT",
                       "LEGACY_ARCHIVE_ROOT", "_agent_procs")}
        homes.ROOTS = {"claude": j("claude-homes"), "codex": j("codex-homes")}
        homes.DEFAULTS = {"claude": j("default-claude"), "codex": j("default-codex")}
        homes.SHARED_PROJECTS = j("default-claude", "projects")
        homes.ARCHIVE_ROOT = j("helm-home-archive")
        homes.LEGACY_ARCHIVE_ROOT = j("sesh-home-archive")
        homes._agent_procs = lambda: []
        for r in homes.ROOTS.values():
            os.makedirs(r)
        self.backups = j("cred-backups")
        self.envp = mock.patch.dict(os.environ, {"HELM_CRED_BACKUP_ROOT": self.backups})
        self.envp.start()
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        cred.cache_clear()
        # nothing on this box holds a test home — heal's refusal is exercised
        # explicitly, never by an accident of the host's process table
        self._holders = mock.patch.object(cred, "holders_of", lambda p, default=False: [])
        self._holders.start()

    def tearDown(self):
        self._holders.stop()
        self.envp.stop()
        cred.cache_clear()
        for k, v in self._orig.items():
            setattr(homes, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------
    def plant(self, dirname, email, token=FAKE, uuid="uuid-1", org="Org"):
        """A fake home: identity METADATA (.claude.json oauthAccount) + a
        fake-token credentials file. `dirname` is deliberately free to LIE
        about `email` — that is the whole subject under test."""
        d = os.path.join(homes.ROOTS["claude"], dirname)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"numStartups": 3, "oauthAccount": {
                "emailAddress": email, "accountUuid": uuid,
                "organizationName": org}}, f)
        if token is not None:
            with open(os.path.join(d, ".credentials.json"), "w") as f:
                json.dump({"claudeAiOauth": {"refreshToken": token,
                                             "accessToken": token + "-A"}}, f)
        cred.cache_clear()
        return d

    def out(self, fn, *a, **kw):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = fn(*a, **kw)
        return rc, buf.getvalue(), err.getvalue()


class IdentityTest(CredBase):
    def test_identity_comes_from_content_not_the_dir_name(self):
        """THE law: the dir name is a label; .claude.json is the truth."""
        d = self.plant("cto-example-com", "owner@example.invalid")
        acct = cred.account_of(d)
        self.assertTrue(acct["ok"])
        self.assertEqual(acct["email"], "owner@example.invalid")
        self.assertEqual(acct["uuid"], "uuid-1")
        self.assertEqual(acct["org"], "Org")

    def test_drift_and_agreement_verdicts(self):
        self.plant("david-example-invalid", "owner@example.invalid")     # name == folded email
        self.plant("cto-example-com", "owner@example.invalid")       # name lies
        by = {r["name"]: r for r in cred.rows()}
        self.assertEqual(by["david-example-invalid"]["verdict"], "AGREE")
        self.assertEqual(by["cto-example-com"]["verdict"], "DRIFT")
        self.assertEqual(by["cto-example-com"]["account"], "owner@example.invalid")
        self.assertEqual(by["cto-example-com"]["wants_home"], "david-example-invalid")

    def test_unreadable_identity_is_fail_closed_never_guessed(self):
        d = os.path.join(homes.ROOTS["claude"], "david-example-invalid")
        os.makedirs(d)
        with open(os.path.join(d, ".credentials.json"), "w") as f:
            f.write("{}")
        row = {r["name"]: r for r in cred.rows()}["david-example-invalid"]
        self.assertEqual(row["verdict"], "UNKNOWN")
        self.assertIsNone(row["account"])          # never "owner@example.invalid" from the name
        self.assertIn(".claude.json", row["error"])

    def test_malformed_json_and_missing_block_degrade_without_raising(self):
        d = self.plant("a-b-com", "a@b.com")
        with open(os.path.join(d, ".claude.json"), "w") as f:
            f.write("{not json")
        cred.cache_clear()
        self.assertFalse(cred.account_of(d)["ok"])
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": 42}}, f)
        cred.cache_clear()
        self.assertFalse(cred.account_of(d)["ok"])

    def test_cache_follows_the_file_not_the_first_read(self):
        d = self.plant("a-b-com", "a@b.com")
        self.assertEqual(cred.account_of(d)["email"], "a@b.com")
        self.plant("a-b-com", "other@b.com")       # a /login lands here
        self.assertEqual(cred.account_of(d)["email"], "other@b.com")

    def test_homes_row_identity_uses_the_same_reader(self):
        self.plant("cto-example-com", "owner@example.invalid")
        row = {r["name"]: r for r in homes.homes_list()}["cto-example-com"]
        self.assertEqual(row["identity"], "owner@example.invalid")
        self.assertFalse(row["canonical"])         # the pre-existing drift bit


class BackupTest(CredBase):
    def test_roundtrip_reproduces_bytes_and_identity(self):
        d = self.plant("david-example-invalid", "owner@example.invalid")
        before = open(os.path.join(d, ".credentials.json"), "rb").read()
        res = cred.backup(d)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "backup")
        self.assertEqual(res["account"], "owner@example.invalid")
        # the account dir is the FOLDED EMAIL, never the source dir name
        self.assertEqual(os.path.dirname(res["dest"]),
                         os.path.join(self.backups, "david-example-invalid"))
        # clobber the home the way a /login would, then restore
        self.plant("david-example-invalid", "someone@else.com", token="FAKE-OTHER")
        self.assertEqual(cred.account_of(d)["email"], "someone@else.com")
        r = cred.restore(res["dest"], d)
        self.assertTrue(r["ok"])
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(), before)
        self.assertEqual(cred.account_of(d)["email"], "owner@example.invalid")

    def test_restore_preserves_the_rest_of_claude_json(self):
        d = self.plant("david-example-invalid", "owner@example.invalid")
        snap = cred.backup(d)["dest"]
        doc = json.load(open(os.path.join(d, ".claude.json")))
        doc["oauthAccount"] = {"emailAddress": "someone@else.com"}
        doc["tipsHistory"] = {"keep": 1}
        json.dump(doc, open(os.path.join(d, ".claude.json"), "w"))
        cred.cache_clear()
        cred.restore(snap, d)
        got = json.load(open(os.path.join(d, ".claude.json")))
        self.assertEqual(got["oauthAccount"]["emailAddress"], "owner@example.invalid")
        self.assertEqual(got["tipsHistory"], {"keep": 1})   # untouched keys survive
        self.assertEqual(got["numStartups"], 3)

    def test_permissions_are_0600_files_inside_0700_dirs(self):
        d = self.plant("david-example-invalid", "owner@example.invalid")
        dest = cred.backup(d)["dest"]
        for name in ("credentials.json", "account.json", "meta.json"):
            mode = stat.S_IMODE(os.stat(os.path.join(dest, name)).st_mode)
            self.assertEqual(mode, 0o600, name)
        for p in (self.backups, os.path.dirname(dest), dest):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o700, p)

    def test_identical_snapshot_is_skipped(self):
        d = self.plant("david-example-invalid", "owner@example.invalid")
        first = cred.backup(d)
        again = cred.backup(d)
        self.assertEqual(again["action"], "skip")
        self.assertEqual(again["dest"], first["dest"])
        self.assertEqual(len(cred.snapshots("owner@example.invalid")), 1)

    def test_changed_credentials_make_a_new_snapshot(self):
        d = self.plant("david-example-invalid", "owner@example.invalid")
        cred.backup(d)
        self.plant("david-example-invalid", "owner@example.invalid", token="FAKE-ROTATED")
        self.assertEqual(cred.backup(d)["action"], "backup")
        self.assertEqual(len(cred.snapshots("owner@example.invalid")), 2)

    def test_backup_fails_closed_on_unknown_identity(self):
        d = os.path.join(homes.ROOTS["claude"], "mystery")
        os.makedirs(d)
        with open(os.path.join(d, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"refreshToken": FAKE}}, f)
        res = cred.backup(d)
        self.assertFalse(res["ok"])
        self.assertEqual(res["action"], "skip")
        self.assertFalse(os.path.isdir(os.path.join(self.backups, "mystery")))

    def test_prune_keeps_the_newest_and_never_the_only_one(self):
        d = self.plant("david-example-invalid", "owner@example.invalid")
        for i in range(cred.KEEP + 3):
            self.plant("david-example-invalid", "owner@example.invalid", token="FAKE-%d" % i)
            cred.backup(d)
        snaps = cred.snapshots("owner@example.invalid")
        self.assertEqual(len(snaps), cred.KEEP)
        newest = json.load(open(os.path.join(snaps[-1]["path"], "credentials.json")))
        self.assertEqual(newest["claudeAiOauth"]["refreshToken"],
                         "FAKE-%d" % (cred.KEEP + 2))

    def test_unwritable_root_is_reported_never_raised(self):
        """keepalive's rotation calls backup — a full/blocked disk must not
        raise across it; an unrotated token family is the worse outcome."""
        d = self.plant("david-example-invalid", "owner@example.invalid")
        blocked = os.path.join(self.tmp, "not-a-dir")
        with open(blocked, "w") as f:
            f.write("")
        with mock.patch.dict(os.environ, {"HELM_CRED_BACKUP_ROOT": blocked}):
            res = cred.backup(d)
        self.assertFalse(res["ok"])
        self.assertIn("snapshot write failed", res["reason"])

    def test_backup_all_covers_every_authed_home(self):
        self.plant("david-example-invalid", "owner@example.invalid")
        self.plant("team-example-com", "hey@simbi.com")
        self.plant("no-creds-com", "no@creds.com", token=None)
        done = {r["account"] for r in cred.backup_all() if r["action"] == "backup"}
        self.assertEqual(done, {"owner@example.invalid", "hey@simbi.com"})


class HealTest(CredBase):
    def drifted(self):
        """cto-example-com's NAME promises cto@example.invalid; a /login left owner@example.invalid."""
        cto = self.plant("cto-example-com", "cto@example.invalid", token="FAKE-CTO")
        cred.backup(cto)                                   # the guard ran first
        self.plant("cto-example-com", "owner@example.invalid", token="FAKE-DAVID")
        return cto

    def test_dry_run_is_the_default_and_touches_nothing(self):
        d = self.drifted()
        before = open(os.path.join(d, ".credentials.json"), "rb").read()
        res = cred.heal()
        self.assertFalse(res["apply"])
        plan = res["plans"][0]
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["holds"], "owner@example.invalid")
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(), before)
        self.assertEqual(cred.account_of(d)["email"], "owner@example.invalid")

    def test_apply_restores_the_named_account_when_free(self):
        d = self.drifted()
        res = cred.heal(apply=True)
        plan = res["plans"][0]
        self.assertEqual(plan["status"], "restored")
        self.assertEqual(cred.account_of(d)["email"], "cto@example.invalid")
        creds = json.load(open(os.path.join(d, ".credentials.json")))
        self.assertEqual(creds["claudeAiOauth"]["refreshToken"], "FAKE-CTO")
        # the EVICTED occupant was snapshotted first — the undo is undoable
        self.assertTrue(plan["pre_image"])
        self.assertEqual(len(cred.snapshots("owner@example.invalid")), 1)

    def test_apply_refuses_while_a_live_session_holds_the_home(self):
        d = self.drifted()
        before = open(os.path.join(d, ".credentials.json"), "rb").read()
        with mock.patch.object(cred, "holders_of",
                               lambda p, default=False: [(4242, "claude")]):
            res = cred.heal(apply=True)
        plan = res["plans"][0]
        self.assertEqual(plan["status"], "held")
        self.assertIn("pid 4242", plan["reason"])
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(), before)

    def test_apply_refuses_when_a_holder_arrives_mid_heal(self):
        """The plan/act window: heal re-probes before it writes."""
        d = self.drifted()
        before = open(os.path.join(d, ".credentials.json"), "rb").read()
        seq = [[], [(77, "claude")]]           # free at plan time, held at act time
        with mock.patch.object(cred, "holders_of",
                               lambda p, default=False: seq.pop(0) if seq else []):
            res = cred.heal(apply=True)
        self.assertEqual(res["plans"][0]["status"], "held")
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(), before)

    def test_no_proc_probe_refuses_rather_than_assuming_free(self):
        d = self.drifted()
        with mock.patch.object(cred, "holders_of", lambda p, default=False: None):
            res = cred.heal(apply=True)
        self.assertEqual(res["plans"][0]["status"], "cannot-probe")
        self.assertEqual(cred.account_of(d)["email"], "owner@example.invalid")

    def test_no_backup_says_so_instead_of_inventing_a_restore(self):
        self.plant("cto-example-com", "owner@example.invalid")
        plan = cred.heal()["plans"][0]
        self.assertEqual(plan["status"], "no-backup")
        self.assertIsNone(plan["restore_from"])

    def test_refuses_to_mint_a_shared_token_family(self):
        """helm's oldest credential law: one home = one token family. If the
        snapshot's refresh token is STILL live in another home, restoring it
        here would byte-copy the family — reuse detection revokes both."""
        cto = self.plant("cto-example-com", "cto@example.invalid", token="FAKE-SHARED")
        cred.backup(cto)
        self.plant("cto-example-com", "owner@example.invalid", token="FAKE-DAVID")
        self.plant("elsewhere-com", "cto@example.invalid", token="FAKE-SHARED")  # same bytes
        plan = cred.heal(apply=True)["plans"][0]
        self.assertEqual(plan["status"], "revocation-risk")
        self.assertIn("elsewhere-com", plan["reason"])
        self.assertIn("claude /login", plan["reason"])
        self.assertEqual(cred.account_of(cto)["email"], "owner@example.invalid")   # untouched

    def test_agreeing_homes_are_never_planned(self):
        self.plant("david-example-invalid", "owner@example.invalid")
        self.assertEqual(cred.heal()["plans"], [])

    def test_holders_probe_reads_proc_env_not_the_name(self):
        """The real probe, exercised against this very process."""
        self._holders.stop()
        try:
            d = self.plant("david-example-invalid", "owner@example.invalid")
            self.assertEqual(cred.holders_of(d), [])       # nothing pinned there
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
                # our OWN pid is excluded by contract; a child would be seen
                self.assertEqual(cred.holders_of(d), [])
        finally:
            self._holders.start()


class DoctorTest(CredBase):
    def test_drift_row_names_the_account_and_the_verb(self):
        self.plant("cto-example-com", "owner@example.invalid")
        msgs = [m for lvl, m in cred.doctor_rows() if lvl == "WARN"]
        self.assertTrue(any("credhome cto-example-com HOLDS owner@example.invalid (drift" in m
                            for m in msgs), msgs)
        self.assertTrue(any("helm cred heal" in m for m in msgs))

    def test_missing_backup_row(self):
        self.plant("david-example-invalid", "owner@example.invalid")
        msgs = [m for lvl, m in cred.doctor_rows() if lvl == "WARN"]
        self.assertTrue(any("no cred backup for owner@example.invalid" in m for m in msgs), msgs)

    def test_clean_estate_is_one_ok_row(self):
        d = self.plant("david-example-invalid", "owner@example.invalid")
        cred.backup(d)
        rows = cred.doctor_rows()
        self.assertEqual([lvl for lvl, _ in rows], ["OK"])
        self.assertIn("every dir name matches", rows[0][1])

    def test_doctor_check_is_wired_into_the_report(self):
        self.assertIn("check_cred_drift", doctor.CHECKS)
        self.plant("cto-example-com", "owner@example.invalid")
        rows = doctor.check_cred_drift()
        self.assertTrue(any("drift" in m for _, m in rows))


class CliTest(CredBase):
    def test_list_shows_dir_name_actual_account_and_verdict(self):
        self.plant("cto-example-com", "owner@example.invalid")
        self.plant("team-example-com", "hey@simbi.com")
        rc, out, _ = self.out(cred.cmd_cred, ["list"])
        self.assertEqual(rc, 0)
        self.assertIn("DIR NAME", out)
        self.assertIn("ACTUAL ACCOUNT", out)
        self.assertIn("cto-example-com", out)
        self.assertIn("owner@example.invalid", out)
        self.assertIn("DRIFT", out)
        self.assertIn("AGREE", out)
        self.assertIn("1 drift", out)

    def test_bare_cred_is_list(self):
        self.plant("david-example-invalid", "owner@example.invalid")
        rc, out, _ = self.out(cred.cmd_cred, [])
        self.assertEqual(rc, 0)
        self.assertIn("ACTUAL ACCOUNT", out)

    def test_list_json(self):
        self.plant("cto-example-com", "owner@example.invalid")
        rc, out, _ = self.out(cred.cmd_cred, ["list", "--json"])
        rows = json.loads(out)
        self.assertEqual(rows[0]["verdict"], "DRIFT")

    def test_backup_all_and_switch_guard_report_the_account(self):
        self.plant("david-example-invalid", "owner@example.invalid")
        rc, out, _ = self.out(cred.cmd_cred, ["backup", "--all"])
        self.assertEqual(rc, 0)
        self.assertIn("owner@example.invalid", out)
        rc, out, _ = self.out(cred.cmd_cred, ["switch-guard", "--home", "david-example-invalid"])
        self.assertEqual(rc, 0)
        self.assertIn("protected", out)
        self.assertIn("claude /login", out)      # the exact command, human-run

    def test_switch_guard_quiet_backup_prints_nothing(self):
        """SessionStart hook mode: stdout becomes session context — stay silent."""
        d = self.plant("david-example-invalid", "owner@example.invalid")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            rc, out, err = self.out(cred.cmd_cred, ["backup", "--quiet"])
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(len(cred.snapshots("owner@example.invalid")), 1)

    def test_switch_guard_reports_when_it_cannot_protect(self):
        d = os.path.join(homes.ROOTS["claude"], "mystery")
        os.makedirs(d)
        rc, out, err = self.out(cred.cmd_cred, ["switch-guard", "--home", "mystery"])
        self.assertEqual(rc, 1)
        self.assertIn("NOT protected", err)

    def test_heal_cli_is_dry_run_by_default(self):
        cto = self.plant("cto-example-com", "cto@example.invalid", token="FAKE-CTO")
        cred.backup(cto)
        self.plant("cto-example-com", "owner@example.invalid", token="FAKE-DAVID")
        rc, out, _ = self.out(cred.cmd_cred, ["heal"])
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", out)
        self.assertEqual(cred.account_of(cto)["email"], "owner@example.invalid")
        rc, out, _ = self.out(cred.cmd_cred, ["heal", "--apply"])
        self.assertIn("APPLIED", out)
        self.assertEqual(cred.account_of(cto)["email"], "cto@example.invalid")

    def test_verb_and_help_wired_into_the_dispatcher(self):
        from helm import cli
        self.assertIn("cred", cli.VERBS)
        self.assertIn("cred", cli._VERB_HELP)
        self.plant("david-example-invalid", "owner@example.invalid")
        rc, out, _ = self.out(cli.VERBS["cred"], ["list"])
        self.assertEqual(rc, 0)
        self.assertIn("owner@example.invalid", out)

    def test_unknown_subverb_is_usage(self):
        rc, _, err = self.out(cred.cmd_cred, ["bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown subverb", err)


class SecrecyTest(CredBase):
    def test_no_output_path_ever_carries_a_credential_byte(self):
        """Every surface at once: list, backup, switch-guard, heal, doctor."""
        cto = self.plant("cto-example-com", "cto@example.invalid", token="FAKE-CTO-SECRET")
        cred.backup(cto)
        self.plant("cto-example-com", "owner@example.invalid", token="FAKE-DAVID-SECRET")
        self.plant("team-example-com", "hey@simbi.com", token="FAKE-SIMBI-SECRET")
        text = ""
        for argv in (["list"], ["list", "--json"], ["backup", "--all"],
                     ["switch-guard", "--home", "team-example-com"],
                     ["heal"], ["heal", "--apply"]):
            _, out, err = self.out(cred.cmd_cred, argv)
            text += out + err
        text += "\n".join(m for _, m in cred.doctor_rows())
        for secret in ("FAKE-CTO-SECRET", "FAKE-DAVID-SECRET", "FAKE-SIMBI-SECRET"):
            self.assertNotIn(secret, text)
        # …and the metadata files beside the creds are clean too
        for account in ("cto@example.invalid", "owner@example.invalid", "hey@simbi.com"):
            for snap in cred.snapshots(account):
                for f in ("meta.json", "account.json"):
                    body = open(os.path.join(snap["path"], f)).read()
                    self.assertNotIn("FAKE-", body, "%s/%s" % (snap["ts"], f))

    def test_digest_is_a_prefix_not_the_token(self):
        d = self.plant("david-example-invalid", "owner@example.invalid")
        meta = json.load(open(os.path.join(cred.backup(d)["dest"], "meta.json")))
        self.assertEqual(len(meta["digest"]), 12)
        self.assertNotIn(FAKE, json.dumps(meta))


class LaunchNoteTest(CredBase):
    def test_launch_prints_the_account_the_home_actually_holds(self):
        from helm import launch
        d = self.plant("cto-example-com", "owner@example.invalid")
        _, _, err = self.out(launch.home_note, d, "cto-example")
        self.assertIn("HOLDS owner@example.invalid", err)
        self.assertIn("DRIFT", err)

    def test_launch_note_on_an_unreadable_home_never_claims_an_account(self):
        from helm import launch
        d = os.path.join(homes.ROOTS["claude"], "mystery")
        os.makedirs(d)
        _, _, err = self.out(launch.home_note, d, "mystery")
        self.assertIn("account unreadable", err)
        self.assertNotIn("HOLDS", err)


if __name__ == "__main__":
    unittest.main()
