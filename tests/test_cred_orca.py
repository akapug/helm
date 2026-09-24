#!/usr/bin/env python3
"""task/2505 — a credhome is measured against, and synced from, Orca's managed
copy of the same account before any claude launch execs on it.

Hermetic: the claude homes root, the default home, Orca's user-data dir, the
cred backup root and the keepalive cache all live under one temp dir, and the
live-holder probe is stubbed. Every token is a synthetic FAKE string.

THE FIXTURE LAYOUT IS ORCA'S OWN, derived from the real store's structure (key
names and file names only, read off a live store):
  <orca>/claude-accounts/<uuid>/auth/.credentials.json   {claudeAiOauth: {accessToken,
        refreshToken, expiresAt, refreshTokenExpiresAt, scopes, subscriptionType,
        rateLimitTier}}
  <orca>/claude-accounts/<uuid>/auth/oauth-account.json  {accountUuid, emailAddress, ...}
  <orca>/claude-accounts/<uuid>/auth/.orca-managed-claude-auth   the <uuid> itself
  <orca>/claude-accounts/<uuid>/auth/.claude.json        {oauthAccount: {...}} (some dirs)
and the credhome side is ~/.claude-homes/<folded-email>/{.claude.json,
.credentials.json}, the shape `helm cred` already reads.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import cred, homes, launch  # noqa: E402

UUID = "0a1b2c3d-0000-4000-8000-00000000cafe"
EMAIL = "seat@example.test"


def _now_ms(hours=0):
    return int((time.time() + hours * 3600) * 1000)


# A refresh lifetime an hour past: a home whose own chain is provably spent,
# STALE against a fresher Orca copy.
SPENT = -1


def _creds(token, hours, refresh_hours=24 * 20):
    return {"claudeAiOauth": {
        "accessToken": token + "-ACCESS", "refreshToken": token + "-REFRESH",
        "expiresAt": _now_ms(hours), "refreshTokenExpiresAt": _now_ms(refresh_hours),
        "scopes": ["user:inference"], "subscriptionType": "max",
        "rateLimitTier": "default"}}


def _state(path):
    """(bytes sha256, mode, mtime_ns) — the effect an arm asserts on."""
    st = os.lstat(path)
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest(), stat.S_IMODE(st.st_mode), st.st_mtime_ns


class OrcaBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-cred-orca-")
        j = lambda *p: os.path.join(self.tmp, *p)
        self.orca = j("orca")
        for patch in (
                mock.patch.dict(os.environ, {
                    "ORCA_USER_DATA_PATH": self.orca,
                    "HELM_CRED_BACKUP_ROOT": j("cred-backups"),
                    "HELM_CACHE_DIR": j("cache")}),
                mock.patch.dict(homes.ROOTS, {"claude": j("claude-homes")}),
                mock.patch.dict(homes.DEFAULTS, {"claude": j("default-claude")}),
                mock.patch.object(cred, "holders_of", lambda p, default=False: [])):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(cred.SYNC_ENV, None)
        os.makedirs(homes.ROOTS["claude"])
        cred.cache_clear()
        self.addCleanup(cred.cache_clear)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def plant_home(self, name, email=EMAIL, creds=None, org=None):
        d = os.path.join(homes.ROOTS["claude"], name)
        os.makedirs(d, exist_ok=True)
        oa = {"emailAddress": email, "accountUuid": "acct-1"}
        if org:
            oa["organizationUuid"] = org
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"numStartups": 1, "oauthAccount": oa}, f)
        if creds is not None:
            with open(os.path.join(d, ".credentials.json"), "w") as f:
                json.dump(creds, f)
            os.chmod(os.path.join(d, ".credentials.json"), 0o600)
        cred.cache_clear()
        return d

    def plant_orca(self, uuid=UUID, email=EMAIL, creds=None, marker=True,
                   cfg_email=None):
        auth = os.path.join(self.orca, "claude-accounts", uuid, "auth")
        os.makedirs(auth, exist_ok=True)
        with open(os.path.join(auth, "oauth-account.json"), "w") as f:
            json.dump({"accountUuid": "acct-1", "emailAddress": email,
                       "organizationUuid": "org-1"}, f)
        if cfg_email:
            with open(os.path.join(auth, ".claude.json"), "w") as f:
                json.dump({"oauthAccount": {"emailAddress": cfg_email}}, f)
        if marker:
            with open(os.path.join(auth, ".orca-managed-claude-auth"), "w") as f:
                f.write(uuid)
        with open(os.path.join(auth, ".credentials.json"), "w") as f:
            json.dump(creds if creds is not None else _creds("FAKE-ORCA", 8), f)
        os.chmod(os.path.join(auth, ".credentials.json"), 0o600)
        return auth

    def read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def orca_held_then_moved(self, home):
        """A history of possession, driven through the shipped producer: Orca
        holds the home's own token while an applying sync measures it, then
        Orca's dir carries a new token with a close refresh lifetime (a refresh
        or a fresh login of that dir; nothing local tells which).
        -> Orca's auth dir."""
        with open(os.path.join(home, ".credentials.json")) as fh:
            self.plant_orca(creds=json.load(fh))
        self.assertEqual(cred.sync(home, apply=True)["verdict"], cred.FRESH)
        return self.plant_orca()


