"""Hermetic tests for helm.cred — a FAKE ~/.claude-homes estate in a tmpdir,
a tmp backup root, and a stubbed live-holder probe. The real credential homes
and the real ~/.cred-backups are never read or written, and every credential
byte in here is an obvious fake string ("FAKE-…"): no test fixture, output or
log line ever carries a real token."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import stat
import tempfile
import time
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
                # a live-looking expiry: real credential files always carry
                # one, and the unattended heal REFUSES a snapshot whose
                # freshness it cannot prove (expiry-unknown) — tests of that
                # refusal write their own credential file without it
                json.dump({"claudeAiOauth": {
                    "refreshToken": token, "accessToken": token + "-A",
                    "expiresAt": int((time.time() + 3600) * 1000)}}, f)
        cred.cache_clear()
        return d

    def out(self, fn, *a, **kw):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = fn(*a, **kw)
        return rc, buf.getvalue(), err.getvalue()

    def tree_state(self):
        """Recursive content + metadata state, including nested dirs/symlinks."""
        out = []
        for root, dirs, files in os.walk(self.tmp):
            for name in sorted(dirs + files):
                p = os.path.join(root, name)
                st = os.lstat(p)
                rel = os.path.relpath(p, self.tmp)
                digest = None
                if stat.S_ISREG(st.st_mode):
                    digest = hashlib.sha256(open(p, "rb").read()).hexdigest()
                elif stat.S_ISLNK(st.st_mode):
                    digest = os.readlink(p)
                out.append((rel, stat.S_IMODE(st.st_mode), st.st_size,
                            st.st_mtime_ns, digest))
        return out


class IdentityTest(CredBase):
    def test_identity_comes_from_content_not_the_dir_name(self):
        """THE law: the dir name is a label; .claude.json is the truth."""
        d = self.plant("admin-example-com", "owner@example.com")
        acct = cred.account_of(d)
        self.assertTrue(acct["ok"])
        self.assertEqual(acct["email"], "owner@example.com")
        self.assertEqual(acct["uuid"], "uuid-1")
        self.assertEqual(acct["org"], "Org")

    def test_drift_and_agreement_verdicts(self):
        self.plant("owner-example-com", "owner@example.com")     # name == folded email
        self.plant("admin-example-com", "owner@example.com")       # name lies
        by = {r["name"]: r for r in cred.rows()}
        self.assertEqual(by["owner-example-com"]["verdict"], "AGREE")
        self.assertEqual(by["admin-example-com"]["verdict"], "DRIFT")
        self.assertEqual(by["admin-example-com"]["account"], "owner@example.com")
        self.assertEqual(by["admin-example-com"]["wants_home"], "owner-example-com")

    def test_unreadable_identity_is_fail_closed_never_guessed(self):
        d = os.path.join(homes.ROOTS["claude"], "owner-example-com")
        os.makedirs(d)
        with open(os.path.join(d, ".credentials.json"), "w") as f:
            f.write("{}")
        row = {r["name"]: r for r in cred.rows()}["owner-example-com"]
        self.assertEqual(row["verdict"], "UNKNOWN")
        self.assertIsNone(row["account"])          # never "owner@example.com" from the name
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

    def test_identity_read_refuses_repeated_concurrent_replacement(self):
        d = self.plant("a-b-com", "a@b.com")
        original = cred._read_account
        n = [0]

        def racing(real):
            res = original(real)
            n[0] += 1
            with open(os.path.join(d, ".claude.json"), "w") as f:
                json.dump({"oauthAccount": {"emailAddress":
                          ("r" * n[0]) + "@example.test"}}, f)
            return res

        cred.cache_clear()
        with mock.patch.object(cred, "_read_account", side_effect=racing):
            got = cred.account_of(d)
        self.assertFalse(got["ok"])
        self.assertIn("changed during identity read", got["error"])

    def test_homes_row_identity_uses_the_same_reader(self):
        self.plant("admin-example-com", "owner@example.com")
        row = {r["name"]: r for r in homes.homes_list()}["admin-example-com"]
        self.assertEqual(row["identity"], "owner@example.com")
        self.assertFalse(row["canonical"])         # the pre-existing drift bit

    def test_content_identity_flows_to_usage_and_command_mint(self):
        from helm import transcripts
        from helm.providers import NativeQuotaProvider
        d = self.plant("admin-example-com", "OWNER@EXAMPLE.COM")
        p = NativeQuotaProvider(history_path=os.path.join(self.tmp, "usage.jsonl"))
        p.claude_root, p.codex_root = homes.ROOTS["claude"], homes.ROOTS["codex"]
        p._active_homes = lambda: set()
        expand = lambda x: ({"~/.claude": homes.DEFAULTS["claude"],
                             "~/.codex": homes.DEFAULTS["codex"]}.get(x, x))
        with mock.patch("helm.providers.os.path.expanduser", side_effect=expand):
            account = p.accounts()[0]
        self.assertEqual(account["name"], "owner@example.com")
        with mock.patch.object(p, "_get_json", return_value={"limits": [{
                "kind": "session", "percent": 12, "resets_at": None}]}):
            usage, _ = p._probe_one(account)
        self.assertEqual(usage["account"], "owner@example.com")
        mint = os.path.join(self.tmp, "mints.jsonl")
        with mock.patch.object(transcripts, "MINTS_PATH", mint), \
                mock.patch.object(transcripts, "_provider", return_value=p), \
                mock.patch("helm.providers.os.path.expanduser", side_effect=expand):
            transcripts._log_mint({"i": "sid", "h": "claude", "cwd": self.tmp},
                                  "owner@example.com", None)
        row = json.loads(open(mint).read())
        self.assertEqual((row["account"], row["home"]), ("owner@example.com", d))


class BackupTest(CredBase):
    def test_roundtrip_reproduces_bytes_and_identity(self):
        d = self.plant("owner-example-com", "owner@example.com")
        before = open(os.path.join(d, ".credentials.json"), "rb").read()
        res = cred.backup(d, apply=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "backup")
        self.assertEqual(res["account"], "owner@example.com")
        # the account dir is the FOLDED EMAIL, never the source dir name
        self.assertEqual(os.path.dirname(res["dest"]),
                         os.path.join(self.backups, "owner-example-com"))
        # clobber the home the way a /login would, then restore
        self.plant("owner-example-com", "someone@else.com", token="FAKE-OTHER")
        self.assertEqual(cred.account_of(d)["email"], "someone@else.com")
        r = cred.restore(res["dest"], d)
        self.assertTrue(r["ok"])
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(), before)
        self.assertEqual(cred.account_of(d)["email"], "owner@example.com")
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        for name in (".credentials.json", ".claude.json"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, name)).st_mode), 0o600)

    def test_restore_preserves_the_rest_of_claude_json(self):
        d = self.plant("owner-example-com", "owner@example.com")
        snap = cred.backup(d, apply=True)["dest"]
        doc = json.load(open(os.path.join(d, ".claude.json")))
        doc["oauthAccount"] = {"emailAddress": "someone@else.com"}
        doc["tipsHistory"] = {"keep": 1}
        json.dump(doc, open(os.path.join(d, ".claude.json"), "w"))
        cred.cache_clear()
        cred.restore(snap, d)
        got = json.load(open(os.path.join(d, ".claude.json")))
        self.assertEqual(got["oauthAccount"]["emailAddress"], "owner@example.com")
        self.assertEqual(got["tipsHistory"], {"keep": 1})   # untouched keys survive
        self.assertEqual(got["numStartups"], 3)

    def test_permissions_are_0600_files_inside_0700_dirs(self):
        d = self.plant("owner-example-com", "owner@example.com")
        dest = cred.backup(d, apply=True)["dest"]
        for name in ("credentials.json", "account.json", "meta.json"):
            mode = stat.S_IMODE(os.stat(os.path.join(dest, name)).st_mode)
            self.assertEqual(mode, 0o600, name)
        for p in (self.backups, os.path.dirname(dest), dest):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o700, p)

    def test_identical_snapshot_is_skipped(self):
        d = self.plant("owner-example-com", "owner@example.com")
        first = cred.backup(d, apply=True)
        again = cred.backup(d, apply=True)
        self.assertEqual(again["action"], "skip")
        self.assertEqual(again["dest"], first["dest"])
        self.assertEqual(len(cred.snapshots("owner@example.com")), 1)

    def test_changed_credentials_make_a_new_snapshot(self):
        d = self.plant("owner-example-com", "owner@example.com")
        cred.backup(d, apply=True)
        self.plant("owner-example-com", "owner@example.com", token="FAKE-ROTATED")
        self.assertEqual(cred.backup(d, apply=True)["action"], "backup")
        self.assertEqual(len(cred.snapshots("owner@example.com")), 2)

    def test_backup_fails_closed_on_unknown_identity(self):
        d = os.path.join(homes.ROOTS["claude"], "mystery")
        os.makedirs(d)
        with open(os.path.join(d, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"refreshToken": FAKE}}, f)
        res = cred.backup(d, apply=True)
        self.assertFalse(res["ok"])
        self.assertEqual(res["action"], "skip")
        self.assertFalse(os.path.isdir(os.path.join(self.backups, "mystery")))

    def test_prune_keeps_the_newest_and_never_the_only_one(self):
        d = self.plant("owner-example-com", "owner@example.com")
        for i in range(cred.KEEP + 3):
            self.plant("owner-example-com", "owner@example.com", token="FAKE-%d" % i)
            cred.backup(d, apply=True)
        snaps = cred.snapshots("owner@example.com")
        self.assertEqual(len(snaps), cred.KEEP)
        newest = json.load(open(os.path.join(snaps[-1]["path"], "credentials.json")))
        self.assertEqual(newest["claudeAiOauth"]["refreshToken"],
                         "FAKE-%d" % (cred.KEEP + 2))

    def test_fold_collisions_never_mix_counts_idempotence_or_pruning(self):
        plus = self.plant("plus-home", "a+b@example.com", token="FAKE-PLUS")
        dash = self.plant("dash-home", "a-b@example.com", token="FAKE-DASH")
        cred.backup(dash, apply=True)
        for i in range(cred.KEEP + 2):
            self.plant("plus-home", "a+b@example.com", token="FAKE-PLUS-%d" % i)
            cred.backup(plus, apply=True)
        self.assertEqual(len(cred.snapshots("a+b@example.com")), cred.KEEP)
        dash_snaps = cred.snapshots("a-b@example.com")
        self.assertEqual(len(dash_snaps), 1)
        self.assertEqual(json.load(open(os.path.join(
            dash_snaps[0]["path"], "credentials.json")))["claudeAiOauth"]["refreshToken"],
                         "FAKE-DASH")
        self.assertEqual(len(cred.snapshots_for_home_name("a-b-example-com")),
                         cred.KEEP + 1)

    def test_unwritable_root_is_reported_never_raised(self):
        """keepalive's rotation calls backup — a full/blocked disk must not
        raise across it; an unrotated token family is the worse outcome."""
        d = self.plant("owner-example-com", "owner@example.com")
        blocked = os.path.join(self.tmp, "not-a-dir")
        with open(blocked, "w") as f:
            f.write("")
        with mock.patch.dict(os.environ, {"HELM_CRED_BACKUP_ROOT": blocked}):
            res = cred.backup(d, apply=True)
        self.assertFalse(res["ok"])
        self.assertIn("snapshot write failed", res["reason"])

    def test_a_concurrent_backup_never_deletes_the_other_one(self):
        """One account can occupy two homes and the guard runs per turn, so two
        backups can land in the same account dir in the same SECOND. A shared
        snapshot name would make the loser's error path delete the winner's
        finished pre-image."""
        a = self.plant("owner-example-com", "owner@example.com", token="FAKE-A")
        b = self.plant("admin-example-com", "owner@example.com", token="FAKE-B")
        first = cred.backup(a, apply=True)
        # freeze the clock so both claims want the SAME timestamp name
        with mock.patch.object(cred.time, "strftime",
                               lambda *a_, **k: os.path.basename(first["dest"])):
            second = cred.backup(b, apply=True)
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(second["dest"], first["dest"])
        self.assertTrue(os.path.exists(os.path.join(first["dest"], "credentials.json")))
        snaps = cred.snapshots("owner@example.com")
        self.assertEqual(len(snaps), 2)
        self.assertEqual(snaps[-1]["path"], second["dest"])      # newest sorts last
        for s in snaps:
            self.assertEqual(stat.S_IMODE(os.stat(s["path"]).st_mode), 0o700)

    def test_backup_all_covers_every_authed_home(self):
        # distinct tokens: one family belongs to ONE account — two accounts
        # sharing bytes is the mixed-home shape capture now refuses
        self.plant("owner-example-com", "owner@example.com", token="FAKE-OWNER")
        self.plant("team-example-com", "team@example.com", token="FAKE-TEAM")
        self.plant("no-creds-com", "no@creds.com", token=None)
        done = {r["account"] for r in cred.backup_all(apply=True) if r["action"] == "backup"}
        self.assertEqual(done, {"owner@example.com", "team@example.com"})

    def test_every_mutating_cli_is_recursive_metadata_noop_without_apply(self):
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN")
        cred.backup(cto, apply=True)
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        self.plant("nested/path/home", "nested@example.test")
        before = self.tree_state()
        for argv in (["backup", "--all"], ["switch-guard", "--home", "admin-example-com"],
                     ["switch-guard", "--install"], ["heal"],
                     ["backup", "--home"]):
            self.out(cred.cmd_cred, argv)
            self.assertEqual(self.tree_state(), before, argv)


class RestoreAtomicityTest(CredBase):
    """A restore is ALL-OR-NOTHING across .credentials.json AND .claude.json.
    A home holding one account's tokens under another's identity block is the
    exact state this module exists to abolish — a half-written restore must
    never be able to mint it."""

    def staged(self, token="FAKE-OLD"):
        d = self.plant("owner-example-com", "owner@example.com", token=token)
        snap = cred.backup(d, apply=True)["dest"]
        self.plant("owner-example-com", "intruder@example.com", token="FAKE-INTRUDER")
        return d, snap

    def test_staging_failure_touches_neither_live_file(self):
        """Disk-full lands in staging — before any live byte moves."""
        d, snap = self.staged()
        creds = open(os.path.join(d, ".credentials.json"), "rb").read()
        cfg = open(os.path.join(d, ".claude.json"), "rb").read()
        real = cred._stage_private

        def flaky(path, data, mode=0o600):
            if path.endswith(".claude.json"):
                raise OSError(28, "No space left on device")
            return real(path, data, mode)

        with mock.patch.object(cred, "_stage_private", flaky):
            res = cred.restore(snap, d)
        self.assertFalse(res["ok"])
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(), creds)
        self.assertEqual(open(os.path.join(d, ".claude.json"), "rb").read(), cfg)
        cred.cache_clear()
        self.assertEqual(cred.account_of(d)["email"], "intruder@example.com")   # consistent
        self.assertFalse([f for f in os.listdir(d) if "helm-tmp" in f])

    def test_half_commit_rolls_the_credentials_file_back(self):
        """The second rename fails: the creds that already landed are undone."""
        d, snap = self.staged()
        creds = open(os.path.join(d, ".credentials.json"), "rb").read()
        real = os.replace

        def flaky(src, dst, *a, **kw):
            if str(dst).endswith(".claude.json"):
                raise OSError(5, "I/O error")
            return real(src, dst, *a, **kw)

        with mock.patch.object(cred.os, "replace", flaky):
            res = cred.restore(snap, d)
        self.assertFalse(res["ok"])
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(), creds)
        self.assertFalse([f for f in os.listdir(d) if "helm-tmp" in f])

    def test_restore_refuses_when_preimage_changes_after_staging(self):
        d, snap = self.staged()
        auth = os.path.join(d, ".credentials.json")
        cfg = open(os.path.join(d, ".claude.json"), "rb").read()
        real = cred._stage_private

        def racing(path, data, mode=0o600):
            tmp = real(path, data, mode)
            if path.endswith(".claude.json"):
                with open(auth, "wb") as f:
                    f.write(b"CONCURRENT-WRITER")
            return tmp

        with mock.patch.object(cred, "_stage_private", side_effect=racing):
            res = cred.restore(snap, d)
        self.assertFalse(res["ok"])
        self.assertIn("changed during restore", res["error"])
        self.assertEqual(open(auth, "rb").read(), b"CONCURRENT-WRITER")
        self.assertEqual(open(os.path.join(d, ".claude.json"), "rb").read(), cfg)

    def test_directory_mode_and_first_commit_failures_touch_nothing(self):
        d, snap = self.staged()
        def state():
            return (stat.S_IMODE(os.stat(d).st_mode),
                    tuple((name, open(os.path.join(d, name), "rb").read(),
                           stat.S_IMODE(os.stat(os.path.join(d, name)).st_mode))
                          for name in (".credentials.json", ".claude.json")),
                    tuple(sorted(n for n in os.listdir(d) if "helm-tmp" in n)))
        before = state()
        real_chmod = os.chmod

        def denied(path, mode, *a, **kw):
            if path == d:
                raise OSError(1, "synthetic")
            return real_chmod(path, mode, *a, **kw)

        with mock.patch.object(cred.os, "chmod", side_effect=denied):
            self.assertFalse(cred.restore(snap, d)["ok"])
        self.assertEqual(state(), before)

        real_replace = os.replace
        calls = [0]

        def first_fails(src, dst, *a, **kw):
            calls[0] += 1
            if calls[0] == 1:
                raise OSError(5, "synthetic")
            return real_replace(src, dst, *a, **kw)

        with mock.patch.object(cred.os, "replace", side_effect=first_fails):
            self.assertFalse(cred.restore(snap, d)["ok"])
        self.assertEqual(state(), before)

    def test_post_commit_fsync_failure_restores_exact_bytes_modes_and_absence(self):
        d, snap = self.staged()
        auth, cfg = (os.path.join(d, ".credentials.json"),
                     os.path.join(d, ".claude.json"))
        home_mode = stat.S_IMODE(os.stat(d).st_mode)
        old = {p: (open(p, "rb").read(), 0o640 + i) for i, p in enumerate((auth, cfg))}
        for p, (_, mode) in old.items():
            os.chmod(p, mode)
        real = cred._fsync_dir
        calls = [0]

        def flaky(path):
            calls[0] += 1
            if calls[0] == 1:
                raise OSError(5, "SENTINEL must not escape")
            return real(path)

        with mock.patch.object(cred, "_fsync_dir", flaky):
            res = cred.restore(snap, d)
        self.assertFalse(res["ok"])
        for p, (blob, mode) in old.items():
            self.assertEqual(open(p, "rb").read(), blob)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), mode)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), home_mode)

        empty = os.path.join(homes.ROOTS["claude"], "empty-target")
        os.makedirs(empty)
        empty_mode = stat.S_IMODE(os.stat(empty).st_mode)
        calls[0] = 0
        with mock.patch.object(cred, "_fsync_dir", flaky):
            res = cred.restore(snap, empty)
        self.assertFalse(res["ok"])
        self.assertFalse(os.path.lexists(os.path.join(empty, ".credentials.json")))
        self.assertFalse(os.path.lexists(os.path.join(empty, ".claude.json")))
        self.assertEqual(stat.S_IMODE(os.stat(empty).st_mode), empty_mode)

    def test_snapshot_identity_digest_and_length_must_self_consist(self):
        d, snap = self.staged()
        before = open(os.path.join(d, ".credentials.json"), "rb").read()
        meta = json.load(open(os.path.join(snap, "meta.json")))
        meta["account"] = "colliding+account@example.test"
        with open(os.path.join(snap, "meta.json"), "w") as f:
            json.dump(meta, f)
        res = cred.restore(snap, d)
        self.assertFalse(res["ok"])
        self.assertIn("metadata/content mismatch", res["error"])
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(), before)

    def test_symlink_inputs_are_refused_without_touching_targets(self):
        d, snap = self.staged()
        external = os.path.join(self.tmp, "external-secret")
        outside = b'{"claudeAiOauth":{"refreshToken":"FAKE-OUTSIDE"}}'
        with open(external, "wb") as f:
            f.write(outside)
        auth = os.path.join(d, ".credentials.json")
        os.unlink(auth)
        os.symlink(external, auth)
        res = cred.restore(snap, d)
        self.assertFalse(res["ok"])
        self.assertFalse(cred.backup(d, apply=True)["ok"])
        row = next(r for r in homes.homes_list() if r.get("path") == d)
        self.assertIsNone(row["family"])
        self.assertEqual(open(external, "rb").read(), outside)

    def test_unparseable_claude_json_is_refused_not_overwritten(self):
        """.claude.json holds the home's WHOLE state (projects, mcp, history).
        Rewriting it from {} would destroy that — refuse, like hooks.py does."""
        d, snap = self.staged()
        with open(os.path.join(d, ".claude.json"), "w") as f:
            f.write('{"projects": {"a": 1}, TRUNCATED')
        cred.cache_clear()
        res = cred.restore(snap, d)
        self.assertFalse(res["ok"])
        self.assertIn("refusing to overwrite", res["error"])
        self.assertEqual(open(os.path.join(d, ".claude.json")).read(),
                         '{"projects": {"a": 1}, TRUNCATED')

    def test_a_leftover_temp_file_cannot_block_a_restore_forever(self):
        """The temp name is ours by pid; a crashed run must not brick recovery."""
        d, snap = self.staged()
        with open(os.path.join(d, ".credentials.json.helm-tmp.%d" % os.getpid()),
                  "w") as f:
            f.write("leftover")
        res = cred.restore(snap, d)
        self.assertTrue(res["ok"], res)
        self.assertEqual(cred.account_of(d)["email"], "owner@example.com")


class HealTest(CredBase):
    def drifted(self):
        """admin-example-com's NAME promises admin@example.com; a /login left owner@example.com."""
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN")
        cred.backup(cto, apply=True)                                   # the guard ran first
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        return cto

    def test_dry_run_is_the_default_and_touches_nothing(self):
        d = self.drifted()
        before = open(os.path.join(d, ".credentials.json"), "rb").read()
        res = cred.heal()
        self.assertFalse(res["apply"])
        plan = res["plans"][0]
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["holds"], "owner@example.com")
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(), before)
        self.assertEqual(cred.account_of(d)["email"], "owner@example.com")

    def test_apply_restores_the_named_account_when_free(self):
        d = self.drifted()
        res = cred.heal(apply=True)
        plan = res["plans"][0]
        self.assertEqual(plan["status"], "restored")
        self.assertEqual(cred.account_of(d)["email"], "admin@example.com")
        creds = json.load(open(os.path.join(d, ".credentials.json")))
        self.assertEqual(creds["claudeAiOauth"]["refreshToken"], "FAKE-ADMIN")
        # the EVICTED occupant was snapshotted first — the undo is undoable
        self.assertTrue(plan["pre_image"])
        self.assertEqual(len(cred.snapshots("owner@example.com")), 1)

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
        seq = [[], [], [(77, "claude")]]       # arrives after pre-image capture
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
        self.assertEqual(cred.account_of(d)["email"], "owner@example.com")

    def test_no_backup_says_so_instead_of_inventing_a_restore(self):
        self.plant("admin-example-com", "owner@example.com")
        plan = cred.heal()["plans"][0]
        self.assertEqual(plan["status"], "no-backup")
        self.assertIsNone(plan["restore_from"])

    def test_refuses_to_mint_a_shared_token_family(self):
        """helm's oldest credential law: one home = one token family. If the
        snapshot's refresh token is STILL live in another home, restoring it
        here would byte-copy the family — reuse detection revokes both."""
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-SHARED")
        cred.backup(cto, apply=True)
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        self.plant("elsewhere-com", "admin@example.com", token="FAKE-SHARED")  # same bytes
        plan = cred.heal(apply=True)["plans"][0]
        self.assertEqual(plan["status"], "revocation-risk")
        self.assertIn("elsewhere-com", plan["reason"])
        self.assertIn("claude /login", plan["reason"])
        self.assertEqual(cred.account_of(cto)["email"], "owner@example.com")   # untouched

    def test_refuses_to_evict_an_occupant_whose_pre_image_failed(self):
        """The pre-image law ENFORCED, not merely attempted: if the occupant
        cannot be snapshotted (full/blocked disk), evicting it would delete the
        only copy of a live credential. Refuse."""
        d = self.drifted()
        occupant = open(os.path.join(d, ".credentials.json"), "rb").read()
        with mock.patch.object(cred, "backup", lambda p, apply=False: {
                "ok": False, "action": "skip", "account": "owner@example.com",
                "reason": "snapshot write failed (OSError)"}):
            res = cred.heal(apply=True)
        plan = res["plans"][0]
        self.assertEqual(plan["status"], "no-preimage")
        self.assertIn("exist nowhere", plan["reason"])
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(),
                         occupant)
        self.assertEqual(cred.account_of(d)["email"], "owner@example.com")

    def test_cli_exits_nonzero_when_a_heal_refused_mid_apply(self):
        self.drifted()
        with mock.patch.object(cred, "backup", lambda p, apply=False: {
                "ok": False, "action": "skip", "account": "owner@example.com",
                "reason": "snapshot write failed (OSError)"}):
            rc, out, _ = self.out(cred.cmd_cred, ["heal", "--apply"])
        self.assertEqual(rc, 1)
        self.assertIn("no-preimage", out)

    def test_agreeing_homes_are_never_planned(self):
        self.plant("owner-example-com", "owner@example.com")
        self.assertEqual(cred.heal()["plans"], [])

    def fake_proc(self, pid, env, comm="claude", start=777):
        root = os.path.join(self.tmp, "proc")
        p = os.path.join(root, str(pid))
        os.makedirs(p, exist_ok=True)
        fields = ["S"] + ["0"] * 18 + [str(start), "0"]
        with open(os.path.join(p, "stat"), "w") as f:
            f.write("%d (%s) %s" % (pid, comm, " ".join(fields)))
        with open(os.path.join(p, "comm"), "w") as f:
            f.write(comm)
        with open(os.path.join(p, "environ"), "wb") as f:
            f.write(env)
        return root, p

    def test_holders_probe_reads_content_and_brackets_pid_identity(self):
        self._holders.stop()
        try:
            d = self.plant("owner-example-com", "owner@example.com")
            root, _ = self.fake_proc(4242, b"CLAUDE_CONFIG_DIR=" + os.fsencode(d) + b"\0")
            with mock.patch.object(cred, "PROC_ROOT", root):
                self.assertEqual(cred.holders_of(d), [(4242, "claude")])
                with mock.patch.object(cred, "_proc_start", side_effect=[1, 2]):
                    self.assertIsNone(cred.holders_of(d))  # PID reuse is uncertainty
        finally:
            self._holders.start()

    def test_holders_permission_or_read_error_fails_closed(self):
        self._holders.stop()
        try:
            d = self.plant("owner-example-com", "owner@example.com")
            root, p = self.fake_proc(4242, b"")
            os.unlink(os.path.join(p, "environ"))
            os.mkdir(os.path.join(p, "environ"))       # read fails while pid still exists
            with mock.patch.object(cred, "PROC_ROOT", root):
                self.assertIsNone(cred.holders_of(d))
            os.rmdir(os.path.join(p, "environ"))
            with open(os.path.join(p, "environ"), "wb") as f:
                f.write(b"")
            real_open = open

            def denied(path, *a, **kw):
                if str(path).endswith("/environ"):
                    raise PermissionError("synthetic")
                return real_open(path, *a, **kw)

            with mock.patch.object(cred, "PROC_ROOT", root), \
                    mock.patch("builtins.open", side_effect=denied):
                self.assertIsNone(cred.holders_of(d))
        finally:
            self._holders.start()

    def foreign(self, pid):
        """Patch the uid seam so `pid` reads as another user's process, and
        deny every read under its /proc dir — the kernel's behavior for a
        foreign environ. create=True so the patch also applies to code that
        lacks the seam (the stash-verified pre-fix behavior)."""
        marker = os.sep + str(pid)
        me = os.geteuid()
        uidp = mock.patch.object(
            cred, "_proc_uid", create=True,
            new=lambda pdir: me + 1 if pdir.endswith(marker) else me)
        real_open = open

        def denied(path, *a, **kw):
            if marker + os.sep in str(path):
                raise PermissionError("synthetic foreign-uid environ")
            return real_open(path, *a, **kw)

        openp = mock.patch("builtins.open", side_effect=denied)
        return uidp, openp

    def test_foreign_uid_unreadable_pids_do_not_poison_the_scan(self):
        """The live-probed bug: on any real host, other users' pids raise
        EACCES on environ, and ONE such pid turned the whole scan into
        uncertainty — heal was a permanent no-op. A holder of our 0600
        credentials is necessarily our uid; foreign pids are structurally
        not holders, never uncertainty."""
        self._holders.stop()
        try:
            d = self.plant("owner-example-com", "owner@example.com")
            root, _ = self.fake_proc(4242, b"CLAUDE_CONFIG_DIR=" + os.fsencode(d) + b"\0")
            self.fake_proc(5555, b"CLAUDE_CONFIG_DIR=" + os.fsencode(d) + b"\0")
            uidp, openp = self.foreign(5555)
            with mock.patch.object(cred, "PROC_ROOT", root), uidp, openp:
                self.assertEqual(cred.holders_of(d), [(4242, "claude")])
        finally:
            self._holders.start()

    def test_same_uid_read_error_is_still_uncertainty(self):
        """Scoping to our uid must not soften the fail-closed core: a SAME-UID
        pid whose environ cannot be read while the pid persists stays None."""
        self._holders.stop()
        try:
            d = self.plant("owner-example-com", "owner@example.com")
            root, p = self.fake_proc(4242, b"")
            real_open = open

            def denied(path, *a, **kw):
                if str(path).endswith(os.path.join(str(4242), "environ")):
                    raise PermissionError("synthetic")
                return real_open(path, *a, **kw)

            with mock.patch.object(cred, "PROC_ROOT", root), \
                    mock.patch("builtins.open", side_effect=denied):
                self.assertIsNone(cred.holders_of(d))
        finally:
            self._holders.start()

    def test_pid_vanishing_mid_scan_is_absence_not_uncertainty(self):
        self._holders.stop()
        try:
            d = self.plant("owner-example-com", "owner@example.com")
            root, _ = self.fake_proc(4242, b"CLAUDE_CONFIG_DIR=" + os.fsencode(d) + b"\0")
            _, p9 = self.fake_proc(9999, b"")
            real_open = open

            def vanishing(path, *a, **kw):
                if (os.sep + "9999" + os.sep) in str(path):
                    shutil.rmtree(p9, ignore_errors=True)
                    raise FileNotFoundError(path)
                return real_open(path, *a, **kw)

            with mock.patch.object(cred, "PROC_ROOT", root), \
                    mock.patch("builtins.open", side_effect=vanishing):
                self.assertEqual(cred.holders_of(d), [(4242, "claude")])
        finally:
            self._holders.start()

    def deny_environ(self, *pids):
        """The kernel's shape for a ptrace-protected same-uid pid: environ is
        EACCES while comm (0444, world-readable even for non-dumpable
        processes) still answers."""
        real_open = open
        markers = tuple(os.sep + str(p) + os.sep + "environ" for p in pids)
        def denied(path, *a, **kw):
            if str(path).endswith(markers):
                raise PermissionError("synthetic ptrace-protected environ")
            return real_open(path, *a, **kw)
        return mock.patch("builtins.open", side_effect=denied)

    def test_protected_non_claude_comm_is_structurally_not_a_holder(self):
        """The live residue the uid-scoping fix surfaced: ~239 same-uid pids
        (systemd --user, git helpers, ssh-agent, sandbox children) hold EACCES
        environs FOREVER, keeping heal at cannot-probe for good. Their
        world-readable comm proves they are not claude-harness processes — a
        claude session cannot wear `systemd`'s comm — so they are structurally
        not holders, never uncertainty."""
        self._holders.stop()
        try:
            d = self.plant("owner-example-com", "owner@example.com")
            root, _ = self.fake_proc(4242, b"CLAUDE_CONFIG_DIR=" + os.fsencode(d) + b"\0")
            for pid, comm in ((9001, "systemd"), (9002, "git"), (9003, "ssh-agent")):
                self.fake_proc(pid, b"", comm=comm)
            with mock.patch.object(cred, "PROC_ROOT", root), \
                    self.deny_environ(9001, 9002, 9003):
                self.assertEqual(cred.holders_of(d), [(4242, "claude")])
        finally:
            self._holders.start()

    def test_protected_claude_family_comm_stays_uncertainty(self):
        """The pin's other half: a non-dumpable pid that COULD be a claude
        host (comm `claude`, or the node comms a claude estate actually shows)
        keeps the scan at None — fail closed where a live session could be
        evicted."""
        self._holders.stop()
        try:
            d = self.plant("owner-example-com", "owner@example.com")
            for pid, comm in ((9001, "claude"), (9002, "node"),
                              (9003, "node-MainThread"), (9004, "claude-code")):
                root, p = self.fake_proc(pid, b"", comm=comm)
                with mock.patch.object(cred, "PROC_ROOT", root), self.deny_environ(pid):
                    self.assertIsNone(cred.holders_of(d), comm)
                shutil.rmtree(p)
        finally:
            self._holders.start()

    def test_protected_pid_with_unreadable_comm_stays_uncertainty(self):
        self._holders.stop()
        try:
            d = self.plant("owner-example-com", "owner@example.com")
            root, _ = self.fake_proc(9001, b"", comm="systemd")
            real_open = open
            suffixes = tuple(os.path.join("9001", f) for f in ("environ", "comm"))
            def denied(path, *a, **kw):
                if str(path).endswith(suffixes):
                    raise PermissionError("synthetic")
                return real_open(path, *a, **kw)
            with mock.patch.object(cred, "PROC_ROOT", root), \
                    mock.patch("builtins.open", side_effect=denied):
                self.assertIsNone(cred.holders_of(d))
        finally:
            self._holders.start()

    def test_heal_reaches_ready_through_ptrace_protected_system_pids(self):
        """End-to-end pin of the policy on the live-box shape: with only
        protected NON-claude pids in the table, a free drifted home plans
        `ready` instead of the forever cannot-probe it planned before."""
        self._holders.stop()
        try:
            self.drifted()
            root, _ = self.fake_proc(9001, b"", comm="systemd")
            self.fake_proc(9002, b"", comm="git")
            with mock.patch.object(cred, "PROC_ROOT", root), \
                    self.deny_environ(9001, 9002):
                plan = cred.heal()["plans"][0]
            self.assertEqual(plan["status"], "ready", plan)
            self.assertEqual(plan["holders"], [])
        finally:
            self._holders.start()

    def test_heal_dry_run_reaches_ready_through_the_real_probe(self):
        """End-to-end pin of the bug: a free drifted home with a snapshot must
        plan `ready` even though the process table holds foreign-uid pids —
        before the fix this was cannot-probe on every real multi-user host."""
        self._holders.stop()
        try:
            d = self.drifted()
            root, _ = self.fake_proc(5555, b"CLAUDE_CONFIG_DIR=" + os.fsencode(d) + b"\0")
            uidp, openp = self.foreign(5555)
            with mock.patch.object(cred, "PROC_ROOT", root), uidp, openp:
                res = cred.heal()
            plan = res["plans"][0]
            self.assertEqual(plan["status"], "ready", plan)
            self.assertEqual(plan["holders"], [])
        finally:
            self._holders.start()

    def test_cannot_probe_reason_matches_the_actual_platform(self):
        """`no /proc` was reported even where /proc was right there. The two
        distinct uncertainties get two distinct reasons."""
        self.drifted()
        with mock.patch.object(cred, "holders_of", lambda p, default=False: None):
            with mock.patch.object(cred, "PROC_ROOT",
                                   os.path.join(self.tmp, "no-such-proc")):
                plan = cred.heal()["plans"][0]
                self.assertEqual(plan["status"], "cannot-probe")
                self.assertIn("no /proc on this platform", plan["reason"])
            with mock.patch.object(cred, "PROC_ROOT", self.tmp):
                plan = cred.heal()["plans"][0]
                self.assertEqual(plan["status"], "cannot-probe")
                self.assertIn("same-uid", plan["reason"])
                self.assertNotIn("no /proc", plan["reason"])


