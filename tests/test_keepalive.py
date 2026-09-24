"""Hermetic tests for helm.keepalive — home roots, log and lock all point at tmp
dirs; the OAuth endpoint is mocked; the live-holder probe is stubbed. No real
credential home is ever read or written."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from helm import keepalive


def _http_error(code):
    # a real fp: HTTPError(fp=None) mints an internal tempfile that only closes
    # at GC — a flaky ResourceWarning under the warning-clean gate
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b""))


FAKE_ID = "0f0f0f0f-1111-4222-8333-444444444444"
LOCAL_ID = "5a5a5a5a-5a5a-4a5a-8a5a-5a5a5a5a5a5a"
DESIGN_ID = "00000000-0000-4000-8000-000000000000"


def _cli_bytes(ids=(FAKE_ID,), local=LOCAL_ID, product="fake-cli",
               version="9.8.7"):
    """An executable shaped like the installed CLI's bundle where it matters:
    one config object per environment, each with a CLIENT_ID, a
    DESIGN_CLIENT_ID and an OAUTH_FILE_SUFFIX, and the user-agent template.
    The `--version` template beside it has the same head and must not read
    as a user agent. Every value is made up."""
    objs = b"".join(
        b'{MANUAL_REDIRECT_URL:"https://example.test/cb",CLIENT_ID:"%s",'
        b'DESIGN_CLIENT_ID:"%s",OAUTH_FILE_SUFFIX:"",X:!0}'
        % (cid.encode(), DESIGN_ID.encode()) for cid in ids)
    if local:
        objs += (b'{MANUAL_REDIRECT_URL:`${o}/cb`,CLIENT_ID:"%s",'
                 b'DESIGN_CLIENT_ID:"%s",OAUTH_FILE_SUFFIX:"-local-oauth",'
                 b'X:!0}' % (local.encode(), DESIGN_ID.encode()))
    ua = b""
    if product:
        ua = (b'function kI(){let s="";return`%s/${{ISSUES_EXPLAINER:"x",'
              b'VERSION:"%s",BUILD_TIME:"t"}.VERSION} (external, '
              b'${a.CLAUDE_CODE_ENTRYPOINT??"cli"}${e}${n}${s})`}'
              % (product.encode(), version.encode()))
    return (b"\x7fELF padding\0" + objs + ua
            + b'console.log(`${{VERSION:"1.0.0"}.VERSION} (Fake Code)`)')


def _write_cli(path, data=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(_cli_bytes() if data is None else data)
    return path


class _FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class KeepaliveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-keepalive-")
        self.cache_prior = os.environ.get("HELM_CACHE_DIR")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        j = lambda *p: os.path.join(self.tmp, *p)
        self._orig = {k: getattr(keepalive, k) for k in
                      ("CLAUDE_HOMES_ROOT", "CODEX_HOMES_ROOT",
                       "CLI_VERSIONS_DIR",
                       "_live_holder_pid", "_predecessor_pids")}
        keepalive.CLAUDE_HOMES_ROOT = j("claude-homes")
        keepalive.CODEX_HOMES_ROOT = j("codex-homes")
        keepalive._live_holder_pid = lambda p: None
        keepalive._predecessor_pids = lambda: []
        # refresh_home's pre-image rides cred.backup — point it at tmp for
        # EVERY test, or fake-token snapshots land in the real ~/.cred-backups
        # (they did; cred's foreign-family coherence check caught the leak)
        self._envp = mock.patch.dict(os.environ, {
            "HELM_CRED_BACKUP_ROOT": j("cred-backups"),
            # the Orca-chain guard reads Orca's store: never the real one
            "ORCA_USER_DATA_PATH": j("orca"),
            # the grant's client identity is read from an installed CLI:
            # a made-up one, never the real executable
            keepalive.CLI_ENV: _write_cli(j("cli", "cli-under-test"))})
        self._envp.start()
        keepalive.CLI_VERSIONS_DIR = j("no-versions-here")
        keepalive._identity_cache.clear()

    def tearDown(self):
        self._envp.stop()
        if self.cache_prior is None:
            os.environ.pop("HELM_CACHE_DIR", None)
        else:
            os.environ["HELM_CACHE_DIR"] = self.cache_prior
        for k, v in self._orig.items():
            setattr(keepalive, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------
    def _plant_home(self, name, oauth=None):
        d = os.path.join(keepalive.CLAUDE_HOMES_ROOT, name)
        os.makedirs(d, exist_ok=True)
        if oauth is not None:
            with open(os.path.join(d, ".credentials.json"), "w") as f:
                json.dump({"claudeAiOauth": oauth}, f)
            with open(os.path.join(d, ".claude.json"), "w") as f:
                json.dump({"oauthAccount": {
                    "emailAddress": name.replace("_", "-") + "@example.test"}}, f)
        return d

    def _log_lines(self):
        with open(keepalive._log_path()) as f:
            return [json.loads(l) for l in f]

    def _tree_state(self):
        out = []
        for root, dirs, files in os.walk(self.tmp):
            for name in sorted(dirs + files):
                p = os.path.join(root, name)
                st = os.lstat(p)
                digest = hashlib.sha256(open(p, "rb").read()).hexdigest() \
                    if stat.S_ISREG(st.st_mode) else None
                out.append((os.path.relpath(p, self.tmp), stat.S_IMODE(st.st_mode),
                            st.st_size, st.st_mtime_ns, digest))
        return out

    # -- the cadence's own record ------------------------------------------
    def test_every_logged_event_names_who_ran_the_pass(self):
        """A log that cannot say whether a loop or a person did the last
        refresh cannot answer whether the loop is turning, which is the
        question a due-refresh account puts to this file."""
        d = self._plant_home("who-home")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "keepalive-cron"}):
            keepalive.refresh_home(d, apply=True)
        self.assertEqual(self._log_lines()[-1]["by"], "keepalive-cron")
        # the control: an unnamed pass is a human at a keyboard, not a blank
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HELM_CHAT_NAME", None)
            os.environ.pop("MELD_CHAT_NAME", None)
            keepalive.refresh_home(d, apply=True)
        self.assertEqual(self._log_lines()[-1]["by"], "hand")

    def test_last_refresh_finds_the_newest_grant_for_one_home(self):
        path = keepalive._log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = [{"ts": "t1", "by": "hand", "home": "a", "action": "refreshed"},
                {"ts": "t2", "by": "keepalive-cron", "home": "b", "action": "refreshed"},
                {"ts": "t3", "by": "keepalive-cron", "home": "a", "action": "refreshed"},
                {"ts": "t4", "by": "keepalive-cron", "home": "a", "action": "skip"}]
        with open(path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        self.assertEqual(keepalive.last_refresh("a"),
                         {"ts": "t3", "home": "a", "by": "keepalive-cron"})
        self.assertEqual(keepalive.last_refresh("b")["ts"], "t2")
        self.assertIsNone(keepalive.last_refresh("never-refreshed"))
        # unfiltered: the newest grant anywhere
        self.assertEqual(keepalive.last_refresh()["ts"], "t3")

    def test_last_refresh_reads_only_the_tail_and_survives_a_cut_line(self):
        """An hourly cadence appends forever. The seek lands mid-line, so the
        partial first line must be dropped rather than parsed — and a grant
        that fell off the end of the window is reported as none rather than as
        a wrong timestamp."""
        path = keepalive._log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps({"ts": "old", "by": "hand", "home": "a",
                                "action": "refreshed"}) + "\n")
            f.write(json.dumps({"ts": "pad", "by": "hand", "home": "pad",
                                "action": "skip", "filler": "x" * 4000}) + "\n")
            f.write(json.dumps({"ts": "new", "by": "keepalive-cron", "home": "a",
                                "action": "refreshed"}) + "\n")
        self.assertEqual(keepalive.last_refresh("a", tail_bytes=1000)["ts"], "new")
        # the control on the same file: the whole file DOES see the old grant,
        # so the window above is what hid it and not a parse failure
        self.assertEqual(keepalive.last_refresh("a")["ts"], "new")
        os.truncate(path, 0)
        with open(path, "w") as f:
            f.write(json.dumps({"ts": "only", "by": "hand", "home": "a",
                                "action": "refreshed"}) + "\n")
            f.write(json.dumps({"ts": "pad", "by": "hand", "home": "pad",
                                "action": "skip", "filler": "x" * 4000}) + "\n")
        self.assertIsNone(keepalive.last_refresh("a", tail_bytes=1000))

    def test_last_refresh_on_a_missing_log_is_none_not_a_raise(self):
        """A status sentence may not be taken down by a log nobody has written
        yet — every box gets there before its first grant."""
        # THE POSITIVE CONTROL FIRST, on the same observable: this reader DOES
        # find a grant when one is on disk, so the None below is a statement
        # about the missing file and not about a reader that answers None to
        # everything.
        path = keepalive._log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps({"ts": "t1", "by": "hand", "home": "a",
                                "action": "refreshed"}) + "\n")
        self.assertEqual(keepalive.last_refresh("a")["ts"], "t1")
        os.unlink(path)
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(keepalive.last_refresh("a"))

    # -- refresh_home guards -----------------------------------------------
    def test_skip_when_no_credentials(self):
        d = self._plant_home("empty-home")
        res = keepalive.refresh_home(d, apply=True)
        self.assertEqual(res["action"], "skip")
        self.assertIn("no credentials file", res["reason"])
        self.assertEqual(self._log_lines()[-1]["action"], "skip")  # every outcome logged

    def test_skip_when_live_holder_never_writes(self):
        d = self._plant_home("busy-home", {"refreshToken": "SECRET-R", "expiresAt": 0})
        with open(os.path.join(d, ".credentials.json")) as fh:
            before = fh.read()
        keepalive._live_holder_pid = lambda p: "4242"
        res = keepalive.refresh_home(d, apply=True)
        self.assertEqual(res["action"], "skip")
        self.assertIn("live holder detected", res["reason"])
        with open(os.path.join(d, ".credentials.json")) as _f:
            self.assertEqual(_f.read(), before)

    def test_skip_when_not_due(self):
        far = int(time.time() * 1000) + 100 * 3600 * 1000
        d = self._plant_home("fresh-home", {"refreshToken": "r", "expiresAt": far})
        res = keepalive.refresh_home(d, apply=True)  # default horizon 60s
        self.assertEqual(res["action"], "skip")
        self.assertIn("not due", res["reason"])

    def test_needs_reauth_without_refresh_token(self):
        d = self._plant_home("dead-home", {"expiresAt": 0})
        self.assertEqual(keepalive.refresh_home(d, apply=True)["action"], "needs_reauth")

    def test_default_dry_run_is_recursive_metadata_noop_and_makes_no_request(self):  # noqa: VACUOUS_ASSERTION — would-refresh plus tree equality prove the dry-run path executed
        d = self._plant_home("nested/path/due-home", {
            "refreshToken": "FAKE-R", "expiresAt": 0})
        before = self._tree_state()
        with mock.patch("urllib.request.urlopen") as request:
            res = keepalive.refresh_home(d)
            self.assertEqual(res["action"], "would-refresh")
            request.assert_not_called()
        self.assertEqual(self._tree_state(), before)
        self.assertFalse(os.path.exists(keepalive._log_path()))
        self.assertFalse(os.path.exists(keepalive._cache_path("keepalive.lock")))

    def test_preimage_failure_refuses_before_rotating_grant(self):
        d = self._plant_home("due-home", {"refreshToken": "FAKE-R", "expiresAt": 0})
        before = open(os.path.join(d, ".credentials.json"), "rb").read()
        with mock.patch.object(keepalive.cred, "backup", return_value={
                "ok": False, "reason": "synthetic failure"}), \
                mock.patch("urllib.request.urlopen") as request:
            res = keepalive.refresh_home(d, apply=True)
        self.assertEqual(res["action"], "error")
        request.assert_not_called()
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(), before)

    # -- the refresh grant: rotation persisted, owner-only, token-free log --
    def test_refresh_persists_rotation_atomically(self):
        d = self._plant_home("due-home", {"refreshToken": "OLD-R", "accessToken": "OLD-A",
                                          "expiresAt": 0, "scopes": ["keepme"]})
        resp = _FakeResp({"access_token": "NEW-A", "refresh_token": "NEW-R",
                          "expires_in": 3600})
        with mock.patch("urllib.request.urlopen", return_value=resp) as uo:
            res = keepalive.refresh_home(d, apply=True)
        self.assertEqual(res["action"], "refreshed")
        self.assertTrue(res["rotated_refresh_token"])
        self.assertEqual(res["new_expiry_in_h"], 1.0)
        sent = json.loads(uo.call_args[0][0].data)
        self.assertEqual(sent["grant_type"], "refresh_token")
        cred_path = os.path.join(d, ".credentials.json")
        with open(cred_path) as fh:
            val = json.load(fh)
        oauth = val["claudeAiOauth"]
        self.assertEqual(oauth["accessToken"], "NEW-A")
        self.assertEqual(oauth["refreshToken"], "NEW-R")      # rotation persisted
        self.assertGreater(oauth["expiresAt"], time.time() * 1000)
        self.assertEqual(oauth["scopes"], ["keepme"])          # untouched fields survive
        self.assertEqual(stat.S_IMODE(os.stat(cred_path).st_mode), 0o600)  # owner-only
        with open(keepalive._log_path()) as fh:
            log_text = fh.read()                               # token VALUES never logged
        for secret in ("NEW-A", "NEW-R", "OLD-R"):
            self.assertNotIn(secret, log_text)

    def test_log_names_the_ACCOUNT_and_a_pre_image_precedes_the_rotation(self):
        """The audit log's worst old bug: it recorded the DIR NAME, so a home
        holding another account logged `refreshed ops-example` about someone else's
        token. The account now rides every record, read from the home's own
        .claude.json (cred.py) — and the pre-image lands BEFORE the write."""
        d = self._plant_home("ops-example", {"refreshToken": "OLD-R", "expiresAt": 0})
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "user@example.com"}}, f)
        backups = os.path.join(self.tmp, "cred-backups")
        resp = _FakeResp({"access_token": "NEW-A", "refresh_token": "NEW-R",
                          "expires_in": 3600})
        with mock.patch.dict(os.environ, {"HELM_CRED_BACKUP_ROOT": backups}), \
                mock.patch("urllib.request.urlopen", return_value=resp):
            res = keepalive.refresh_home(d, apply=True)
        self.assertEqual(res["home"], "ops-example")            # the label survives
        self.assertEqual(res["account"], "user@example.com")   # …beside the TRUTH
        pre = os.path.join(res["pre_image"], "credentials.json")
        with open(pre) as fh:                              # the PRE-rotation bytes
            self.assertEqual(json.load(fh)["claudeAiOauth"]["refreshToken"], "OLD-R")
        self.assertEqual(stat.S_IMODE(os.stat(pre).st_mode), 0o600)
        with open(keepalive._log_path()) as fh:
            self.assertNotIn("OLD-R", fh.read())           # still token-free

    def _plant_orca_copy(self, email, oauth, uuid="0a1b2c3d-0000-4000-8000-00000000beef"):
        """Orca's managed per-account layout (file names and key names read off
        the real store): claude-accounts/<uuid>/auth/{.credentials.json,
        oauth-account.json, .orca-managed-claude-auth naming <uuid>}."""
        auth = os.path.join(os.environ["ORCA_USER_DATA_PATH"], "claude-accounts",
                            uuid, "auth")
        os.makedirs(auth, exist_ok=True)
        with open(os.path.join(auth, "oauth-account.json"), "w") as f:
            json.dump({"emailAddress": email}, f)
        with open(os.path.join(auth, ".orca-managed-claude-auth"), "w") as f:
            f.write(uuid)
        with open(os.path.join(auth, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": oauth}, f)
        return auth

    def test_a_home_whose_chain_orca_holds_is_never_granted_on(self):  # noqa: VACUOUS_ASSERTION — the no-orca leg asserts refreshed and one request unconditionally
        """task/2505: after a launch syncs a home from Orca, the home and Orca
        hold ONE refresh token, and a keepalive grant would spend Orca's copy;
        a home stale against Orca may present a rotated-away token. Both skip,
        with no request and no byte written.

        CONTROL: the same due home with NO Orca copy is refreshed (request made,
        bytes rotated), so the skip is the Orca guard and not the fixture.
        Mutation: return None from `_orca_holds_the_chain` and both legs
        reach urlopen."""
        soon = int(time.time() * 1000) + 3600 * 1000
        later = soon + 6 * 3600 * 1000
        # one grant's refresh lifetime on both copies (126 ms of refresh
        # jitter): the home's token may be the one Orca rotated
        # away; and a home whose own refresh lifetime has passed is stale
        life = soon + 20 * 86400 * 1000
        past = int(time.time() * 1000) - 3600 * 1000
        resp = _FakeResp({"access_token": "NEW-A", "refresh_token": "NEW-R",
                          "expires_in": 3600})
        legs = (("same-family", {"refreshToken": "SHARED-R", "expiresAt": soon},
                 {"refreshToken": "SHARED-R", "expiresAt": soon}, "family"),
                ("unproven", {"refreshToken": "OLD-R", "expiresAt": soon,
                              "refreshTokenExpiresAt": life},
                 {"refreshToken": "ORCA-R", "expiresAt": later,
                  "refreshTokenExpiresAt": life + 126}, "cannot be told apart"),
                ("stale", {"refreshToken": "OLD-R", "expiresAt": soon,
                           "refreshTokenExpiresAt": past},
                 {"refreshToken": "ORCA-R", "expiresAt": later,
                  "refreshTokenExpiresAt": life}, "stale"))
        for label, home_oauth, orca_oauth, clause in legs:
            with self.subTest(leg=label):
                d = self._plant_home("chain-" + label, dict(home_oauth))
                self._plant_orca_copy("chain-%s@example.test" % label, orca_oauth,
                                      uuid="0a1b2c3d-0000-4000-8000-0000000%05d"
                                           % len(label))
                cred_path = os.path.join(d, ".credentials.json")
                with open(cred_path, "rb") as fh:
                    before = fh.read()
                with mock.patch("urllib.request.urlopen", return_value=resp) as uo:
                    res = keepalive.refresh_home(d, early_horizon_s=86400, apply=True)
                self.assertEqual(res["action"], "skip", res)
                self.assertIn(clause, res["reason"])
                # r3 P3: no skip reason promises a launch re-sync that a
                # CHAIN-UNPROVEN home refuses; the shared-family reason names the
                # operator's replacement instead
                self.assertNotIn("re-sync", res["reason"])
                if label == "same-family":
                    # the replacement is named only where sync-orca can act
                    self.assertIn("claude /login", res["reason"])
                    self.assertEqual("--replace-own-chain" in res["reason"],
                                     keepalive.cred.is_credhome(d))
                uo.assert_not_called()
                with open(cred_path, "rb") as fh:
                    self.assertEqual(fh.read(), before)
        d = self._plant_home("no-orca", {"refreshToken": "OWN-R", "expiresAt": soon})
        with mock.patch("urllib.request.urlopen", return_value=resp) as uo:
            res = keepalive.refresh_home(d, early_horizon_s=86400, apply=True)
        self.assertEqual(res["action"], "refreshed", res)
        uo.assert_called_once()

    def test_the_due_refresh_shape_never_outranks_a_skip_rule(self):  # noqa: VACUOUS_ASSERTION — the third leg is an UNCONDITIONAL POSITIVE CONTROL on both observables of the two silences: the same fixture shape with neither rule tripped asserts action==refreshed and uo3.assert_called_once(), so an urlopen that was never reachable would fail this arm rather than pass it
        """A home the quota page calls `due-refresh` is still only a home.

        The new state says the OWNER owes nothing; it says nothing about
        whether THIS process may write, and the two rules that decide that —
        one refresher per home, and Orca owns its own chain — are asked before
        anything about dueness. So the fixture here is the exact shape
        `providers.refresh_chain` reads as a live chain behind an expired
        access token, and each rule still stops it cold with no request and no
        byte moved.

        The arm goes red if either rule were consulted after a due check: a
        due home is precisely the input that would then be written."""
        from helm import providers
        past = int(time.time() * 1000) - 3600 * 1000
        life = int(time.time() * 1000) + 30 * 86400 * 1000
        due = {"refreshToken": "FAKE-R", "expiresAt": past,
               "refreshTokenExpiresAt": life}
        # the fixture IS the due-refresh shape, asserted rather than assumed
        self.assertEqual(providers.refresh_chain(due), providers.CHAIN_LIVE)
        resp = _FakeResp({"access_token": "NEW-A", "refresh_token": "NEW-R",
                          "expires_in": 3600})

        def unwritten(d):
            with open(os.path.join(d, ".credentials.json"), "rb") as fh:
                return fh.read()

        held = self._plant_home("due-but-held", dict(due))
        before = unwritten(held)
        keepalive._live_holder_pid = lambda p: "4242"
        with mock.patch("urllib.request.urlopen", return_value=resp) as uo:
            res = keepalive.refresh_home(held, early_horizon_s=86400, apply=True)
        self.assertEqual(res["action"], "skip", res)
        self.assertIn("live holder", res["reason"])
        uo.assert_not_called()
        self.assertEqual(unwritten(held), before)

        keepalive._live_holder_pid = lambda p: None
        orca = self._plant_home("due-but-orcas", dict(due))
        self._plant_orca_copy("due-but-orcas@example.test",
                              {"refreshToken": "FAKE-R", "expiresAt": past + 60_000,
                               "refreshTokenExpiresAt": life})
        before2 = unwritten(orca)
        with mock.patch("urllib.request.urlopen", return_value=resp) as uo2:
            res2 = keepalive.refresh_home(orca, early_horizon_s=86400, apply=True)
        self.assertEqual(res2["action"], "skip", res2)
        self.assertIn("family", res2["reason"])
        uo2.assert_not_called()
        self.assertEqual(unwritten(orca), before2)

        # THE UNCONDITIONAL POSITIVE CONTROL, same fixture shape, neither rule
        # tripped: the grant DOES happen, so the two silences above are the
        # rules and not a fixture keepalive never looks at.
        free = self._plant_home("due-and-free", dict(due))
        with mock.patch("urllib.request.urlopen", return_value=resp) as uo3:
            res3 = keepalive.refresh_home(free, early_horizon_s=86400, apply=True)
        self.assertEqual(res3["action"], "refreshed", res3)
        uo3.assert_called_once()

    def test_a_home_on_its_own_chain_behind_orca_is_still_rolled(self):
        """An independently logged-in home whose refresh lifetime is days away
        from Orca's is OWN-CHAIN, not stale: Orca's later access expiry says
        nothing about it, so keepalive rolls it as it always did.

        CONTROL: the `unproven` leg of the arm above is this fixture with the
        two lifetimes 126 ms apart, and it is skipped. Mutation: drop the one-grant
        window from orca._measure's chain test (classify any Orca-ahead home as
        STALE) and this home is skipped with no request."""
        soon = int(time.time() * 1000) + 3600 * 1000
        d = self._plant_home("own-chain", {
            "refreshToken": "OWN-R", "expiresAt": soon,
            "refreshTokenExpiresAt": soon + 3 * 86400 * 1000})
        self._plant_orca_copy("own-chain@example.test", {
            "refreshToken": "ORCA-R", "expiresAt": soon + 6 * 3600 * 1000,
            "refreshTokenExpiresAt": soon + 20 * 86400 * 1000})
        self.assertEqual(keepalive.cred.freshness(d)["verdict"], keepalive.cred.OWN_CHAIN)
        resp = _FakeResp({"access_token": "NEW-A", "refresh_token": "NEW-R",
                          "expires_in": 3600})
        with mock.patch("urllib.request.urlopen", return_value=resp) as uo:
            res = keepalive.refresh_home(d, early_horizon_s=86400, apply=True)
        self.assertEqual(res["action"], "refreshed", res)
        uo.assert_called_once()
        with open(os.path.join(d, ".credentials.json")) as fh:
            self.assertEqual(json.load(fh)["claudeAiOauth"]["refreshToken"], "NEW-R")

    def test_orca_taking_the_chain_after_the_pre_image_stops_the_grant(self):  # noqa: VACUOUS_ASSERTION — the two-call count and the post-pre-image skip reason are asserted positively before the no-request claim
        """The second `_orca_holds_the_chain` call closes the window in which a
        launch syncs the home from Orca between the plan and the grant.

        CONTROL: the no-Orca leg of the arm above, where both calls answer None,
        reaches urlopen. Here the first call answers None and the second a
        reason, so a skip can only come from the post-pre-image re-check.
        Mutation: delete that second call and urlopen is reached, the bytes
        rotate."""
        d = self._plant_home("race", {"refreshToken": "RACE-R", "expiresAt": 0})
        cred_path = os.path.join(d, ".credentials.json")
        with open(cred_path, "rb") as fh:
            before = fh.read()
        resp = _FakeResp({"access_token": "NEW-A", "refresh_token": "NEW-R",
                          "expires_in": 3600})
        with mock.patch.object(keepalive, "_orca_holds_the_chain",
                               side_effect=[None, "shares its chain (synced mid-run)"]) as oh, \
                mock.patch("urllib.request.urlopen", return_value=resp) as uo:
            res = keepalive.refresh_home(d, apply=True)
        self.assertEqual(oh.call_count, 2)
        self.assertEqual(res["action"], "skip", res)
        self.assertIn("measured after pre-image capture", res["reason"])
        uo.assert_not_called()
        with open(cred_path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_one_home_apply_takes_the_writer_lock_the_orca_sync_takes(self):  # noqa: VACUOUS_ASSERTION — the released leg asserts one request and the rotated refresh token unconditionally
        """`keepalive --home H --apply` and the Orca credhome sync never write
        one home at once: the lock is taken through the SYNC's own producer
        (cred.orca._keepalive_lock), and the one-home apply skips while it is
        held.

        CONTROL: the same command after the lock is released refreshes the home
        (request made, bytes rotated). Mutation: call refresh_home without
        `_writer_lock` in the --home branch and the held leg reaches urlopen."""
        from helm.cred import orca
        d = self._plant_home("locked", {"refreshToken": "LOCK-R", "expiresAt": 0})
        cred_path = os.path.join(d, ".credentials.json")
        with open(cred_path, "rb") as fh:
            before = fh.read()
        resp = _FakeResp({"access_token": "NEW-A", "refresh_token": "NEW-R",
                          "expires_in": 3600})
        lock = orca._keepalive_lock()
        self.assertIsNotNone(lock)
        try:
            out = io.StringIO()
            with mock.patch("urllib.request.urlopen", return_value=resp) as uo, \
                    contextlib.redirect_stdout(out):
                self.assertEqual(keepalive.cmd_keepalive(["--home", d, "--apply"]), 0)
            uo.assert_not_called()
            self.assertIn("credential-writer lock is held", out.getvalue())
            with open(cred_path, "rb") as fh:
                self.assertEqual(fh.read(), before)
        finally:
            lock.close()
        out = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=resp) as uo, \
                contextlib.redirect_stdout(out):
            self.assertEqual(keepalive.cmd_keepalive(["--home", d, "--apply"]), 0)
        uo.assert_called_once()
        with open(cred_path) as fh:
            self.assertEqual(json.load(fh)["claudeAiOauth"]["refreshToken"], "NEW-R")

    def test_refresh_http_error_means_reauth_and_no_write(self):
        d = self._plant_home("expired-home", {"refreshToken": "OLD-R", "expiresAt": 0})
        with open(os.path.join(d, ".credentials.json")) as fh:
            before = fh.read()
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(403)):
            res = keepalive.refresh_home(d, apply=True)
        self.assertEqual(res["action"], "needs_reauth")
        self.assertIn("HTTP 403", res["reason"])
        with open(os.path.join(d, ".credentials.json")) as _f:
            self.assertEqual(_f.read(), before)

    def test_non_utf8_and_exception_messages_never_reach_output_or_log(self):  # noqa: VACUOUS_ASSERTION — two real failure records render before secret absence is asserted
        sentinel = "sk-ant-oat01-" + "Q" * 96
        d = self._plant_home("secret-shaped-home", {"refreshToken": sentinel,
                                                     "expiresAt": 0})
        with mock.patch("urllib.request.urlopen", side_effect=RuntimeError(sentinel)):
            res = keepalive.refresh_home(d, apply=True)
        text = json.dumps(res) + open(keepalive._log_path()).read()
        self.assertNotIn(sentinel, text)
        with open(os.path.join(d, ".credentials.json"), "wb") as f:
            f.write(b"\xff\xfe" + sentinel.encode())
        res = keepalive.refresh_home(d, apply=True)
        text = json.dumps(res) + open(keepalive._log_path()).read()
        self.assertNotIn(sentinel, text)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(keepalive, "_predecessor_pids",
                               side_effect=RuntimeError(sentinel)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(keepalive.cmd_keepalive(["--apply"]), 1)
        self.assertNotIn(sentinel, out.getvalue() + err.getvalue())
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            keepalive.cmd_keepalive(["--home", sentinel])
        self.assertNotIn(sentinel, out.getvalue() + err.getvalue())

    # -- the client identity: the installed CLI's, read, never guessed -----
    def test_the_grant_names_the_installed_clis_identity(self):
        """The id in the body and the agent in the header are the fake CLI's
        own, down to the product word: nothing of either lives in helm."""
        d = self._plant_home("due-home", {"refreshToken": "R", "expiresAt": 0})
        resp = _FakeResp({"access_token": "A", "expires_in": 60})
        with mock.patch("urllib.request.urlopen", return_value=resp) as uo:
            res = keepalive.refresh_home(d, apply=True)
        self.assertEqual(res["action"], "refreshed")
        req = uo.call_args[0][0]
        self.assertEqual(json.loads(req.data)["client_id"], FAKE_ID)
        self.assertEqual(req.get_header("User-agent"),
                         "fake-cli/9.8.7 (external, cli)")

    def test_the_newest_installed_version_is_the_one_read(self):
        """By VERSION, not by spelling: 1.10.0 is newer than 1.9.9."""
        os.environ.pop(keepalive.CLI_ENV)
        vdir = os.path.join(self.tmp, "versions")
        keepalive.CLI_VERSIONS_DIR = vdir
        _write_cli(os.path.join(vdir, "1.9.9"), _cli_bytes(version="1.9.9"))
        _write_cli(os.path.join(vdir, "1.10.0"), _cli_bytes(version="1.10.0"))
        ident, why = keepalive.cli_identity()
        self.assertIsNone(why)
        self.assertEqual(ident["cli"], os.path.join(vdir, "1.10.0"))
        self.assertEqual(ident["user_agent"], "fake-cli/1.10.0 (external, cli)")

    def test_no_installed_cli_refuses_before_any_capture(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted positively (action, reason, path) and the same home's positive control ends the arm: with a CLI, the grant runs and urlopen is called once
        """FAIL CLOSED: no pre-image, no request, no write, and a reason that
        names where it looked."""
        os.environ.pop(keepalive.CLI_ENV)
        d = self._plant_home("due-home", {"refreshToken": "R", "expiresAt": 0})
        before = open(os.path.join(d, ".credentials.json"), "rb").read()
        with mock.patch.object(keepalive.cred, "backup") as backup, \
                mock.patch("urllib.request.urlopen") as request:
            res = keepalive.refresh_home(d, apply=True)
        self.assertEqual(res["action"], "error")
        self.assertIn("client identity could not be read", res["reason"])
        self.assertIn(keepalive.CLI_VERSIONS_DIR, res["reason"])
        backup.assert_not_called()
        request.assert_not_called()
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(),
                         before)
        # the positive control on the same home: with a CLI, the grant runs
        os.environ[keepalive.CLI_ENV] = _write_cli(
            os.path.join(self.tmp, "cli", "cli-under-test"))
        resp = _FakeResp({"access_token": "A", "expires_in": 60})
        with mock.patch("urllib.request.urlopen", return_value=resp) as uo:
            self.assertEqual(keepalive.refresh_home(d, apply=True)["action"],
                             "refreshed")
        uo.assert_called_once()

    def test_an_identity_that_is_absent_or_ambiguous_is_never_guessed(self):
        cases = {
            "two ids for the unsuffixed file":
                _cli_bytes(ids=(FAKE_ID, LOCAL_ID.replace("5", "6"))),
            "only a suffixed environment": _cli_bytes(ids=()),
            "no user-agent template": _cli_bytes(product=None),
            "an empty file": b"",
        }
        for label, data in cases.items():
            with self.subTest(label):
                path = _write_cli(os.path.join(self.tmp, "cli", label), data)
                os.environ[keepalive.CLI_ENV] = path
                ident, why = keepalive.cli_identity()
                self.assertIsNone(ident)
                self.assertIn(path, why)
        os.environ[keepalive.CLI_ENV] = os.path.join(self.tmp, "no-such-cli")
        self.assertIn("is not a file", keepalive.cli_identity()[1])

    def test_the_executable_is_read_once_while_it_is_unchanged(self):  # noqa: VACUOUS_ASSERTION — both reads positively return an identity (why is None), so the unreached re-read is the cache and not a dead path
        self.assertIsNone(keepalive.cli_identity()[1])
        with mock.patch.object(keepalive, "_client_ids") as reread:
            self.assertIsNone(keepalive.cli_identity()[1])
        reread.assert_not_called()

    # -- sweep: claude rolled forward, codex read-only stale-risk -----------
    def test_sweep_covers_claude_and_flags_stale_codex(self):
        self._plant_home("no-creds-home")
        cx = os.path.join(keepalive.CODEX_HOMES_ROOT, "old-codex")
        os.makedirs(cx)
        ap = os.path.join(cx, "auth.json")
        with open(ap, "w") as f:
            f.write("{}")
        old = time.time() - 30 * 3600
        os.utime(ap, (old, old))
        results = keepalive.sweep()
        actions = {(r.get("home"), r["action"]) for r in results}
        self.assertIn(("no-creds-home", "skip"), actions)
        self.assertIn(("old-codex", "stale-risk"), actions)
        stale = next(r for r in results if r["action"] == "stale-risk")
        self.assertEqual(stale["provider"], "codex")
        with open(ap) as _f:
            self.assertEqual(_f.read(), "{}")           # codex NEVER written

    # -- cmd leg: second-instance refusal + sweep smoke ---------------------
    def test_cmd_keepalive_refuses_beside_live_keepalive(self):
        keepalive._predecessor_pids = lambda: [
            ("999", "python3 /home/x/oldtool/server/keepalive.py --sweep")]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = keepalive.cmd_keepalive(["--apply"])
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", err.getvalue())
        self.assertIn("pid 999", err.getvalue())
        self.assertFalse(os.path.exists(keepalive._log_path()))  # nothing ran

    def test_cmd_keepalive_sweep_smoke(self):
        self._plant_home("no-creds-home")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = keepalive.cmd_keepalive([])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertTrue(text.startswith(
            "helm keepalive (dry-run — add --apply): 1 home checked, 0 refreshed"))
        self.assertIn('"skip"', text)

    def test_cmd_keepalive_one_home_and_bad_args(self):
        d = self._plant_home("solo-home")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = keepalive.cmd_keepalive(["--home", d, "--early", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("no credentials file", out.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(keepalive.cmd_keepalive(["--bogus"]), 2)
        self.assertIn("usage:", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class CadenceTest(unittest.TestCase):
    """The loop's own existence. keepalive is the ONE verb that refreshes
    helm's copy of an account's token, and while it was on no cadence it ran
    only when somebody typed it — so healthy accounts read on the quota page as
    accounts wanting a re-login. Nothing here writes a credential: the install
    path touches unit files under a temp HOME and every systemctl/crontab call
    is a stub."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-keepalive-cadence-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.envp = mock.patch.dict(os.environ, {"HOME": self.tmp})
        self.envp.start()
        self.addCleanup(self.envp.stop)

    @contextlib.contextmanager
    def _stubbed(self, crontab_out=""):
        """systemctl present and always succeeding; `crontab -l` answers with
        whatever the caller planted. A real systemctl here would enable a timer
        on the operator's own session.

        EVERY OTHER SPAWN IS DELEGATED TO THE REAL ONE, and that is the whole
        design of this helper rather than a nicety. `helm.keepalive.subprocess`
        IS the subprocess module, so patching `.run` through it replaces
        subprocess.run for the WHOLE PROCESS — and the installer reaches
        `work.find_root`, which spawns git. A blanket stub therefore answered
        the `git rev-parse --git-common-dir` behind `hooks._shared_root` with
        an empty stdout; that answer is MEMOISED per path for the life of the
        process, so every later module in the same run found helm unable to
        prove where its checkout is. 88 failures in two unrelated modules, from
        a fixture in this one.
        """
        calls = []
        real_run, real_which = subprocess.run, shutil.which

        def mine(cmd):
            return os.path.basename(str(cmd)) in ("systemctl", "crontab")

        def run(cmd, **kw):
            if not (cmd and mine(cmd[0])):
                return real_run(cmd, **kw)
            calls.append(list(cmd))
            out = crontab_out if "crontab" in cmd[0] else ""
            return mock.Mock(returncode=0, stdout=out, stderr="")

        def which(name, *a, **kw):
            return "/usr/bin/" + name if mine(name) else real_which(name, *a, **kw)
        with mock.patch("helm.keepalive.shutil.which", side_effect=which), \
                mock.patch("helm.keepalive.subprocess.run", side_effect=run):
            yield calls

    def test_the_unit_folds_a_lane_worktree_back_to_the_shared_checkout(self):
        """A persistent unit may never capture a DISPOSABLE WORKTREE — this
        arm runs from one, so a literal or a bare cwd would be right here in
        the text."""
        _spath, service, _tpath, timer = keepalive._timer_units()
        self.assertNotIn("-wt/", service)
        self.assertNotIn("-wt/", timer)
        # UNCONDITIONAL POSITIVE CONTROL: the unit is not empty, names a
        # WorkingDirectory, this verb, the stable install, and the identity
        # that makes every log row say who ran the pass.
        self.assertIn("WorkingDirectory=/", service)
        self.assertIn("keepalive --apply", service)
        self.assertIn(".local/bin/helm", service)
        self.assertIn("Environment=HELM_CHAT_NAME=keepalive-cron", service)
        self.assertIn("UnsetEnvironment=CLAUDE_CODE_SESSION_ID", service)

    def test_ensure_installs_then_verifies_with_no_change(self):
        with self._stubbed():
            ok, first = keepalive.ensure_timer()
            self.assertTrue(ok, first)
            unit = keepalive._timer_units()[2]
            self.assertTrue(os.path.exists(unit))
            self.assertTrue(keepalive.timer_installed())
            stamp = os.stat(unit).st_mtime_ns
            body = open(unit).read()
            ok2, second = keepalive.ensure_timer()
        self.assertIn("enabled", first)
        self.assertTrue(ok2, second)
        self.assertIn("already installed", second)
        self.assertIn("unchanged", second)
        self.assertEqual(open(unit).read(), body,
                         "a verify pass rewrote the unit's content")
        self.assertEqual(os.stat(unit).st_mtime_ns, stamp,
                         "a verify pass re-wrote the unit file")

    def test_ensure_reports_a_hand_crontab_line_and_never_edits_it(self):
        line = "23 * * * * HELM_CHAT_NAME=keepalive-cron helm keepalive --apply"
        with self._stubbed(crontab_out="# a comment\n%s\n" % line) as calls:
            ok, detail = keepalive.ensure_timer()
        self.assertTrue(ok, detail)
        self.assertIn("SUPERSEDED", detail)
        self.assertIn(line, detail)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the crontab
        # binary WAS invoked, read-only, so the empty write list below is a
        # reader that looked rather than a stub nobody called.
        read = [c for c in calls if "crontab" in c[0]]
        self.assertEqual(read, [["/usr/bin/crontab", "-l"]])
        wrote = [c for c in calls if "crontab" in c[0] and "-l" not in c]
        self.assertEqual(wrote, [], "the operator's crontab was written to")

    def test_a_commented_crontab_line_is_not_a_cadence(self):
        """The must-hit control on the same reader: a live line IS found, so
        the silence on the commented one is the filter working."""
        live = "0 * * * * helm keepalive --apply"
        with self._stubbed(crontab_out="%s\n" % live):
            self.assertEqual(keepalive.hand_crontab(), [live])
        with self._stubbed(crontab_out="#%s\n0 3 * * * something-else\n" % live):
            self.assertEqual(keepalive.hand_crontab(), [])

    def test_the_interval_is_refused_rather_than_silently_floored(self):
        ok, detail = keepalive.ensure_timer(0)
        self.assertFalse(ok)
        self.assertIn("at least 1 second", detail)

    def test_the_flag_reaches_the_installer_and_junk_does_not(self):
        """BUILT OWES WIRED, and the guard owes the mutating path: the
        installer writes the operator's systemd units."""
        with mock.patch.object(keepalive, "ensure_timer",
                               return_value=(True, "installed")) as p:
            rc = keepalive.cmd_keepalive(["--ensure-timer"])
        self.assertEqual(rc, 0)
        self.assertTrue(p.called, "the flag exists and reaches nothing")
        with mock.patch.object(keepalive, "ensure_timer") as p2:
            rc2 = keepalive.cmd_keepalive(["--bogus", "--ensure-timer"])
        self.assertEqual(rc2, 2)
        self.assertFalse(p2.called, "junk reached the unit installer")

    def test_ensure_timer_grants_nothing(self):
        """--ensure-timer is about the loop's EXISTENCE. Mixing a grant into
        an idempotent verify would make it a credential write."""
        with mock.patch.object(keepalive, "sweep") as swept, \
                mock.patch.object(keepalive, "refresh_home") as one, \
                mock.patch.object(keepalive, "ensure_timer",
                                  return_value=(True, "installed")):
            keepalive.cmd_keepalive(["--ensure-timer", "--apply"])
        self.assertFalse(swept.called)
        self.assertFalse(one.called)
        # the control on the same seams: without the flag, an applied run DOES
        # reach the sweep, so the silence above is the branch and not a dead
        # dispatcher
        with mock.patch.object(keepalive, "sweep", return_value=[]) as swept2, \
                mock.patch.object(keepalive, "_predecessor_pids", lambda: []):
            keepalive.cmd_keepalive(["--apply"])
        self.assertTrue(swept2.called)

    def test_help_carries_the_synopsis(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = keepalive.cmd_keepalive(["--help"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("usage: helm keepalive", text)   # unconditional
        for flag in ("--home", "--early", "--apply", "--ensure-timer"):
            self.assertIn(flag, text)

