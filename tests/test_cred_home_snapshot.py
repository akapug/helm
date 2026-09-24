"""Home-provenance selection under an account-wide backup census; fake bytes only."""
import os
from unittest import mock

from helm import cli, cred, doctor, hooks, homes
from tests import test_cred as fixture


class HomeSnapshotTest(fixture.CredBase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        super().setUp()
        for patcher in (
                mock.patch.object(doctor, "CHECKS", ()),
                mock.patch.object(doctor, "_record_genesis"),
                mock.patch.object(cli, "which_helm_warning", return_value=None),
                mock.patch.object(hooks, "hook_skips_here", return_value=False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def shadow(self):
        target = self.plant("admin-example-com", "admin@example.com", token="FAKE-F0")
        original = fixture._read(os.path.join(target, cred.AUTH_JSON), "rb")
        saved = cred.backup(target, apply=True)
        self.assertTrue(saved["ok"])
        self.plant("admin-example-com", "borrower@example.com", token="FAKE-B")
        default = self.plant("default", "admin@example.com", token="FAKE-F1")
        os.rename(default, homes.DEFAULTS["claude"])
        self.assertEqual(len(cred.rows()), 2)
        self.assertEqual(len({r["account"] for r in cred.rows()}), 2)
        self.assertEqual(len({r["family"] for r in cred.rows()}), 2)
        return target, original, saved["dest"]

    def ensure(self):
        return self.out(cli.main, ["doctor", "--ensure", "--quiet"])

    def unchanged(self, target):
        return [fixture._read(os.path.join(p, n), "rb")
                for p in (target, homes.DEFAULTS["claude"])
                for n in (cred.AUTH_JSON, cred.ACCOUNT_JSON)]

    def refused(self, target, drift_count=1):
        # Census succeeds, so these controls reach selection, not an earlier
        # backup failure that would mask whether the candidate was rejected.
        self.assertTrue(all(r["ok"] for r in cred.backup_all(apply=True)))
        newest = cred.snapshots("admin@example.com")[-1]["path"]
        before = self.unchanged(target)
        plans = cred.heal_plan()
        self.assertEqual(len(plans), drift_count)
        plan = next(p for p in plans if p["path"] == target)
        self.assertEqual(plan["status"], "revocation-risk")
        self.assertEqual(plan["restore_from"], newest)
        self.assertTrue(all(p["status"] == "no-backup" for p in plans if p is not plan))
        rc, out, err = self.ensure()
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("0 of %d drifted homes restored" % drift_count, err)
        self.assertEqual(self.unchanged(target), before)

    def test_ensure_restores_own_f0_not_live_default_f1(self):
        target, original, saved = self.shadow()
        default = self.unchanged(target)[2:]
        self.assertEqual(cred.heal_plan()[0]["restore_from"], saved)
        self.assertEqual(self.ensure(), (0, "", ""))
        self.assertEqual(fixture._read(os.path.join(target, cred.AUTH_JSON), "rb"), original)
        self.assertEqual(self.unchanged(target)[2:], default)
        self.assertEqual(len(cred.snapshots("borrower@example.com")), 1)
        self.assertEqual(self.ensure(), (0, "", ""))
        self.assertEqual(fixture._read(os.path.join(saved, "credentials.json"), "rb"), original)

    def test_stale_own_candidate_is_not_a_fallback(self):
        target, _, saved = self.shadow()
        path = os.path.join(saved, "credentials.json")
        doc = fixture._load(path)
        doc["claudeAiOauth"]["expiresAt"] = 1
        fixture._dump(path, doc)
        self.refused(target)

    def test_unknown_expiry_is_not_a_fallback(self):
        target, _, saved = self.shadow()
        path = os.path.join(saved, "credentials.json")
        doc = fixture._load(path)
        del doc["claudeAiOauth"]["expiresAt"]
        fixture._dump(path, doc)
        self.refused(target)

    def test_missing_provenance_is_not_guessed(self):
        target, _, saved = self.shadow()
        path = os.path.join(saved, "meta.json")
        doc = fixture._load(path)
        del doc["source_home"]
        fixture._dump(path, doc)
        self.refused(target)

    def test_foreign_provenance_is_not_a_fallback(self):
        target, _, saved = self.shadow()
        path = os.path.join(saved, "meta.json")
        doc = fixture._load(path)
        doc["source_home"] = homes.DEFAULTS["claude"]
        fixture._dump(path, doc)
        self.refused(target)

    def test_live_own_family_elsewhere_is_still_refused(self):
        target, _, _ = self.shadow()
        self.plant("other", "admin@example.com", token="FAKE-F0")
        self.refused(target, drift_count=2)

    def test_historical_own_family_elsewhere_is_still_refused(self):
        target, _, _ = self.shadow()
        other = self.plant("other", "admin@example.com", token="FAKE-F0")
        self.assertTrue(cred.backup(other, apply=True)["ok"])
        self.plant("other", "admin@example.com", token="FAKE-ROTATED")
        self.refused(target, drift_count=2)

    def test_identity_discontinuity_is_still_refused(self):
        target, _, saved = self.shadow()
        fam = fixture._load(os.path.join(saved, "meta.json"))["family"]
        cred._lineage_record([(fam, "admin-example-com", "other@example.com")])
        self.refused(target)

    def test_newest_own_capture_blocks_older_safe_capture(self):
        target, original, saved = self.shadow()
        self.plant("admin-example-com", "admin@example.com", token="FAKE-F2")
        latest = cred.backup(target, apply=True)
        self.assertTrue(latest["ok"])
        path = os.path.join(latest["dest"], "credentials.json")
        doc = fixture._load(path)
        doc["claudeAiOauth"]["expiresAt"] = 1
        fixture._dump(path, doc)
        self.plant("admin-example-com", "borrower@example.com", token="FAKE-B")
        self.refused(target)
        self.assertEqual(fixture._read(os.path.join(saved, "credentials.json"), "rb"), original)

    def test_newest_own_consumed_capture_blocks_older_safe_capture(self):
        target, original, saved = self.shadow()
        self.plant("admin-example-com", "admin@example.com", token="FAKE-F2")
        latest = cred.backup(target, apply=True)
        self.assertTrue(latest["ok"])
        fam = fixture._load(os.path.join(latest["dest"], "meta.json"))["family"]
        cred._lineage_record([(fam, "foreign-home", "admin@example.com")])
        self.plant("admin-example-com", "borrower@example.com", token="FAKE-B")
        self.refused(target)
        self.assertEqual(fixture._read(os.path.join(saved, "credentials.json"), "rb"), original)

    def test_ambiguous_account_directory_still_refuses(self):
        target, _, saved = self.shadow()
        self.assertTrue(all(r["ok"] for r in cred.backup_all(apply=True)))
        path = os.path.join(saved, "meta.json")
        doc = fixture._load(path)
        doc["account"] = "other@example.com"
        fixture._dump(path, doc)
        before = self.unchanged(target)
        plan = cred.heal(apply=True, hook=True)["plans"][0]
        self.assertEqual(plan["status"], "ambiguous-backup")
        self.assertEqual(self.unchanged(target), before)

    def test_selected_candidate_checks_holder_after_preimage(self):
        target, _, saved = self.shadow()
        self.assertTrue(all(r["ok"] for r in cred.backup_all(apply=True)))
        self.assertEqual(cred.heal_plan()[0]["restore_from"], saved)
        before = self.unchanged(target)
        with mock.patch.object(cred, "holders_of", side_effect=[[], [], [(4242, "claude")]]):
            result = cred.heal(apply=True, hook=True)
        self.assertEqual(result["plans"][0]["status"], "held")
        self.assertIn("during pre-image capture", result["plans"][0]["reason"])
        self.assertEqual(self.unchanged(target), before)

    def test_selected_candidate_still_checks_holders(self):
        target, _, saved = self.shadow()
        self.assertTrue(all(r["ok"] for r in cred.backup_all(apply=True)))
        self.assertEqual(cred.heal_plan()[0]["restore_from"], saved)
        before = self.unchanged(target)
        with mock.patch.object(cred, "holders_of", return_value=[(4242, "claude")]):
            self.assertEqual(cred.heal(apply=True, hook=True)["plans"][0]["status"], "held")
        with mock.patch.object(cred, "holders_of", return_value=None):
            self.assertEqual(cred.heal(apply=True, hook=True)["plans"][0]["status"], "cannot-probe")
        self.assertEqual(self.unchanged(target), before)

    def test_selected_candidate_checks_holder_after_plan(self):
        target, _, saved = self.shadow()
        self.assertTrue(all(r["ok"] for r in cred.backup_all(apply=True)))
        self.assertEqual(cred.heal_plan()[0]["restore_from"], saved)
        before = self.unchanged(target)
        with mock.patch.object(cred, "holders_of", side_effect=[[], [(4242, "claude")]]):
            result = cred.heal(apply=True, hook=True)
        self.assertEqual(result["plans"][0]["status"], "held")
        self.assertEqual(self.unchanged(target), before)

    def test_selected_candidate_still_requires_occupant_preimage(self):
        target, _, saved = self.shadow()
        self.assertTrue(all(r["ok"] for r in cred.backup_all(apply=True)))
        self.assertEqual(cred.heal_plan()[0]["restore_from"], saved)
        before = self.unchanged(target)
        with mock.patch.object(cred, "backup", return_value={"ok": False, "reason": "fixture refusal"}):
            result = cred.heal(apply=True, hook=True)
        self.assertEqual(result["plans"][0]["status"], "no-preimage")
        self.assertEqual(self.unchanged(target), before)