class DoctorTest(CredBase):
    def test_drift_row_names_the_account_and_the_verb(self):
        """With a snapshot of the NAMED account, the row names the repair verb."""
        cto = self.plant("admin-example-com", "admin@example.com")
        cred.backup(cto, apply=True)
        self.plant("admin-example-com", "owner@example.com")
        msgs = [m for lvl, m in cred.doctor_rows() if lvl == "WARN"]
        self.assertTrue(any("credhome admin-example-com HOLDS owner@example.com (drift" in m
                            for m in msgs), msgs)
        self.assertTrue(any("`helm cred heal` restores admin-example-com from its 1 "
                            "snapshot" in m for m in msgs), msgs)

    def test_drift_row_is_honest_when_nothing_was_snapshotted(self):
        """Naming `helm cred heal` here would be a lie — it cannot restore an
        account that was never snapshotted."""
        self.plant("admin-example-com", "owner@example.com")
        msgs = [m for lvl, m in cred.doctor_rows() if lvl == "WARN"]
        self.assertTrue(any("NOTHING was snapshotted for admin-example-com" in m
                            for m in msgs), msgs)
        self.assertFalse(any("`helm cred heal` restores" in m for m in msgs), msgs)

    def test_list_tells_the_owner_the_evicted_account_is_recoverable(self):
        """At the moment of the incident the BACKUPS column counts the ARRIVING
        account (0) — the owner must still see that the EVICTED one is safe."""
        cto = self.plant("admin-example-com", "admin@example.com")
        cred.backup(cto, apply=True)
        self.plant("admin-example-com", "owner@example.com")
        row = {r["name"]: r for r in cred.rows()}["admin-example-com"]
        self.assertEqual((row["backups"], row["named_backups"]), (0, 1))
        _, out, _ = self.out(cred.cmd_cred, ["list"])
        self.assertIn("heal can restore admin-example-com (1 snapshot)", out)
        self.plant("nobackup-example-invalid", "owner@example.com")
        _, out, _ = self.out(cred.cmd_cred, ["list"])
        self.assertIn("NO snapshot of nobackup-example-invalid", out)

    def test_missing_backup_row(self):
        self.plant("owner-example-com", "owner@example.com")
        msgs = [m for lvl, m in cred.doctor_rows() if lvl == "WARN"]
        self.assertTrue(any("no cred backup for owner@example.com" in m for m in msgs), msgs)

    def test_clean_estate_is_one_ok_row(self):
        d = self.plant("owner-example-com", "owner@example.com")
        cred.backup(d, apply=True)
        rows = cred.doctor_rows()
        self.assertEqual([lvl for lvl, _ in rows], ["OK"])
        self.assertIn("every dir name matches", rows[0][1])

    def test_doctor_check_is_wired_into_the_report(self):
        self.assertIn("check_cred_drift", doctor.CHECKS)
        self.plant("admin-example-com", "owner@example.com")
        rows = doctor.check_cred_drift()
        self.assertTrue(any("drift" in m for _, m in rows))


