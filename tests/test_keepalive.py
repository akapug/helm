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
        j = lambda *p: os.path.join(self.tmp, *p)
        self._orig = {k: getattr(keepalive, k) for k in
                      ("LOG_PATH", "LOCK_PATH", "CLAUDE_HOMES_ROOT", "CODEX_HOMES_ROOT",
                       "_live_holder_pid", "_predecessor_pids")}
        keepalive.LOG_PATH = j("cache", "keepalive-log.jsonl")
        keepalive.LOCK_PATH = j("cache", "keepalive.lock")
        keepalive.CLAUDE_HOMES_ROOT = j("claude-homes")
        keepalive.CODEX_HOMES_ROOT = j("codex-homes")
        keepalive._live_holder_pid = lambda p: None
        keepalive._predecessor_pids = lambda: []
        # refresh_home's pre-image rides cred.backup — point it at tmp for
        # EVERY test, or fake-token snapshots land in the real ~/.cred-backups
        # (they did; cred's foreign-family coherence check caught the leak)
        self._envp = mock.patch.dict(os.environ, {
            "HELM_CRED_BACKUP_ROOT": j("cred-backups")})
        self._envp.start()

    def tearDown(self):
        self._envp.stop()
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
        with open(keepalive.LOG_PATH) as f:
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

    def test_default_dry_run_is_recursive_metadata_noop_and_makes_no_request(self):
        d = self._plant_home("nested/path/due-home", {
            "refreshToken": "FAKE-R", "expiresAt": 0})
        before = self._tree_state()
        with mock.patch("urllib.request.urlopen") as request:
            res = keepalive.refresh_home(d)
            self.assertEqual(res["action"], "would-refresh")
            request.assert_not_called()
        self.assertEqual(self._tree_state(), before)
        self.assertFalse(os.path.exists(keepalive.LOG_PATH))
        self.assertFalse(os.path.exists(keepalive.LOCK_PATH))

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
        with open(keepalive.LOG_PATH) as fh:
            log_text = fh.read()                               # token VALUES never logged
        for secret in ("NEW-A", "NEW-R", "OLD-R"):
            self.assertNotIn(secret, log_text)

    def test_log_names_the_ACCOUNT_and_a_pre_image_precedes_the_rotation(self):
        """The audit log's worst old bug: it recorded the DIR NAME, so a home
        holding another account logged `refreshed admin` about someone else's
        token. The account now rides every record, read from the home's own
        .claude.json (cred.py) — and the pre-image lands BEFORE the write."""
        d = self._plant_home("admin", {"refreshToken": "OLD-R", "expiresAt": 0})
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "owner@example.com"}}, f)
        backups = os.path.join(self.tmp, "cred-backups")
        resp = _FakeResp({"access_token": "NEW-A", "refresh_token": "NEW-R",
                          "expires_in": 3600})
        with mock.patch.dict(os.environ, {"HELM_CRED_BACKUP_ROOT": backups}), \
                mock.patch("urllib.request.urlopen", return_value=resp):
            res = keepalive.refresh_home(d, apply=True)
        self.assertEqual(res["home"], "admin")            # the label survives
        self.assertEqual(res["account"], "owner@example.com")   # …beside the TRUTH
        pre = os.path.join(res["pre_image"], "credentials.json")
        with open(pre) as fh:                              # the PRE-rotation bytes
            self.assertEqual(json.load(fh)["claudeAiOauth"]["refreshToken"], "OLD-R")
        self.assertEqual(stat.S_IMODE(os.stat(pre).st_mode), 0o600)
        with open(keepalive.LOG_PATH) as fh:
            self.assertNotIn("OLD-R", fh.read())           # still token-free

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

    def test_non_utf8_and_exception_messages_never_reach_output_or_log(self):
        sentinel = "sk-ant-oat01-" + "Q" * 96
        d = self._plant_home("secret-shaped-home", {"refreshToken": sentinel,
                                                     "expiresAt": 0})
        with mock.patch("urllib.request.urlopen", side_effect=RuntimeError(sentinel)):
            res = keepalive.refresh_home(d, apply=True)
        text = json.dumps(res) + open(keepalive.LOG_PATH).read()
        self.assertNotIn(sentinel, text)
        with open(os.path.join(d, ".credentials.json"), "wb") as f:
            f.write(b"\xff\xfe" + sentinel.encode())
        res = keepalive.refresh_home(d, apply=True)
        text = json.dumps(res) + open(keepalive.LOG_PATH).read()
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
            ("999", "python3 /home/x/sesh/server/keepalive.py --sweep")]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = keepalive.cmd_keepalive(["--apply"])
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", err.getvalue())
        self.assertIn("pid 999", err.getvalue())
        self.assertFalse(os.path.exists(keepalive.LOG_PATH))  # nothing ran

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