class SyncTest(OrcaBase):
    def test_a_stale_home_is_synced_from_orca_and_orca_is_untouched(self):
        """THE INCIDENT: identity AGREE, token weeks behind Orca's, and the
        home's own refresh lifetime already past.

        CONTROL: the dry run on the identical fixture leaves the home's bytes
        and mtime exactly as planted — so the write below is the apply, not the
        measurement. Mutation: flip `ofacts["expires_at"] > hfacts["expires_at"]`
        in orca._measure and this arm reads FRESH with nothing written."""
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -300, SPENT))
        auth = self.plant_orca()
        orca_creds = os.path.join(auth, ".credentials.json")
        home_creds = os.path.join(home, ".credentials.json")
        orca_before, home_before = _state(orca_creds), _state(home_creds)

        plan = cred.sync(home)
        self.assertEqual((plan["verdict"], plan["action"]), (cred.STALE, "would-sync"))
        self.assertEqual(_state(home_creds), home_before)

        res = cred.sync(home, apply=True)
        self.assertEqual(res["action"], "synced", res["reason"])
        self.assertEqual(self.read(home_creds), self.read(orca_creds))
        self.assertEqual(stat.S_IMODE(os.lstat(home_creds).st_mode), 0o600)
        self.assertEqual(_state(orca_creds), orca_before)
        # the pre-image is the OLD home bytes, filed the way `cred backup` files
        with open(os.path.join(res["pre_image"], "credentials.json"), "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), home_before[0])
        self.assertEqual(cred.freshness(home)["verdict"], cred.FRESH)

    def test_a_fresher_home_is_never_overwritten(self):  # noqa: VACUOUS_ASSERTION — the verdict/action tuple is asserted positively before the byte-and-mtime equality
        """CONTROL: the first arm is this fixture with the expiries swapped, and
        it writes. Mutation: invert _measure's staleness comparison to `<` and
        this arm's byte-and-mtime equality goes RED."""
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", 8))
        self.plant_orca(creds=_creds("FAKE-ORCA", 2))
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        res = cred.sync(home, apply=True)
        self.assertEqual((res["verdict"], res["action"]), (cred.FRESH, "none"))
        self.assertEqual(_state(home_creds), before)

    def test_identity_is_matched_by_email_never_by_dir_name(self):  # noqa: VACUOUS_ASSERTION — the same arm ends with an unconditional synced action on an agreeing home against the same Orca store
        """An Orca dir holding ANOTHER account whose folded email is this home's
        dir name must not be synced in; the real match lives in a dir whose name
        says nothing. CONTROL: once the matching dir exists the same home syncs."""
        home = self.plant_home("other-example-test", email=EMAIL,
                               creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca(uuid="11111111-0000-4000-8000-000000000001",
                        email="other@example.test", creds=_creds("FAKE-OTHER", 8))
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        res = cred.sync(home, apply=True)
        self.assertEqual((res["verdict"], res["action"]), (cred.NO_COPY, "none"))
        self.assertEqual(_state(home_creds), before)

        self.plant_orca(uuid="22222222-0000-4000-8000-000000000002")
        # the home's name promises other@, so the estate verdict is DRIFT and
        # the sync refuses to entrench it; freshness itself found the copy
        self.assertEqual(cred.freshness(home)["verdict"], cred.STALE)
        self.assertEqual(cred.sync(home, apply=True)["action"], "skip")
        self.assertEqual(_state(home_creds), before)
        agree = self.plant_home("seat-example-test", creds=_creds("FAKE-AGREE", -10, SPENT))
        self.assertEqual(cred.sync(agree, apply=True)["action"], "synced")

    def test_an_orca_dir_without_its_ownership_marker_is_unknown_and_unwritten(self):  # noqa: VACUOUS_ASSERTION — the reason is asserted to name the marker, a presence check on the same result, before the unchanged-bytes claim
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca(marker=False)
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        res = cred.sync(home, apply=True)
        self.assertEqual((res["verdict"], res["action"]), (cred.UNKNOWN, "none"))
        self.assertIn(".orca-managed-claude-auth", res["reason"])
        self.assertEqual(_state(home_creds), before)

    def test_a_live_claude_holder_blocks_the_write(self):  # noqa: VACUOUS_ASSERTION — each leg asserts the skip action positively before the unchanged-bytes claim; the unheld write is the first arm of this class
        """One writer per home. CONTROL: the first arm is this fixture with no
        holder, and it writes; here both a proven holder and an unprovable probe
        leave the bytes alone. Mutation: drop the first holder check and the
        post-pre-image re-probe still keeps the bytes, but a snapshot lands and
        the reason no longer names pid 4242 — both asserted."""
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca()
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        for held in ([(4242, "claude")], None):
            with self.subTest(held=held), \
                    mock.patch.object(cred, "holders_of", lambda p, default=False: held):
                res = cred.sync(home, apply=True)
                self.assertEqual(res["action"], "skip")
                self.assertEqual(_state(home_creds), before)
                # refused BEFORE the pre-image: a held home gets no snapshot
                # written, and the refusal names the holder it saw
                self.assertIsNone(res["pre_image"])
                self.assertFalse(os.path.exists(os.environ["HELM_CRED_BACKUP_ROOT"]))
                self.assertIn("4242" if held else "could not be proven free",
                              res["reason"])

    def test_orcas_family_live_in_another_home_is_never_copied(self):  # noqa: VACUOUS_ASSERTION — each leg asserts the skip action and the named home positively before the unchanged-bytes claim; the incident arm is the writing control
        """P1: Orca's copy of the account it switched ~/.claude to IS ~/.claude's
        chain. Copying it would put one refresh token in two homes with two live
        refreshers — the state heal refuses and doctor FAILs.

        CONTROL: the incident arm is this fixture with no other home holding
        Orca's family, and it writes. Mutation: drop the plan-time `shared()`
        check in orca.sync and the dry run reads would-sync and the apply
        writes (the in-lock check is proven by the next arm)."""
        auth = self.plant_orca()
        orca_bytes = self.read(os.path.join(auth, ".credentials.json"))
        for leg, other in (("default", homes.DEFAULTS["claude"]),
                           ("named", os.path.join(homes.ROOTS["claude"], "twin-home"))):
            with self.subTest(leg=leg):
                os.makedirs(other, exist_ok=True)
                with open(os.path.join(other, ".credentials.json"), "wb") as f:
                    f.write(orca_bytes)
                home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
                home_creds = os.path.join(home, ".credentials.json")
                before = _state(home_creds)
                plan = cred.sync(home)
                self.assertEqual((plan["verdict"], plan["action"]), (cred.STALE, "skip"))
                res = cred.sync(home, apply=True)
                self.assertEqual(res["action"], "skip")
                self.assertIn(cred._display_path(other), res["reason"])
                self.assertIn("claude /login", res["reason"])
                self.assertIsNone(res["pre_image"])
                self.assertEqual(_state(home_creds), before)
                err = io.StringIO()
                self.assertTrue(cred.launch_sync(home, "seat-example-test", out=err))
                self.assertIn("NOT synced", err.getvalue())
                self.assertEqual(_state(home_creds), before)
                shutil.rmtree(other)

    def test_a_census_that_cannot_read_another_home_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — every leg asserts the skip action and the unprovable reason positively before the unchanged-bytes claim, and the arm ends with an unconditional synced control
        """F1: an unread home is not a home without Orca's family. The
        default home and a named twin each hold Orca's token in a credentials
        file this process cannot read, and the homes root itself cannot be
        listed: each census is unprovable and nothing is written. CONTROL: the
        same fixture with every home readable and no copy of Orca's family
        elsewhere is synced. Mutation: fold an unreadable file back to "no
        token" in _family_live_elsewhere and the default/named legs write."""
        auth = self.plant_orca()
        orca_bytes = self.read(os.path.join(auth, ".credentials.json"))
        root = homes.ROOTS["claude"]
        for leg in ("default", "named", "root"):
            with self.subTest(leg=leg):
                home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
                home_creds = os.path.join(home, ".credentials.json")
                before = _state(home_creds)
                other = {"default": homes.DEFAULTS["claude"],
                         "named": os.path.join(root, "twin-home")}.get(leg)
                locked = root
                if other:
                    os.makedirs(other, exist_ok=True)
                    locked = os.path.join(other, ".credentials.json")
                    with open(locked, "wb") as f:
                        f.write(orca_bytes)
                    os.chmod(locked, 0)
                else:
                    os.chmod(root, 0o300)     # traversable, not listable
                try:
                    if other:
                        self.assertFalse(os.access(locked, os.R_OK),
                                         "this arm needs an unreadable file")
                    else:
                        with self.assertRaises(OSError):
                            os.listdir(root)
                    plan = cred.sync(home)
                    self.assertEqual((plan["verdict"], plan["action"]), (cred.STALE, "skip"))
                    self.assertIn("cannot prove Orca's refresh chain is live in no other "
                                  "home", plan["reason"])
                    res = cred.sync(home, apply=True)
                    self.assertEqual(res["action"], "skip")
                    self.assertIsNone(res["pre_image"])
                    self.assertEqual(_state(home_creds), before)
                finally:
                    os.chmod(locked, 0o700 if not other else 0o600)
                if other:
                    shutil.rmtree(other)
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        res = cred.sync(home, apply=True)
        self.assertEqual(res["action"], "synced", res["reason"])
        self.assertEqual(self.read(os.path.join(home, ".credentials.json")), orca_bytes)

    def test_orcas_family_arriving_elsewhere_inside_the_lock_stops_the_write(self):  # noqa: VACUOUS_ASSERTION — the skip action, the two-call count and a taken pre-image are asserted positively before the unchanged-bytes claim
        """The in-lock re-check: ~/.claude takes Orca's family between the plan
        and the write. CONTROL: the incident arm, where both checks answer None,
        writes. Mutation: delete the second `shared()` call in orca.sync and the
        home is written."""
        from helm.cred import orca
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca()
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        with mock.patch.object(orca, "_family_live_elsewhere",
                               side_effect=[(None, None),
                                            (homes.DEFAULTS["claude"], None)]) as fe:
            res = cred.sync(home, apply=True)
        self.assertEqual(fe.call_count, 2)
        self.assertEqual(res["action"], "skip")
        self.assertIsNotNone(res["pre_image"])      # it got as far as the lock
        self.assertEqual(_state(home_creds), before)

    def test_an_independent_live_chain_behind_orca_is_own_chain_and_unwritten(self):  # noqa: VACUOUS_ASSERTION — the expired leg asserts synced and the copied bytes unconditionally beside the live leg's unchanged-bytes claim
        """P2: access expiries across two login chains say nothing about which
        works. A home whose refresh lifetime is live and days away from Orca's is
        OWN-CHAIN, never written. CONTROL: the `expired` leg — the same fixture
        with the home's refresh lifetime in the past — is synced (and reads FRESH
        after), as is
        the incident arm (one grant lifetime). Mutation: classify every
        Orca-ahead home STALE (drop the `chain = None` fallthrough) and the live
        leg is written."""
        orca = _creds("FAKE-ORCA", 8)
        for leg, refresh_hours, verdict, action in (
                ("live", 24 * 3, cred.OWN_CHAIN, "none"),
                ("expired", -24, cred.FRESH, "synced")):
            with self.subTest(leg=leg):
                home = self.plant_home("seat-example-test",
                                       creds=_creds("FAKE-HOME", -2, refresh_hours))
                auth = self.plant_orca(creds=orca)
                home_creds = os.path.join(home, ".credentials.json")
                before = _state(home_creds)
                res = cred.sync(home, apply=True)
                self.assertEqual((res["verdict"], res["action"]), (verdict, action),
                                 res["reason"])
                if action == "none":
                    self.assertIn("own login chain", res["reason"])
                    self.assertEqual(_state(home_creds), before)
                else:
                    self.assertEqual(self.read(home_creds),
                                     self.read(os.path.join(auth, ".credentials.json")))

    def test_an_orca_identity_file_moving_during_the_sync_stops_the_write(self):  # noqa: VACUOUS_ASSERTION — the tear count and the skip reason are asserted positively before the unchanged-bytes claim
        """P3: Orca's oauth-account.json is the only identity proof for its
        .credentials.json, read at another moment — so its stat key is taken
        before the read and re-checked beside the credentials key.

        The tear is injected after Orca's credentials read on every measurement,
        so the identity read and the credentials read straddle an Orca rewrite.
        CONTROL: the incident arm, untorn, writes. Mutation: compare only
        `orca["key"]` at the write and the home is written."""
        from helm.cred import orca
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        auth = self.plant_orca()
        acct = os.path.join(auth, "oauth-account.json")
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        real_read, n = orca._read_creds, [0]

        def torn(path):
            out = real_read(path)
            if path.startswith(auth):
                n[0] += 1
                with open(acct, "w") as f:
                    json.dump({"accountUuid": "acct-1", "emailAddress": EMAIL,
                               "organizationUuid": "org-1", "rewrite": n[0]}, f)
            return out

        with mock.patch.object(orca, "_read_creds", side_effect=torn):
            res = cred.sync(home, apply=True)
        self.assertEqual(n[0], 2)
        self.assertEqual(res["action"], "skip")
        self.assertIn("identity files changed", res["reason"])
        self.assertEqual(_state(home_creds), before)

    def test_the_post_write_check_refuses_bytes_that_do_not_read_back_0600(self):
        """The post-write check is a bytes-and-mode check, and it can fail.
        CONTROL: the incident arm, written through the real _atomic_private, is
        synced. Mutation: drop the `mode != 0o600` clause and this reads synced."""
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca()
        real = cred._atomic_private

        home_creds = os.path.join(home, ".credentials.json")

        def loose(path, blob, mode):
            real(path, blob, mode)
            if os.path.realpath(path) == os.path.realpath(home_creds):  # pre-image stays 0600
                os.chmod(path, 0o644)

        with mock.patch.object(cred, "_atomic_private", side_effect=loose):
            res = cred.sync(home, apply=True)
        self.assertEqual((res["action"], res["exec_ok"]), ("refused", False))
        self.assertIn("mode 0600", res["reason"])

    def test_another_organization_under_the_same_email_is_not_this_homes_copy(self):  # noqa: VACUOUS_ASSERTION — the verdict tuple and both org uuids are asserted positively before the unchanged-bytes claim; the org-1 leg is the writing control
        """P3: one email can sit in several organizations. CONTROL: the same home
        in org-1 (Orca's org) is synced. Mutation: drop the `clash` branch in
        orca_copy and the org-A leg is written with org-1's token."""
        for org, verdict, action in (("org-A", cred.UNKNOWN, "none"),
                                     ("org-1", cred.FRESH, "synced")):
            with self.subTest(org=org):
                home = self.plant_home("seat-example-test", org=org,
                                       creds=_creds("FAKE-HOME", -10, SPENT))
                self.plant_orca()
                home_creds = os.path.join(home, ".credentials.json")
                before = _state(home_creds)
                res = cred.sync(home, apply=True)
                self.assertEqual((res["verdict"], res["action"], res["exec_ok"]),
                                 (verdict, action, True), res["reason"])
                if action == "none":
                    self.assertIn("org-A", res["reason"])
                    self.assertIn("org-1", res["reason"])
                    self.assertEqual(_state(home_creds), before)

    def test_a_secondary_claude_json_naming_this_account_is_unknown_not_a_refusal(self):  # noqa: VACUOUS_ASSERTION — the verdict and launch_sync's True are asserted positively before the unchanged-bytes claim; the torn-dir arm is the DISAGREE control
        """P3: a dir whose authoritative oauth-account.json names ANOTHER account
        would never be written into this home, so its stale secondary
        .claude.json naming this one cannot refuse this home's launch.
        CONTROL: `test_a_torn_orca_dir_disagrees_and_forbids_exec` — the
        authoritative file naming this account beside another — still refuses.
        Mutation: restore the symmetric `named != cfg_email` disagree test and
        this leg refuses exec."""
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca(email="someone-else@example.test", cfg_email=EMAIL)
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        res = cred.sync(home, apply=True)
        self.assertEqual((res["verdict"], res["action"], res["exec_ok"]),
                         (cred.UNKNOWN, "none", True))
        self.assertIn("someone-else@example.test", res["reason"])
        self.assertTrue(cred.launch_sync(home, "seat-example-test", out=io.StringIO()))
        self.assertEqual(_state(home_creds), before)

    def test_an_existing_secondary_identity_that_cannot_be_read_never_supports_a_copy(self):  # noqa: VACUOUS_ASSERTION — each leg asserts the UNKNOWN verdict and its reason positively before the unchanged-bytes claim, and the arm ends with an unconditional synced control
        """F2: the dir's authoritative oauth-account.json names this
        account and its secondary .claude.json names ANOTHER — the DISAGREE
        population — but the secondary cannot be read (mode 0) or is not an
        identity object. Neither is "primary-only": each reads UNKNOWN and
        writes nothing. CONTROL: the same dir with no secondary at all syncs.
        Mutation: treat an unreadable secondary as absent (drop cfg_unread) and
        both legs write Orca's copy into the home."""
        for leg in ("unreadable", "malformed"):
            with self.subTest(leg=leg):
                shutil.rmtree(os.path.join(self.orca, "claude-accounts"), True)
                home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
                auth = self.plant_orca(cfg_email="someone-else@example.test")
                cfg = os.path.join(auth, ".claude.json")
                if leg == "malformed":
                    with open(cfg, "w") as f:
                        f.write("[1, 2]")
                else:
                    os.chmod(cfg, 0)
                home_creds = os.path.join(home, ".credentials.json")
                before = _state(home_creds)
                try:
                    if leg == "unreadable":
                        self.assertFalse(os.access(cfg, os.R_OK),
                                         "this arm needs an unreadable file")
                    res = cred.sync(home, apply=True)
                    self.assertEqual((res["verdict"], res["action"]), (cred.UNKNOWN, "none"))
                    self.assertIn("could not be read as an identity", res["reason"])
                    self.assertEqual(_state(home_creds), before)
                finally:
                    os.chmod(cfg, 0o600)
        shutil.rmtree(os.path.join(self.orca, "claude-accounts"), True)
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca()
        res = cred.sync(home, apply=True)
        self.assertEqual(res["action"], "synced", res["reason"])

    def test_an_unreadable_sibling_dir_makes_a_found_copy_unknown(self):  # noqa: VACUOUS_ASSERTION — the verdict tuple and the unreadable count are asserted positively before the unchanged-bytes claim
        """P3: an unreadable dir may claim the same account, and two claimants
        resolve to UNKNOWN. CONTROL: the incident arm (the found dir alone)
        writes. Mutation: drop `if mine and unread` and this home is written."""
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca()
        bad = os.path.join(self.orca, "claude-accounts",
                           "ffffffff-0000-4000-8000-00000000dead", "auth")
        os.makedirs(bad)
        with open(os.path.join(bad, "oauth-account.json"), "w") as f:
            f.write("{not json")
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        res = cred.sync(home, apply=True)
        self.assertEqual((res["verdict"], res["action"]), (cred.UNKNOWN, "none"))
        self.assertIn("1 other account dir is unreadable", res["reason"])
        self.assertEqual(_state(home_creds), before)

    def test_a_torn_orca_dir_disagrees_and_forbids_exec(self):
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca(cfg_email="someone-else@example.test")
        res = cred.sync(home, apply=True)
        self.assertEqual((res["verdict"], res["action"], res["exec_ok"]),
                         (cred.DISAGREE, "refused", False))
        self.assertIn(EMAIL, res["reason"])
        self.assertIn("someone-else@example.test", res["reason"])

    def test_no_output_carries_a_token_byte(self):  # noqa: VACUOUS_ASSERTION — the positive half asserts each surface printed its verdict first
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca()
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            cred.cmd_cred(["list"])
            cred.cmd_cred(["list", "--json"])
            cred.cmd_cred(["sync-orca", "--home", "seat-example-test"])
            cred.launch_sync(home, "seat-example-test")
        text = buf.getvalue() + err.getvalue()
        self.assertIn(cred.STALE, text)
        self.assertIn("synced", text)
        for secret in ("FAKE-HOME-", "FAKE-ORCA-"):
            self.assertNotIn(secret, text)


class ListFreshnessTest(OrcaBase):
    def test_list_shows_stale_where_the_identity_agrees(self):  # noqa: VACUOUS_ASSERTION — every assertion is a presence check (AGREE and STALE in the row, FRESH in the json)
        """The AGREE-while-stale lie. CONTROL: the same row reads FRESH once the
        home's token is ahead of Orca's."""
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cred.cmd_cred(["list"]), 0)
        row = [l for l in buf.getvalue().splitlines() if "seat-example-test" in l][0]
        self.assertIn("AGREE", row)
        self.assertIn(cred.STALE, row)
        with open(os.path.join(home, ".credentials.json"), "w") as f:
            json.dump(_creds("FAKE-HOME", 20), f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cred.cmd_cred(["list", "--json"])
        rows = {r["name"]: r for r in json.loads(buf.getvalue())}
        self.assertEqual(rows["seat-example-test"]["orca"]["verdict"], cred.FRESH)

    def test_the_sync_note_is_printed_only_where_the_sync_would_write(self):  # noqa: VACUOUS_ASSERTION — the AGREE row's positive sync-orca presence is the control beside the DRIFT and shared-family rows' absence
        """P3: a DRIFT home is STALE by freshness but the sync skips it, and a
        home whose Orca family is live elsewhere is skipped too — the note must
        not promise a sync for either (the DRIFT row keeps its own drift note).
        CONTROL: the AGREE row promises it.
        Mutation: print the note on every STALE verdict and the DRIFT row
        carries `sync-orca`."""
        self.plant_home("seat-example-test", creds=_creds("FAKE-AGREE", -10, SPENT))
        self.plant_home("other-example-test", creds=_creds("FAKE-DRIFT", -10, SPENT))
        self.plant_orca()

        def rows():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cred.cmd_cred(["list"])
            return {n: next(l for l in buf.getvalue().splitlines()
                            if l.strip().startswith(n + " "))
                    for n in ("seat-example-test", "other-example-test")}

        got = rows()
        self.assertIn("sync-orca --home seat-example-test --apply", got["seat-example-test"])
        self.assertIn("DRIFT", got["other-example-test"])
        self.assertIn(cred.STALE, got["other-example-test"])
        self.assertNotIn("sync-orca", got["other-example-test"])
        self.assertIn("this account's home is seat-example-test", got["other-example-test"])
        os.makedirs(homes.DEFAULTS["claude"])
        with open(os.path.join(homes.DEFAULTS["claude"], ".credentials.json"), "wb") as f:
            f.write(self.read(os.path.join(self.orca, "claude-accounts", UUID, "auth",
                                           ".credentials.json")))
        got = rows()
        self.assertNotIn("sync-orca --home", got["seat-example-test"])
        self.assertIn("NOT syncable", got["seat-example-test"])


class ListHeldHomeTest(OrcaBase):
    def test_a_held_home_is_never_promised_a_sync(self):  # noqa: VACUOUS_ASSERTION — the free-home control after the loop asserts the promised sync unconditionally
        """F4: the list's dry run stops before the holder check both
        applying doors make, so a home a live claude holds was promised a sync
        that neither `sync-orca --apply` nor the next launch performs. The row
        now says it stays unsynced while held and names the pid; an
        unprovable probe says so. CONTROL: the same home with no holder is
        promised the sync. Mutation: drop the holder read in cred list and the
        held row promises the next launch syncs it."""
        self.plant_home("seat-example-test", creds=_creds("FAKE-AGREE", -10, SPENT))
        self.plant_orca()

        def row():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cred.cmd_cred(["list"])
            return next(l for l in buf.getvalue().splitlines()
                        if l.strip().startswith("seat-example-test "))

        for held, text in (([(4242, "claude")], "live claude pid 4242"),
                           (None, "could not be ruled out")):
            with self.subTest(held=held), \
                    mock.patch.object(cred, "holders_of",
                                      lambda p, default=False, _h=held: _h):
                got = row()
                self.assertIn("NOT synced while held", got)
                self.assertIn(text, got)
                self.assertNotIn("next `helm launch --home` syncs it", got)
        got = row()
        self.assertIn("sync-orca --home seat-example-test --apply` or the next "
                      "`helm launch --home` syncs it", got)


class ChainUnprovenTest(OrcaBase):
    """F3: refresh lifetimes within the jitter window are one grant OR
    two logins minted minutes apart, so they prove no chain, whatever helm saw before.
    Such a home is neither copied onto nor launched on."""

    def _row(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cred.cmd_cred(["list"])
        return next(l for l in buf.getvalue().splitlines()
                    if l.strip().startswith("seat-example-test "))

    def test_close_lifetimes_are_neither_copied_nor_launched_whatever_helm_saw_before(self):  # noqa: VACUOUS_ASSERTION — the operator leg at the end asserts synced and the copied bytes unconditionally
        """r1 F3 and r2 F1. The witness: two live chains of one
        account, refresh lifetimes milliseconds apart, Orca's access expiry
        later. Refused: nothing written, exec_ok False, no file under the
        backup root, the spawn note says the launch refuses, and the list row
        and the reason name both cures. The HISTORY leg: an applying sync saw
        Orca holding the home's own token, then Orca's dir carries a new close
        token (a refresh or a re-login; nothing local tells which) — still
        CHAIN-UNPROVEN, nothing written.
        CONTROL: the operator's --replace-own-chain on the same home syncs.
        Mutation: classify a within-window pair STALE (restore a
        rotated-by-orca chain) and both legs are written over a possibly live
        login."""
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10))
        self.plant_orca()
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        res = cred.sync(home, apply=True)
        self.assertEqual((res["verdict"], res["action"], res["exec_ok"]),
                         (cred.UNPROVEN, "refused", False), res["reason"])
        self.assertIn("within 5 min", res["reason"])
        self.assertIn("claude /login", res["reason"])
        self.assertIn("sync-orca --home seat-example-test --apply --replace-own-chain",
                      res["reason"])
        self.assertIsNone(res["pre_image"])
        self.assertEqual(_state(home_creds), before)
        self.assertFalse(os.path.exists(os.environ["HELM_CRED_BACKUP_ROOT"]))
        self.assertIn("REFUSE to start claude", cred.spawn_note(home))
        row = self._row()
        self.assertIn(cred.UNPROVEN, row)
        self.assertIn("--replace-own-chain", row)
        self.assertNotIn("syncs it", row)

        auth = self.orca_held_then_moved(home)
        res = cred.sync(home, apply=True)
        self.assertEqual((res["verdict"], res["action"]), (cred.UNPROVEN, "refused"),
                         res["reason"])
        self.assertEqual(_state(home_creds), before)

        res = cred.sync(home, apply=True, replace_own_chain=True)
        self.assertEqual(res["action"], "synced", res["reason"])
        self.assertEqual(self.read(home_creds),
                         self.read(os.path.join(auth, ".credentials.json")))

    def test_a_missing_refresh_lifetime_proves_no_chain(self):
        """A home token with no refreshTokenExpiresAt, behind Orca: its chain can
        be neither placed in Orca's grant nor told apart from it. CONTROL: the
        close-lifetime arm above reads the same verdict with the within-window
        reason; a spent lifetime reads STALE (the SPENT fixtures). Mutation:
        classify a missing lifetime OWN-CHAIN and this arm reads OWN-CHAIN."""
        creds = _creds("FAKE-HOME", -10)
        del creds["claudeAiOauth"]["refreshTokenExpiresAt"]
        home = self.plant_home("seat-example-test", creds=creds)
        self.plant_orca()
        res = cred.sync(home, apply=True)
        self.assertEqual((res["verdict"], res["action"]), (cred.UNPROVEN, "refused"))
        self.assertIn("refresh lifetime is missing", res["reason"])

    def test_replace_own_chain_is_the_operators_word_behind_every_fence(self):  # noqa: VACUOUS_ASSERTION — each flagged leg asserts rc 0 and the copied bytes positively
        """`sync-orca --replace-own-chain --apply` writes an unproven and an
        own-chain home. CONTROL: the same command without the flag leaves the
        bytes unchanged (exit 1 refused for unproven, exit 0 for own-chain); with the flag, Orca's family live in another home
        still writes nothing. Mutation: leave replace_own_chain out of the
        CLI's sync call and every flagged leg exits 1 unwritten."""
        auth = self.plant_orca()
        orca_bytes = self.read(os.path.join(auth, ".credentials.json"))
        argv = ["sync-orca", "--home", "seat-example-test", "--apply"]
        for leg, refresh_hours, plain_rc in (("unproven", 24 * 20, 1),
                                             ("own-chain", 24 * 3, 0)):
            with self.subTest(leg=leg):
                home = self.plant_home("seat-example-test",
                                       creds=_creds("FAKE-HOME", -10, refresh_hours))
                home_creds = os.path.join(home, ".credentials.json")
                before = _state(home_creds)
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(cred.cmd_cred(argv), plain_rc)
                self.assertEqual(_state(home_creds), before)
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(io.StringIO()):
                    rc = cred.cmd_cred(argv + ["--replace-own-chain"])
                self.assertEqual(rc, 0, out.getvalue())
                self.assertEqual(self.read(home_creds), orca_bytes)
                self.assertIn("on the operator's word", out.getvalue())
        twin = os.path.join(homes.ROOTS["claude"], "twin-home")
        os.makedirs(twin)
        with open(os.path.join(twin, ".credentials.json"), "wb") as f:
            f.write(orca_bytes)
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10))
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        res = cred.sync(home, apply=True, replace_own_chain=True)
        self.assertEqual(res["action"], "skip", res["reason"])
        self.assertIn("twin-home", res["reason"])
        self.assertEqual(_state(home_creds), before)


    def test_a_home_outside_the_credhome_root_is_never_offered_the_credhome_doors(self):  # noqa: VACUOUS_ASSERTION — the named leg asserts the replacement command and the launch refusal positively on both surfaces
        """r4 P3: sync-orca writes and a launch syncs only a named
        credhome, so a reason about the default ~/.claude (or any home outside
        the credhome root) that names --replace-own-chain, sync-orca or a
        refused launch is a command that refuses. Both surfaces, the
        measurement's reason and keepalive's skip, name only the fresh login
        there. CONTROL: the same tokens in a named credhome name the
        replacement and the refused launch. Mutation: drop the is_credhome
        branch in orca._cure and the default leg names sync-orca."""
        from helm import keepalive
        self.plant_orca()
        default = homes.DEFAULTS["claude"]
        os.makedirs(default, exist_ok=True)
        with open(os.path.join(default, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": EMAIL, "accountUuid": "acct-1"}}, f)
        for leg, refresh_hours in (("unproven", 24 * 20), ("stale", SPENT)):
            with open(os.path.join(default, ".credentials.json"), "w") as f:
                json.dump(_creds("FAKE-DEFAULT", -10, refresh_hours), f)
            named = self.plant_home("seat-example-test",
                                    creds=_creds("FAKE-HOME", -10, refresh_hours))
            cred.cache_clear()
            for where, home in (("named", named), ("default", default)):
                with self.subTest(leg=leg, home=where):
                    fr = cred.freshness(home)
                    self.assertEqual(fr["verdict"],
                                     cred.UNPROVEN if leg == "unproven" else cred.STALE)
                    said = [keepalive._orca_holds_the_chain(home)]
                    if leg == "unproven":
                        said.append(fr["reason"])
                    for text in said:
                        if where == "default" or leg == "unproven":
                            self.assertIn("claude /login", text)
                        if where == "named":
                            self.assertIn("sync-orca --home seat-example-test", text)
                        else:
                            self.assertNotIn("sync-orca", text)
                            self.assertNotIn("replace-own-chain", text)
                    if leg == "unproven":
                        self.assertEqual("not started on it" in fr["reason"],
                                         where == "named")


class LaunchSyncTest(OrcaBase):
    ENV = ("CLAUDE_CONFIG_DIR", "HELM_CHAT_NAME", "HELM_CHAT_ROOM",
           "HELM_SPAWN_ATTEMPT")

    def setUp(self):
        super().setUp()
        saved = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            for k in ("HELM_HOME", "HELM_CHAT_DIR"):
                os.environ.pop(k, None)
        self.addCleanup(restore)

    def _launch(self, argv, home_creds):
        """-> (rc, stderr, bytes the home held AT EXEC, exec mock, join mock)."""
        seen = {}

        def at_exec(child_argv, env):
            seen["bytes"] = self.read(home_creds)
            seen["argv"] = child_argv
            return 0

        err = io.StringIO()
        with mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.seat_launch_owner, "exec_attached",
                                  side_effect=at_exec) as ex, \
                contextlib.redirect_stderr(err):
            rc = launch.cmd_launch(argv)
        return rc, err.getvalue(), seen, ex, join

    def test_the_home_holds_orcas_token_when_claude_execs(self):  # noqa: VACUOUS_ASSERTION — three subtests run unconditionally; each asserts exec was called once and the exact bytes seen at exec
        """Asserted at the moment of exec, on the file claude will read — for the
        --home door and for the inherited CLAUDE_CONFIG_DIR a spawn pins.
        CONTROL: with the sync switched off the same launch execs on the stale
        bytes, so the equality is the sync and not the fixture."""
        for door in ("--home", "inherited", "disabled"):
            with self.subTest(door=door):
                home = self.plant_home("seat-example-test",
                                       creds=_creds("FAKE-HOME", -10, SPENT))
                orca_creds = os.path.join(self.plant_orca(), ".credentials.json")
                home_creds = os.path.join(home, ".credentials.json")
                stale = self.read(home_creds)
                argv = ["--no-install", "--seat", "alice"]
                if door == "--home":
                    argv += ["--home", "seat-example-test"]
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = home
                if door == "disabled":
                    os.environ[cred.SYNC_ENV] = "0"
                try:
                    rc, err, seen, ex, _join = self._launch(argv, home_creds)
                finally:
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                    os.environ.pop(cred.SYNC_ENV, None)
                self.assertEqual(rc, 0, err)
                ex.assert_called_once()
                expect = stale if door == "disabled" else self.read(orca_creds)
                self.assertEqual(seen["bytes"], expect)
                self.assertIn("disabled" if door == "disabled" else "STALE-vs-ORCA", err)
                if door != "disabled":
                    # the synced seat now shares Orca's chain: the line says so
                    self.assertIn("do not switch Orca to that account", err)

    def test_an_unproven_chain_execs_nothing_and_joins_nothing(self):  # noqa: VACUOUS_ASSERTION — the control leg asserts exec and join were each called once
        """A launch onto a CHAIN-UNPROVEN home is refused before the roster row:
        claude presenting a token Orca may have rotated away is the reuse that
        revokes Orca's live family. CONTROL: once the operator's
        --replace-own-chain synced the home the same launch execs. Mutation:
        return exec_ok True for CHAIN-UNPROVEN in orca.sync and the refused leg
        execs on the home's token."""
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10))
        self.plant_orca()
        home_creds = os.path.join(home, ".credentials.json")
        argv = ["--no-install", "--seat", "alice", "--home", "seat-example-test"]
        rc, err, _seen, ex, join = self._launch(argv, home_creds)
        self.assertEqual(rc, 1, err)
        self.assertIn("REFUSED", err)
        self.assertIn("--replace-own-chain", err)
        ex.assert_not_called()
        join.assert_not_called()
        self.assertEqual(cred.sync(home, apply=True, replace_own_chain=True)["action"],
                         "synced")
        rc, err, _seen, ex, join = self._launch(argv, home_creds)
        self.assertEqual(rc, 0, err)
        ex.assert_called_once()
        join.assert_called_once()

    def test_a_disagreeing_identity_execs_nothing_and_joins_nothing(self):  # noqa: VACUOUS_ASSERTION — the agreeing leg of the same arm asserts join and exec were called once
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        auth = self.plant_orca(cfg_email="someone-else@example.test")
        home_creds = os.path.join(home, ".credentials.json")
        argv = ["--no-install", "--seat", "alice", "--home", "seat-example-test"]
        rc, err, _seen, ex, join = self._launch(argv, home_creds)
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", err)
        self.assertIn("someone-else@example.test", err)
        self.assertIn(EMAIL, err)
        ex.assert_not_called()
        join.assert_not_called()
        os.unlink(os.path.join(auth, ".claude.json"))
        rc, err, _seen, ex, join = self._launch(argv, home_creds)
        self.assertEqual(rc, 0, err)
        ex.assert_called_once()
        join.assert_called_once()

    def test_sessions_resume_syncs_the_credhome_before_the_resume_script(self):  # noqa: VACUOUS_ASSERTION — every leg asserts the exact bytes the spy saw or the raise, positively
        """P2: the spawn_resume door. The spy on mint_resume_script records the
        bytes the home holds at the moment the resume is minted.
        CONTROL: HELM_CREDHOME_ORCA_SYNC=0 — the spy sees the stale bytes, so the
        equality is the sync. DISAGREE raises HarnessError with no script minted,
        naming the identity; a same-account CHAIN-UNPROVEN home raises with its
        own reason and never claims an identity mismatch (r2 F3).
        Mutation: delete the launch_sync call in sessions.spawn_resume and the
        synced leg sees the stale bytes."""
        from helm import harness, sessions
        auth = self.plant_orca()
        orca_bytes = self.read(os.path.join(auth, ".credentials.json"))
        row = {"i": "0a1b2c3d-resume", "h": "claude", "cwd": self.tmp}
        ad = mock.Mock()
        ad.name = "fake"
        ad.spawn.return_value = "pane-1"
        for leg in ("synced", "disabled", "disagree", "unproven"):
            with self.subTest(leg=leg):
                home = self.plant_home("seat-example-test", creds=_creds(
                    "FAKE-HOME", -10, 24 * 20 if leg == "unproven" else SPENT))
                home_creds = os.path.join(home, ".credentials.json")
                stale = self.read(home_creds)
                seen = []
                if leg == "disabled":
                    os.environ[cred.SYNC_ENV] = "0"
                cfg = os.path.join(auth, ".claude.json")
                if leg == "disagree":
                    with open(cfg, "w") as f:
                        json.dump({"oauthAccount": {"emailAddress": "x@example.test"}}, f)
                try:
                    with mock.patch.object(harness, "detect", return_value=ad), \
                            mock.patch.object(sessions, "mint_resume_script",
                                              side_effect=lambda r, **kw: seen.append(
                                                  self.read(home_creds)) or "/x/resume.sh"), \
                            contextlib.redirect_stderr(io.StringIO()):
                        if leg in ("disagree", "unproven"):
                            with self.assertRaises(harness.HarnessError) as raised:
                                sessions.spawn_resume(row, home=home)
                            said = str(raised.exception)
                            if leg == "unproven":
                                self.assertIn(cred.UNPROVEN, said)
                                self.assertNotIn("identity", said)
                            else:
                                self.assertIn("x@example.test", said)
                        else:
                            sessions.spawn_resume(row, home=home)
                finally:
                    os.environ.pop(cred.SYNC_ENV, None)
                    if os.path.exists(cfg):
                        os.unlink(cfg)
                if leg in ("disagree", "unproven"):
                    self.assertEqual(seen, [])
                    self.assertEqual(self.read(home_creds), stale)
                else:
                    self.assertEqual(seen, [orca_bytes if leg == "synced" else stale])

    def test_a_native_spawn_plan_says_what_the_panes_launch_will_do_with_the_home(self):  # noqa: VACUOUS_ASSERTION — each leg asserts its verdict and outcome clause positively; the unchanged bytes follow
        """P3: the pane's sync line prints only inside the pane, so the spawn
        plan says it. A home held by a live claude (the spawner on its own
        credhome) launches unsynced. CONTROL: the default home (no credhome)
        prints no token line. Mutation: drop the `token:` print in
        seat._spawn_native_plan and both credhome legs lose it."""
        from helm import seat
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        self.plant_orca()
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        for leg, held, clause in (("free", [], "syncs it from Orca before exec"),
                                  ("held", [(4242, "claude")], "will NOT sync it"),
                                  ("default", [], None)):
            with self.subTest(leg=leg):
                if leg != "default":
                    os.environ["CLAUDE_CONFIG_DIR"] = home
                out = io.StringIO()
                try:
                    with mock.patch.object(cred, "holders_of",
                                           lambda p, default=False: held), \
                            contextlib.redirect_stdout(out):
                        self.assertEqual(seat._spawn_native_plan(
                            "proj-claude", "proj", None, self.tmp, "worker"), 0)
                finally:
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                lines = [l for l in out.getvalue().splitlines() if "token:" in l]
                if clause is None:
                    self.assertIn("claude home:", out.getvalue())
                    self.assertEqual(lines, [])
                else:
                    self.assertEqual(len(lines), 1, out.getvalue())
                    self.assertIn(cred.STALE, lines[0])
                    self.assertIn(clause, lines[0])
                    if held:
                        self.assertIn("4242", lines[0])
                self.assertEqual(_state(home_creds), before)

    def test_an_unknown_store_launches_as_is_and_says_not_synced(self):  # noqa: VACUOUS_ASSERTION — rc 0 and the UNKNOWN / NOT synced text are presence checks on the same launch before the unchanged-bytes claim
        """No Orca store at all: UNKNOWN is said out loud, never passed as
        synced. CONTROL: the first launch arm syncs from a planted store."""
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", -10, SPENT))
        home_creds = os.path.join(home, ".credentials.json")
        before = _state(home_creds)
        rc, err, seen, ex, _join = self._launch(
            ["--no-install", "--seat", "alice", "--home", "seat-example-test"], home_creds)
        self.assertEqual(rc, 0, err)
        self.assertIn("UNKNOWN", err)
        self.assertIn("NOT synced", err)
        self.assertEqual(_state(home_creds), before)

    def test_model_is_carried_and_an_explicit_passthrough_wins(self):
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", 8))
        home_creds = os.path.join(home, ".credentials.json")
        base = ["--no-install", "--seat", "alice", "--home", "seat-example-test"]
        _rc, _err, seen, _ex, _join = self._launch(base + ["--model", "opus"], home_creds)
        self.assertEqual(seen["argv"], ["claude", "--model", "opus"])
        _rc, _err, seen, _ex, _join = self._launch(
            base + ["--model", "opus", "--", "--model", "sonnet"], home_creds)
        self.assertEqual(seen["argv"], ["claude", "--model", "sonnet"])

    def test_a_settings_default_model_is_named_and_never_written(self):  # noqa: VACUOUS_ASSERTION — the argv equality and the named model in stderr are presence checks on the same launch before the unchanged settings claim
        """CONTROL: with --model the settings line is absent and the argv
        carries the model (arm above); here the file's bytes and mtime are
        unchanged after the launch that names its value."""
        home = self.plant_home("seat-example-test", creds=_creds("FAKE-HOME", 8))
        settings = os.path.join(home, "settings.json")
        with open(settings, "w") as f:
            json.dump({"model": "sonnet"}, f)
        before = _state(settings)
        _rc, err, seen, _ex, _join = self._launch(
            ["--no-install", "--seat", "alice", "--home", "seat-example-test"],
            os.path.join(home, ".credentials.json"))
        self.assertEqual(seen["argv"], ["claude"])
        self.assertIn("'sonnet'", err)
        self.assertEqual(_state(settings), before)


if __name__ == "__main__":
    unittest.main()