class CliTest(CredBase):
    def test_list_shows_dir_name_actual_account_and_verdict(self):
        self.plant("admin-example-com", "owner@example.com")
        self.plant("team-example-com", "team@example.com")
        rc, out, _ = self.out(cred.cmd_cred, ["list"])
        self.assertEqual(rc, 0)
        self.assertIn("DIR NAME", out)
        self.assertIn("ACTUAL ACCOUNT", out)
        self.assertIn("admin-example-com", out)
        self.assertIn("owner@example.com", out)
        self.assertIn("DRIFT", out)
        self.assertIn("AGREE", out)
        self.assertIn("1 drift", out)

    def test_bare_cred_is_list(self):
        self.plant("owner-example-com", "owner@example.com")
        rc, out, _ = self.out(cred.cmd_cred, [])
        self.assertEqual(rc, 0)
        self.assertIn("ACTUAL ACCOUNT", out)

    def test_list_json(self):
        self.plant("admin-example-com", "owner@example.com")
        rc, out, _ = self.out(cred.cmd_cred, ["list", "--json"])
        rows = json.loads(out)
        self.assertEqual(rows[0]["verdict"], "DRIFT")

    def test_backup_all_and_switch_guard_report_the_account(self):
        self.plant("owner-example-com", "owner@example.com")
        rc, out, _ = self.out(cred.cmd_cred, ["backup", "--all", "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("owner@example.com", out)
        rc, out, _ = self.out(cred.cmd_cred, ["switch-guard", "--home", "owner-example-com", "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("protected", out)
        self.assertIn("claude /login", out)      # the exact command, human-run

    def test_switch_guard_quiet_backup_prints_nothing(self):
        """SessionStart hook mode: stdout becomes session context — stay silent."""
        d = self.plant("owner-example-com", "owner@example.com")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            rc, out, err = self.out(cred.cmd_cred, ["backup", "--apply", "--quiet"])
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(len(cred.snapshots("owner@example.com")), 1)

    def test_switch_guard_reports_when_it_cannot_protect(self):
        d = os.path.join(homes.ROOTS["claude"], "mystery")
        os.makedirs(d)
        rc, out, err = self.out(cred.cmd_cred, ["switch-guard", "--home", "mystery"])
        self.assertEqual(rc, 1)
        self.assertIn("NOT protected", err)

    def test_heal_cli_is_dry_run_by_default(self):
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN")
        cred.backup(cto, apply=True)
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        rc, out, _ = self.out(cred.cmd_cred, ["heal"])
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", out)
        self.assertEqual(cred.account_of(cto)["email"], "owner@example.com")
        rc, out, _ = self.out(cred.cmd_cred, ["heal", "--apply"])
        self.assertIn("APPLIED", out)
        self.assertEqual(cred.account_of(cto)["email"], "admin@example.com")

    def test_verb_and_help_wired_into_the_dispatcher(self):
        from helm import cli
        self.assertIn("cred", cli.VERBS)
        self.assertIn("cred", cli._VERB_HELP)
        self.plant("owner-example-com", "owner@example.com")
        rc, out, _ = self.out(cli.VERBS["cred"], ["list"])
        self.assertEqual(rc, 0)
        self.assertIn("owner@example.com", out)

    def test_unknown_subverb_is_usage(self):
        rc, _, err = self.out(cred.cmd_cred, ["bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown subverb", err)


class GuardFreshnessTest(CredBase):
    """A pre-image is only worth the token it still holds. A live session
    refreshes its own credentials and the grant ROTATES the refresh token, so a
    SessionStart-only guard goes dead hours before the `/login` it exists for."""

    def test_the_guard_rides_the_turn_boundary_as_well_as_session_start(self):
        events = {s["event"] for s in cred.GUARD_SPECS}
        self.assertEqual(events, {"SessionStart", "Stop"})
        self.assertEqual(len({s["name"] for s in cred.GUARD_SPECS}), 4)
        for s in cred.GUARD_SPECS:                    # hooks.py spec contract
            self.assertIn(s["args"], ("cred backup --apply --quiet",
                                      "cred heal --apply --quiet"))
            self.assertIn("timeout", s)
            # Stop takes no matcher (hooks.SPECS' law); SessionStart takes *
            self.assertEqual(s["matcher"], {"Stop": None}.get(s["event"], "*"))
        for verb in ("backup", "heal"):               # each leg rides BOTH events
            self.assertEqual({s["event"] for s in cred.GUARD_SPECS
                              if verb in s["args"]}, {"SessionStart", "Stop"})

    def test_backup_precedes_heal_structurally(self):
        """backup-before-heal is BY CONSTRUCTION: every backup spec sorts
        before every heal spec, so _merge_event's append order puts the
        snapshot hook ahead of the heal hook in each event's list."""
        idx = {v: [i for i, s in enumerate(cred.GUARD_SPECS) if v in s["args"]]
               for v in ("backup", "heal")}
        self.assertTrue(idx["backup"] and idx["heal"])
        self.assertLess(max(idx["backup"]), min(idx["heal"]))
        self.assertEqual(cred.GUARD_SPECS, cred._BACKUP_GUARDS + cred._HEAL_GUARDS)

    def install_estate(self, d):
        from helm import configs
        orig = (configs.HOME_ROOTS, configs.BACKUP_DIR)
        configs.HOME_ROOTS = [d]                      # the fake estate's gate
        configs.BACKUP_DIR = os.path.join(self.tmp, "config-backups")
        self.addCleanup(lambda: setattr(configs, "HOME_ROOTS", orig[0]))
        self.addCleanup(lambda: setattr(configs, "BACKUP_DIR", orig[1]))
        return self.out(cred.cmd_cred, ["switch-guard", "--install", "--apply"])

    def test_install_writes_both_events_into_every_home(self):
        d = self.plant("owner-example-com", "owner@example.com")
        rc, out, err = self.install_estate(d)
        self.assertEqual(rc, 0, err)
        sp = os.path.join(homes.ROOTS["claude"], "owner-example-com", "settings.json")
        cfg = json.load(open(sp))
        for event in ("SessionStart", "Stop"):
            cmds = [h["command"] for g in cfg["hooks"][event] for h in g["hooks"]]
            for verb in ("cred backup --apply --quiet", "cred heal --apply --quiet"):
                self.assertTrue(any(verb in c for c in cmds), (event, verb))
            # ordering survives into the settings file itself
            self.assertLess(next(i for i, c in enumerate(cmds) if "cred backup" in c),
                            next(i for i, c in enumerate(cmds) if "cred heal" in c),
                            event)
        # idempotent
        rc, out, _ = self.out(cred.cmd_cred, ["switch-guard", "--install", "--apply"])
        self.assertEqual(json.load(open(sp)), cfg)

    def test_a_spent_looking_snapshot_warns_before_apply(self):
        """expiresAt in the past ⇒ the home refreshed after the snapshot ⇒ its
        refresh token may already be consumed. Surfaced, not hidden."""
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN")
        with open(os.path.join(cto, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"refreshToken": "FAKE-ADMIN",
                                         "expiresAt": 1}}, f)          # long dead
        cred.cache_clear()
        cred.backup(cto, apply=True)
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        plan = cred.heal()["plans"][0]
        self.assertEqual(plan["status"], "ready")
        self.assertTrue(plan["stale_pre_image"])
        self.assertIn("reuse detection", plan["reason"])
        self.assertIn("claude /login", plan["reason"])
        rc, out, _ = self.out(cred.cmd_cred, ["heal"])
        self.assertIn("WARNING", out)

    def test_a_live_snapshot_carries_no_warning(self):
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN")
        with open(os.path.join(cto, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"refreshToken": "FAKE-ADMIN",
                                         "expiresAt": (time.time() + 3600) * 1000}}, f)
        cred.cache_clear()
        cred.backup(cto, apply=True)
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        plan = cred.heal()["plans"][0]
        self.assertFalse(plan["stale_pre_image"])
        self.assertNotIn("WARNING", plan["reason"])


class GuardHealTest(CredBase):
    """The guard's heal leg, run exactly as the installed hook runs it
    (`cred heal --apply --quiet`) — the admin incident's replay: a home whose
    credential was overwritten while a snapshot of the rightful account
    exists."""

    def incident(self):
        """admin-example-com held admin@example.com, the guard's backup leg snapshotted it,
        then a /login overwrote the home with owner@example.com."""
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN")
        cred.backup(cto, apply=True)
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        return cto

    def test_guard_command_restores_a_holder_free_home_silently(self):
        d = self.incident()
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(cred.account_of(d)["email"], "admin@example.com")
        creds = json.load(open(os.path.join(d, ".credentials.json")))
        self.assertEqual(creds["claudeAiOauth"]["refreshToken"], "FAKE-ADMIN")

    def test_guard_command_never_evicts_a_live_borrower(self):
        d = self.incident()
        before = open(os.path.join(d, ".credentials.json"), "rb").read()
        with mock.patch.object(cred, "holders_of",
                               lambda p, default=False: [(4242, "claude")]):
            rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((out, err), ("", ""))   # silent even on refusal
        self.assertEqual(rc, 1)                  # honest exit; the hook's || true absorbs it
        self.assertEqual(open(os.path.join(d, ".credentials.json"), "rb").read(),
                         before)
        self.assertEqual(cred.account_of(d)["email"], "owner@example.com")

    def test_guard_heal_snapshots_the_overwritten_credential_before_restore(self):
        """Nothing is ever lost in either direction: the occupant heal evicts
        is itself snapshotted BEFORE the restore commits."""
        d = self.incident()
        self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        owner = cred.snapshots("owner@example.com")
        self.assertEqual(len(owner), 1)
        blob = json.load(open(os.path.join(owner[-1]["path"], "credentials.json")))
        self.assertEqual(blob["claudeAiOauth"]["refreshToken"], "FAKE-OWNER")
        self.assertEqual(cred.account_of(d)["email"], "admin@example.com")

    def test_quiet_no_op_prints_nothing_and_exits_zero(self):
        """Hook law: silence on no-op — a clean estate injects zero context."""
        self.plant("owner-example-com", "owner@example.com")
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((rc, out, err), (0, "", ""))
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--quiet"])
        self.assertEqual((rc, out, err), (0, "", ""))


class LineageTest(CredBase):
    """The review pair's demonstrated revocation bomb, pinned: the live-bytes
    clash check goes blind ONE borrower rotation after a byte-copy borrow —
    the hashes diverge, the clash vanishes, and the guard's auto-heal would
    restore the CONSUMED token, whose first refresh trips server-side reuse
    detection and revokes the whole family, bricking the live borrower. The
    family lineage remembers what the hashes forget."""

    def test_a_borrower_rotation_never_unblinds_the_auto_heal(self):
        """The executed repro, end-to-end through the exact hook command."""
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-S")
        cred.backup(cto, apply=True)          # the guard's snapshot of token S
        # token S byte-copied into a live borrower home (agreeing name, so
        # only admin-example-com ever drifts in this replay)
        self.plant("x-else-com", "x@else.com", token="FAKE-S")
        # a fleet turn boundary passes: the heal leg's census records where
        # each family is LIVE, exactly as the installed hook runs it
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((rc, out, err), (0, "", ""))
        # /login pollutes cto's home; then the borrower refreshes, ROTATING
        # its copy of S — the live-bytes hash clash vanishes right here
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        self.plant("x-else-com", "x@else.com", token="FAKE-S-ROTATED")
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((out, err), ("", ""))
        self.assertEqual(rc, 1)               # silent, honest refusal
        self.assertEqual(cred.account_of(cto)["email"], "owner@example.com")
        creds = json.load(open(os.path.join(cto, ".credentials.json")))
        self.assertEqual(creds["claudeAiOauth"]["refreshToken"], "FAKE-OWNER")
        plan = cred.heal()["plans"][0]
        self.assertEqual(plan["status"], "revocation-risk")
        self.assertIn("x-else-com", plan["reason"])
        self.assertIn("claude /login", plan["reason"])

    def test_the_hook_refuses_the_stale_pre_image_the_manual_path_warns_about(self):
        """Warnings are for humans: the hook auto-types --apply and --quiet
        swallows every line, so what the CLI surfaces the hook must REFUSE."""
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN")
        with open(os.path.join(cto, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"refreshToken": "FAKE-ADMIN",
                                         "expiresAt": 1}}, f)          # long dead
        cred.cache_clear()
        cred.backup(cto, apply=True)
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        before = open(os.path.join(cto, ".credentials.json"), "rb").read()
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((rc, out, err), (1, "", ""))
        self.assertEqual(open(os.path.join(cto, ".credentials.json"), "rb").read(),
                         before)              # untouched
        plan = cred.heal(apply=True, hook=True)["plans"][0]
        self.assertEqual(plan["status"], "stale-preimage")
        self.assertIn("reuse detection", plan["reason"])
        # the manual path still restores: the owner read the warning and
        # typed --apply — the risk is accepted knowingly, not by a hook
        res = cred.heal(apply=True)
        self.assertEqual(res["plans"][0]["status"], "restored")
        self.assertEqual(cred.account_of(cto)["email"], "admin@example.com")

    def test_the_hook_refuses_a_snapshot_whose_expiry_it_cannot_read(self):
        """An absent or unparseable expiresAt must fail CLOSED on the
        unattended path: freshness that cannot be proven is not freshness.
        The manual path warns and proceeds, as with stale."""
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN")
        with open(os.path.join(cto, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"refreshToken": "FAKE-ADMIN",
                                         "accessToken": "FAKE-ADMIN-A",
                                         "expiresAt": "soon"}}, f)  # unparseable
        cred.cache_clear()
        self.assertTrue(cred.backup(cto, apply=True)["ok"])
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        before = open(os.path.join(cto, ".credentials.json"), "rb").read()
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((rc, out, err), (1, "", ""))
        self.assertEqual(open(os.path.join(cto, ".credentials.json"), "rb").read(),
                         before)              # untouched
        plan = cred.heal(apply=True, hook=True)["plans"][0]
        self.assertEqual(plan["status"], "expiry-unknown")
        self.assertIn("fail closed", plan["reason"])
        res = cred.heal(apply=True)
        self.assertEqual(res["plans"][0]["status"], "restored")
        self.assertEqual(cred.account_of(cto)["email"], "admin@example.com")

    def test_count_pruning_never_evicts_a_family_a_snapshot_still_claims(self):
        """LINEAGE_KEEP caps the FORGETTABLE families only: while a surviving
        snapshot's meta claims a family, its lineage entry outlives any count
        of newer families — otherwise the ever-live-elsewhere refusal would
        expire while the consumed token it guards is still restorable."""
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-S")
        cred.backup(cto, apply=True)
        self.plant("x-else-com", "x@else.com", token="FAKE-S")
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((rc, out, err), (0, "", ""))          # census: S lives in both
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        self.plant("x-else-com", "x@else.com", token="FAKE-S-ROTATED")
        # 600 newer distinct families flood the lineage, far past the cap
        cred._lineage_record([("fam%04d" % i, "flood-home", None)
                              for i in range(600)])
        fams = cred._lineage_load()
        self.assertEqual(len(fams), cred.LINEAGE_KEEP)         # the cap held...
        fam = hashlib.sha256(b"FAKE-S").hexdigest()[:10]
        self.assertIn(fam, fams)                               # ...but S survived
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((out, err), ("", ""))
        self.assertEqual(rc, 1)                                # still refused
        plan = cred.heal()["plans"][0]
        self.assertEqual(plan["status"], "revocation-risk")
        self.assertIn("x-else-com", plan["reason"])

    def test_an_identical_skip_backup_still_censuses_the_family(self):
        """keepalive's pre-rotation capture lands on the identical-skip path
        whenever the home was already snapshotted at the last turn boundary;
        the observation must still enter the lineage, or a byte-copy borrowed
        elsewhere could be auto-restored after the grant consumes it."""
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-S")
        cred.backup(cto, apply=True)
        os.unlink(cred._lineage_path())       # forget every observation
        res = cred.backup(cto, apply=True)
        self.assertEqual(res["action"], "skip")
        fam = hashlib.sha256(b"FAKE-S").hexdigest()[:10]
        entry = cred._lineage_load()[fam]
        self.assertEqual(entry["homes"], ["admin-example-com"])
        self.assertEqual(entry["accounts"], ["admin@example.com"])
        # ...and the dry-run stays a filesystem no-op
        os.unlink(cred._lineage_path())
        before = self.tree_state()
        cred.backup(cto, apply=False)
        self.assertEqual(self.tree_state(), before)

    def test_first_ever_capture_refuses_tokens_the_lineage_binds_elsewhere(self):
        """The no-history gap, closed: _foreign_family needs a snapshot on
        record, but the lineage census sees every live home each turn
        boundary — so a fresh identity over another account's stale tokens
        (the opposite tear) is refused even on the FIRST-ever backup of a
        home."""
        home = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN")
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((rc, out, err), (0, "", ""))          # census only
        self.assertEqual(cred.snapshots("admin@example.com"), [])     # never snapshotted
        # a /login writes the identity file first; the token file still holds
        # cto's credentials — the opposite tear, mid-flight
        with open(os.path.join(home, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "eve@ex.com",
                                        "accountUuid": "u-e",
                                        "organizationName": "O"}}, f)
        cred.cache_clear()
        res = cred.backup(home, apply=True)
        self.assertFalse(res["ok"])
        self.assertIn("another account", res["reason"])
        self.assertEqual(cred.snapshots("eve@ex.com"), [])     # nothing poisoned

    def test_lineage_record_merges_and_survives_a_failed_lock(self):
        """The read-modify-write runs under an flock; a lock that cannot be
        taken degrades to the old best-effort write, never to a crash."""
        cred._lineage_record([("famaaaaaa01", "h1", "a@x.com")])
        with mock.patch.object(cred.fcntl, "flock", side_effect=OSError):
            cred._lineage_record([("famaaaaaa02", "h2", "b@x.com")])
        fams = cred._lineage_load()
        self.assertEqual(fams["famaaaaaa01"]["accounts"], ["a@x.com"])
        self.assertEqual(fams["famaaaaaa02"]["homes"], ["h2"])
        st = os.stat(cred._lineage_path() + ".lock")
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)

    def test_a_login_mid_census_never_records_a_cross_account_pairing(self):
        """The census/login race, pinned dead. The estate census reads a home's
        token FAMILY (.credentials.json, via homes_list) and its ACCOUNT
        (.claude.json, via verdict_for) with a window between the two. A /login
        completing inside that window makes the census pair the EVICTED
        account's family with the ARRIVING account's identity — a pairing that
        never existed on disk. Because the lineage accounts column is union-only
        and never pruned, that phantom pairing would thereafter auto-heal-refuse
        cto's own legitimate snapshots as torn. The stat-bracketed re-read drops
        the mid-write home this cycle instead of recording the tear."""
        home = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN")
        cto_real = os.path.realpath(home)
        fam_cto = hashlib.sha256(b"FAKE-ADMIN").hexdigest()[:10]
        fam_owner = hashlib.sha256(b"FAKE-OWNER").hexdigest()[:10]
        orig = homes._token_family
        fired = []

        def teared(provider, h):
            # Fire once, on the census's FIRST family read of cto's home: return
            # cto's real (old) family, THEN let owner's /login land in the same
            # home — so the account read that follows sees owner, not cto.
            if provider == "claude" and os.path.realpath(h) == cto_real and not fired:
                fired.append(1)
                fam = orig(provider, h)
                with open(os.path.join(cto_real, ".claude.json"), "w") as f:
                    json.dump({"oauthAccount": {"emailAddress": "owner@example.com",
                                                "accountUuid": "u-d",
                                                "organizationName": "O"}}, f)
                with open(os.path.join(cto_real, ".credentials.json"), "w") as f:
                    json.dump({"claudeAiOauth": {
                        "refreshToken": "FAKE-OWNER",
                        "accessToken": "FAKE-OWNER-A",
                        "expiresAt": int((time.time() + 3600) * 1000)}}, f)
                cred.cache_clear()
                return fam
            return orig(provider, h)

        with mock.patch.object(homes, "_token_family", teared):
            cred.heal(apply=True)                 # the apply-path census (record=True)
        self.assertTrue(fired)                    # the race was actually exercised
        # cto's family must NOT have been filed under owner — the phantom pair
        # that would brick cto's own snapshots on every later heal.
        self.assertNotIn("owner@example.com", cred._lineage_accounts(fam_cto))
        self.assertEqual(cred._lineage_accounts(fam_cto), set())   # dropped this cycle
        # and the true, coherent state that landed IS recorded — the census still
        # works, it only refuses the torn read.
        self.assertEqual(cred._lineage_accounts(fam_owner), {"owner@example.com"})


class TornPairTest(CredBase):
    """A backup that fires mid-/login can pair one account's freshly-landed
    TOKENS with another account's still-old IDENTITY — claude's two-file
    login write order is UNVERIFIED upstream, so nothing here assumes
    atomicity. A tear is an IDENTITY DISCONTINUITY, never a timing skew: the
    routine same-account refresh-rotation produces the exact same write
    shape (token file rewritten moments before the Stop capture, identity
    file legitimately older) and that snapshot is the NORMAL pre-image — the
    freshly rotated token is the very thing the guard exists to keep. The
    defense is layered: heal refuses (hook) or warns (manual) on family-claim
    evidence of a misbind; a mixed home whose token bytes already belong to
    another account's snapshots or lineage never becomes a snapshot at all;
    and the restore commit itself is signal-masked so the guard's own
    `timeout 10` SIGTERM cannot mint the mixed home."""

    def rotation_pre_image(self):
        """The r3 false positive's fixture, verbatim: cto's home refreshes
        mid-session (ROTATING the token — only .credentials.json rewritten,
        .claude.json a minute older), and the Stop hook snapshots it moments
        later. Same account in both files: the normal captured pre-image."""
        home = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN-OLD")
        old = time.time() - 60
        os.utime(os.path.join(home, ".claude.json"), (old, old))
        cred.cache_clear()
        with open(os.path.join(home, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {
                "refreshToken": "FAKE-ADMIN-ROTATED",
                "accessToken": "FAKE-ADMIN-ROTATED-A",
                "expiresAt": int((time.time() + 3600) * 1000)}}, f)
        cred.cache_clear()
        self.assertTrue(cred.backup(home, apply=True)["ok"])
        return home

    def test_a_same_account_rotation_pre_image_is_never_torn(self):
        """The false positive, pinned dead: the rotation-fresh snapshot is
        the exact pre-image the guard captures, and the guard's own hook
        command restores it — the evicted account is NOT bricked."""
        home = self.rotation_pre_image()
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(cred.account_of(home)["email"], "admin@example.com")
        creds = json.load(open(os.path.join(home, ".credentials.json")))
        self.assertEqual(creds["claudeAiOauth"]["refreshToken"],
                         "FAKE-ADMIN-ROTATED")

    def test_a_real_tear_is_refused_on_identity_discontinuity_evidence(self):
        """The realistic tear, end to end: alice's /login lands her tokens in
        owner's home moments before the Stop capture (identity file still
        owner's — the misbound snapshot is filed under owner), alice's login
        completes, a turn boundary censuses her live in that home, then bob
        pollutes it. The hook refuses the torn snapshot; the manual path
        warns and proceeds — the owner accepts the risk knowingly."""
        home = self.plant("owner-example-com", "owner@example.com", token="FAKE-AL")
        old = time.time() - 60
        os.utime(os.path.join(home, ".claude.json"), (old, old))
        cred.cache_clear()
        self.assertTrue(cred.backup(home, apply=True)["ok"])   # misbound: filed under owner
        self.plant("owner-example-com", "alice@ex.com", token="FAKE-AL")  # login completes
        # a fleet turn boundary passes: the census records alice live with
        # that family — while she HOLDS the home, heal refuses on occupant
        # evidence (her own tokens are the snapshot's bytes)
        with mock.patch.object(cred, "holders_of",
                               lambda p, default=False: [(4242, "claude")]):
            rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((rc, out, err), (1, "", ""))          # held, censused
        self.plant("owner-example-com", "bob@ex.com", token="FAKE-BOB")   # later drift
        before = open(os.path.join(home, ".credentials.json"), "rb").read()
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((out, err), ("", ""))
        self.assertEqual(rc, 1)
        self.assertEqual(open(os.path.join(home, ".credentials.json"), "rb").read(),
                         before)              # the misroute never happened
        plan = cred.heal(apply=True, hook=True)["plans"][0]
        self.assertEqual(plan["status"], "torn-pair")
        self.assertIn("alice@ex.com", plan["reason"])
        self.assertIn("claude /login", plan["reason"])
        # the manual dry-run WARNS on the same evidence, and manual --apply
        # proceeds: the owner read the warning and typed --apply
        plan = cred.heal()["plans"][0]
        self.assertEqual(plan["status"], "ready")
        self.assertIn("WARNING", plan["reason"])
        self.assertIn("alice@ex.com", plan["reason"])
        res = cred.heal(apply=True)
        self.assertEqual(res["plans"][0]["status"], "restored")
        self.assertEqual(cred.account_of(home)["email"], "owner@example.com")

    def test_a_torn_snapshot_is_refused_while_its_token_owner_occupies(self):
        """Occupant evidence alone (no census ever ran): the snapshot's token
        bytes are what the current occupant holds live — restoring would
        rebind the occupant's own tokens under the evicted identity."""
        home = self.plant("owner-example-com", "owner@example.com", token="FAKE-AL")
        old = time.time() - 60
        os.utime(os.path.join(home, ".claude.json"), (old, old))
        cred.cache_clear()
        self.assertTrue(cred.backup(home, apply=True)["ok"])
        self.plant("owner-example-com", "alice@ex.com", token="FAKE-AL")  # login completes
        rc, out, err = self.out(cred.cmd_cred, ["heal", "--apply", "--quiet"])
        self.assertEqual((out, err), ("", ""))
        self.assertEqual(rc, 1)
        plan = cred.heal(apply=True, hook=True)["plans"][0]
        self.assertEqual(plan["status"], "torn-pair")
        self.assertIn("alice@ex.com", plan["reason"])
        self.assertEqual(cred.account_of(home)["email"], "alice@ex.com")

    def test_a_mixed_home_never_becomes_a_snapshot(self):
        """The post-tear mixed home — account A's token bytes under account
        B's identity — is refused at CAPTURE, where the estate's own snapshot
        history makes the misbinding checkable. No poisoned pre-image is ever
        filed for a later restore to trust."""
        a = self.plant("owner-example-com", "owner@example.com", token="FAKE-OWNER")
        self.assertTrue(cred.backup(a, apply=True)["ok"])   # owner's family on record
        mixed = self.plant("bob-ex-com", "bob@ex.com", token="FAKE-OWNER")
        res = cred.backup(mixed, apply=True)
        self.assertFalse(res["ok"])
        self.assertIn("another account", res["reason"])
        self.assertEqual(cred.snapshots("bob@ex.com"), [])

    def test_the_two_file_commit_is_signal_masked(self):
        """The heal hook runs under `timeout 10` — a SCHEDULED SIGTERM, not
        crash luck. One landing between the two os.replace calls would leave
        restored credentials under the occupant's identity; the commit blocks
        catchable termination signals, so the kill only lands after BOTH
        files (and the verify) are done."""
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN")
        cred.backup(cto, apply=True)
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER")
        snap = cred.snapshots("admin@example.com")[-1]["path"]
        landed, delivered = [], []
        prev = signal.signal(signal.SIGTERM,
                             lambda *a: delivered.append(len(landed)))
        self.addCleanup(signal.signal, signal.SIGTERM, prev)
        real_replace = os.replace
        def kill_mid_commit(src, dst, *a, **kw):
            real_replace(src, dst, *a, **kw)
            landed.append(dst)
            if len(landed) == 1:              # right between the two replaces
                os.kill(os.getpid(), signal.SIGTERM)
        with mock.patch.object(cred.os, "replace", kill_mid_commit):
            res = cred.restore(snap, cto)
        self.assertTrue(res["ok"])
        self.assertEqual(delivered, [2])      # the kill waited out the commit
        self.assertEqual(cred.account_of(cto)["email"], "admin@example.com")
        creds = json.load(open(os.path.join(cto, ".credentials.json")))
        self.assertEqual(creds["claudeAiOauth"]["refreshToken"], "FAKE-ADMIN")


class SecrecyTest(CredBase):
    def test_no_output_path_ever_carries_a_credential_byte(self):
        """Every surface at once: list, backup, switch-guard, heal, doctor."""
        cto = self.plant("admin-example-com", "admin@example.com", token="FAKE-ADMIN-SECRET")
        cred.backup(cto, apply=True)
        self.plant("admin-example-com", "owner@example.com", token="FAKE-OWNER-SECRET")
        self.plant("team-example-com", "team@example.com", token="FAKE-TEAM-SECRET")
        text = ""
        for argv in (["list"], ["list", "--json"], ["backup", "--all", "--apply"],
                     ["switch-guard", "--home", "team-example-com", "--apply"],
                     ["heal"], ["heal", "--apply"]):
            _, out, err = self.out(cred.cmd_cred, argv)
            text += out + err
        text += "\n".join(m for _, m in cred.doctor_rows())
        for secret in ("FAKE-ADMIN-SECRET", "FAKE-OWNER-SECRET", "FAKE-TEAM-SECRET"):
            self.assertNotIn(secret, text)
        # …and the metadata files beside the creds are clean too
        for account in ("admin@example.com", "owner@example.com", "team@example.com"):
            for snap in cred.snapshots(account):
                for f in ("meta.json", "account.json"):
                    body = open(os.path.join(snap["path"], f)).read()
                    self.assertNotIn("FAKE-", body, "%s/%s" % (snap["ts"], f))

    def test_garbled_non_utf8_unreadable_and_exception_paths_never_echo_secrets(self):
        sentinel = "sk-ant-oat01-" + "S" * 96
        self.plant(sentinel, "path@example.test", token="FAKE-PATH-TOKEN")
        d = self.plant("broken-example-test", "broken@example.test", token=sentinel)
        with open(os.path.join(d, ".claude.json"), "wb") as f:
            f.write(b"\xff\xfe" + sentinel.encode())
        cred.cache_clear()
        text = ""
        for argv in (["list"], ["backup", "--all", "--apply"],
                     ["backup", "--home", sentinel]):
            _, out, err = self.out(cred.cmd_cred, argv)
            text += out + err
        with mock.patch.object(cred, "rows", side_effect=RuntimeError(sentinel)):
            _, out, err = self.out(cred.cmd_cred, ["list"])
            text += out + err + "\n".join(m for _, m in cred.doctor_rows())
        secret_root = os.path.join(self.tmp, sentinel)
        with mock.patch.dict(os.environ, {"HELM_CRED_BACKUP_ROOT": secret_root}):
            h = self.plant("redact-example-test", "redact@example.test", token="FAKE-R")
            cred.backup(h, apply=True)
            self.plant("redact-example-test", "other@example.test", token="FAKE-O")
            _, out, err = self.out(cred.cmd_cred, ["heal", "--apply"])
            text += out + err
        self.assertNotIn(sentinel, text)
        self.assertNotIn("Traceback", text)

    def test_digest_is_a_prefix_not_the_token(self):
        d = self.plant("owner-example-com", "owner@example.com")
        meta = json.load(open(os.path.join(cred.backup(d, apply=True)["dest"], "meta.json")))
        self.assertEqual(len(meta["digest"]), 12)
        self.assertNotIn(FAKE, json.dumps(meta))


class LaunchNoteTest(CredBase):
    def test_launch_prints_the_account_the_home_actually_holds(self):
        from helm import launch
        d = self.plant("admin-example-com", "owner@example.com")
        _, _, err = self.out(launch.home_note, d, "admin")
        self.assertIn("HOLDS owner@example.com", err)
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
