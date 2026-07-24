"""Hermetic tests for helm.seat — HELM_HOME and the codex-homes root both point
at tmp dirs; fake JWTs are minted in-test. The real ~/.codex-homes is never
read, no proxy is ever started, no claude is ever launched."""
import base64
import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock

from helm import seat


def _b64seg(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims):
    """header.payload.sig — enough structure for unverified payload decode."""
    return _b64seg({"alg": "RS256", "typ": "JWT"}) + "." + _b64seg(claims) + ".fake-sig"


def _warm_features(**extra):
    """Obviously synthetic GrowthBook-sized map; no live feature state in tests."""
    features = {"synthetic-feature-%03d" % i: False for i in range(100)}
    features["tengu_deferred_stub_tool"] = True
    features.update(extra)
    return features


class SeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seat-")
        self._env = {k: os.environ.get(k) for k in
                     ("HELM_HOME", "MELD_HOME", "HELM_PROXY_BIN",
                      "MELD_PROXY_BIN", "KIMI_API_KEY", "DS4PRO_API_KEY",
                      "HELM_PROC",
                      "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
                      "HELM_CHAT_ROOM_SOURCE", "MELD_CHAT_ROOM",
                      "MELD_CHAT_ROOM_SOURCE", "HELM_CODEX_HOMES_DIR",
                      "MELD_CODEX_HOMES_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ.pop("MELD_HOME", None)
        os.environ.pop("HELM_CODEX_HOMES_DIR", None)
        os.environ.pop("MELD_CODEX_HOMES_DIR", None)
        os.environ.pop("HELM_PROXY_BIN", None)
        os.environ.pop("MELD_PROXY_BIN", None)
        os.environ.pop("KIMI_API_KEY", None)  # hermetic: never the real key
        os.environ.pop("DS4PRO_API_KEY", None)
        # hermetic: the real ~/.hermes/auth.json must never feed a test mint
        self._hermes_auth = seat.HERMES_AUTH
        seat.HERMES_AUTH = os.path.join(self.tmp, "hermes-auth.json")
        # hermetic: the real opencode auth store must never feed a test mint.
        # Point it at a non-existent tmp path — tests that exercise the
        # authstore plant it explicitly; the rest fall through to hermes.
        self._opencode_authstore = seat.OPENCODE_AUTHSTORE
        seat.OPENCODE_AUTHSTORE = os.path.join(self.tmp, "opencode-auth.json")
        for key in ("HELM_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
                    "MELD_CHAT_ROOM", "MELD_CHAT_ROOM_SOURCE"):
            os.environ.pop(key, None)
        # launch's retrofit surface scans /proc + the roster — keep both tmp
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        self._codex_homes = seat.CODEX_HOMES
        seat.CODEX_HOMES = os.path.join(self.tmp, "codex-homes")
        os.makedirs(seat.CODEX_HOMES)
        self.timer = mock.patch.object(seat, "_ensure_autocompact_timer")
        self.timer.start()

    def tearDown(self):
        self.timer.stop()
        seat.CODEX_HOMES = self._codex_homes
        seat.HERMES_AUTH = self._hermes_auth
        seat.OPENCODE_AUTHSTORE = self._opencode_authstore
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def _plant(self, name, email="fake@example.com", plan="pro",
               exp_offset=3600, mtime=None):
        """A fake codex home with an auth.json shaped like the real CLI's."""
        d = os.path.join(seat.CODEX_HOMES, name)
        os.makedirs(d, exist_ok=True)
        exp = int(time.time()) + exp_offset
        auth = {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": _jwt({"email": email,
                                  "https://api.openai.com/auth":
                                      {"chatgpt_plan_type": plan}}),
                "access_token": _jwt({"exp": exp, "sub": "fake"}),
                "refresh_token": "fake-refresh-token-" + name,
                "account_id": "acct-" + name,
            },
            "last_refresh": "2026-07-09T14:52:47.713051089Z",
        }
        path = os.path.join(d, "auth.json")
        with open(path, "w") as f:
            json.dump(auth, f)
        if mtime:
            os.utime(path, (mtime, mtime))
        return path, auth, exp

    def _add(self, args=("add", "codex")):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(list(args))
        return rc, out.getvalue(), err.getvalue()

    # -- translation correctness ------------------------------------------
    def test_translation_correctness(self):
        path, auth, exp = self._plant("home-a")
        rec, fname, err = seat.translate_codex_auth(path)
        self.assertIsNone(err)
        self.assertEqual(fname, "codex-fake@example.com-pro.json")
        t = auth["tokens"]
        self.assertEqual(rec["id_token"], t["id_token"])
        self.assertEqual(rec["access_token"], t["access_token"])
        self.assertEqual(rec["refresh_token"], t["refresh_token"])
        self.assertEqual(rec["account_id"], t["account_id"])
        self.assertEqual(rec["last_refresh"], auth["last_refresh"])
        self.assertEqual(rec["email"], "fake@example.com")
        self.assertEqual(rec["type"], "codex")
        self.assertEqual(rec["expired"],
                         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)))
        # the eval's hard law: the OPENAI_API_KEY field never crosses over
        self.assertNotIn("OPENAI_API_KEY", rec)

    def test_translation_account_id_falls_back_to_jwt_claims(self):
        """kimi FIX 1 (upstream): tokens.account_id absent -> the
        chatgpt_account_id claim fills it (id_token first, access_token
        next) — a minted/pooled record never carries account_id None when
        identity knows it (dedup + pooled-linkage key off it)."""
        d = os.path.join(seat.CODEX_HOMES, "claim-only")
        os.makedirs(d)
        exp = int(time.time()) + 3600
        auth = {"tokens": {
            "id_token": _jwt({"email": "claim@x.com",
                              "https://api.openai.com/auth":
                                  {"chatgpt_plan_type": "pro",
                                   "chatgpt_account_id": "acct-claim-id"}}),
            "access_token": _jwt({"exp": exp, "sub": "fake"}),
            "refresh_token": "fake-refresh-token-claim",
        }, "last_refresh": "2026-07-09T14:52:47.713051089Z"}
        path = os.path.join(d, "auth.json")
        with open(path, "w") as f:
            json.dump(auth, f)
        rec, _fname, err = seat.translate_codex_auth(path)
        self.assertIsNone(err)
        self.assertEqual(rec["account_id"], "acct-claim-id")
        # the access_token-claim-only shape resolves too
        auth["tokens"]["id_token"] = _jwt({"email": "claim@x.com"})
        auth["tokens"]["access_token"] = _jwt(
            {"exp": exp, "https://api.openai.com/auth":
                {"chatgpt_account_id": "acct-claim-acc"}})
        with open(path, "w") as f:
            json.dump(auth, f)
        rec, _fname, err = seat.translate_codex_auth(path)
        self.assertIsNone(err)
        self.assertEqual(rec["account_id"], "acct-claim-acc")

    # -- add: layout + perms + source untouched ----------------------------
    def test_add_seat_layout_perms_and_readonly_source(self):
        path, _, _ = self._plant("home-a")
        with open(path, "rb") as f:
            before = f.read()
        rc, out, err = self._add()
        self.assertEqual(rc, 0, err)
        d = seat.seat_dir("codex")
        cred = os.path.join(d, "auth", "codex-fake@example.com-pro.json")
        for p, want in ((cred, 0o600), (os.path.join(d, "token"), 0o600),
                        (os.path.join(d, "config.yaml"), 0o600),
                        (os.path.join(d, "launch.sh"), 0o700)):
            self.assertTrue(os.path.exists(p), p)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), want, p)
        self.assertTrue(os.path.isdir(os.path.join(d, "claude")))
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)  # source byte-identical
        # config carries the token + auth-dir, and the hard law holds: the
        # string ANTHROPIC_API_KEY appears in NO seat file
        with open(os.path.join(d, "token")) as f:
            token = f.read().strip()
        with open(os.path.join(d, "config.yaml")) as f:
            cfg = f.read()
        self.assertIn(token, cfg)
        self.assertIn(os.path.join(d, "auth"), cfg)
        for root, _, files in os.walk(d):
            for name in files:
                with open(os.path.join(root, name)) as f:
                    self.assertNotIn("ANTHROPIC_API_KEY=", f.read())

    def test_add_preserves_other_pooled_accounts(self):
        """kimi FIX 2: `seat add codex` must not collapse the pool — pool
        three accounts, mint the seat from a fourth: all three survive (the
        proxy's usage-cap fall-through), plus the minted cred. Unattributable
        junk survives too (fail-open: never delete what can't be identified)."""
        from helm import codexhomes
        os.environ["HELM_CODEX_HOMES_DIR"] = seat.CODEX_HOMES
        now = time.time()
        for i, n in enumerate(("pool-a", "pool-b", "pool-c")):
            self._plant(n, email=n + "@x.com", mtime=now - 500 + i)
            self.assertTrue(codexhomes.codex_pool(n).get("ok"))
        auth_dir = os.path.join(seat.seat_dir("codex"), "auth")
        with open(os.path.join(auth_dir, "codex-junk.json"), "w") as f:
            f.write("{not json")
        self._plant("home-d", email="d@x.com", mtime=now)  # newest -> picked
        rc, out, err = self._add()
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(os.listdir(auth_dir)),
                         ["codex-d@x.com-pro.json", "codex-junk.json",
                          "codex-pool-a.json", "codex-pool-b.json",
                          "codex-pool-c.json"])
        self.assertNotIn("replaced same-account", out)  # nothing was removed
        self.assertIn("4 other pooled creds preserved", out)

    def test_re_add_replaces_only_same_account_and_prints_it(self):
        """kimi FIX 2: a stale pooled spelling of the SAME account is removed
        (the re-add IS its refresh) and reported; sibling accounts untouched."""
        auth_dir = os.path.join(seat.seat_dir("codex"), "auth")
        os.makedirs(auth_dir)
        with open(os.path.join(auth_dir, "codex-home-a.json"), "w") as f:
            json.dump({"type": "codex", "account_id": "acct-home-a"}, f)
        with open(os.path.join(auth_dir, "codex-pool-b.json"), "w") as f:
            json.dump({"type": "codex", "account_id": "acct-pool-b"}, f)
        self._plant("home-a")  # mints account acct-home-a
        rc, out, err = self._add()
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(os.listdir(auth_dir)),
                         ["codex-fake@example.com-pro.json", "codex-pool-b.json"])
        self.assertIn("replaced same-account pooled cred: codex-home-a.json", out)
        self.assertIn("1 other pooled cred preserved", out)

    def test_re_add_keeps_token(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with open(os.path.join(seat.seat_dir("codex"), "token")) as f:
            tok1 = f.read().strip()
        self.assertEqual(self._add()[0], 0)
        with open(os.path.join(seat.seat_dir("codex"), "token")) as f:
            self.assertEqual(f.read().strip(), tok1)

    # -- default --auth-from: newest VALID wins, expired skipped ------------
    def test_newest_valid_selection(self):
        now = time.time()
        self._plant("older-valid", email="old@x.com", mtime=now - 5000)
        want, _, _ = self._plant("newer-valid", email="new@x.com", mtime=now - 100)
        self._plant("newest-expired", email="dead@x.com", exp_offset=-60, mtime=now)
        got, err = seat.newest_valid_codex_auth()
        self.assertIsNone(err)
        self.assertEqual(got, want)

    def test_symlink_alias_homes_dedupe(self):
        self._plant("real-home")
        os.symlink(os.path.join(seat.CODEX_HOMES, "real-home"),
                   os.path.join(seat.CODEX_HOMES, "alias-home"))
        seen = set()
        for p in [seat.newest_valid_codex_auth()[0]]:
            seen.add(os.path.realpath(p))
        self.assertEqual(len(seen), 1)

    # -- expired/absent cred: refusal + human unblock line, exit 1 ----------
    def test_expired_cred_refusal(self):
        self._plant("dead", exp_offset=-60)
        rc, out, err = self._add()
        self.assertEqual(rc, 1)
        self.assertIn("codex login --device-auth", err)  # the human's unblock line
        self.assertFalse(os.path.exists(os.path.join(seat.seat_dir("codex"), "config.yaml")))

    def test_absent_cred_refusal(self):
        rc, out, err = self._add()
        self.assertEqual(rc, 1)
        self.assertIn("codex login --device-auth", err)

    def test_explicit_auth_from_expired_refused(self):
        path, _, _ = self._plant("dead", exp_offset=-60)
        rc, out, err = self._add(("add", "codex", "--auth-from", path))
        self.assertEqual(rc, 1)
        self.assertIn("expired", err)

    def test_unknown_family_refused(self):
        rc, out, err = self._add(("add", "glm"))
        self.assertEqual(rc, 2)
        self.assertIn("not yet wired", err)

    # -- proxy-key families (kimi) ------------------------------------------
    def test_config_yaml_key_exact_shape(self):
        cfg = seat._config_yaml_key(8318, "tok-abc", "moonshot",
                                    "https://api.moonshot.ai/v1", "kimi-k3",
                                    "fake-key-xyz")
        self.assertEqual(cfg, (
            'host: "127.0.0.1"\n'
            "port: 8318\n"
            "api-keys:\n"
            '  - "tok-abc"\n'
            "debug: false\n"
            "usage-statistics-enabled: false\n"
            "remote-management:\n"
            "  allow-remote: false\n"
            '  secret-key: ""\n'
            "  disable-control-panel: true\n"
            "openai-compatibility:\n"
            '  - name: "moonshot"\n'
            '    base-url: "https://api.moonshot.ai/v1"\n'
            "    api-key-entries:\n"
            '      - api-key: "fake-key-xyz"\n'
            "    models:\n"
            '      - name: "kimi-k3"\n'
            '        alias: "kimi-k3"\n'
            # long-nonstream keepalive (compaction survival) rides every config
            "nonstream-keepalive-interval: 15\n"
            # streaming-leg survival (the ~90%-context empty-200 class)
            "streaming:\n"
            "  keepalive-seconds: 15\n"
            "  bootstrap-retries: 2\n"))
        self.assertNotIn("auth-dir", cfg)

    # -- key-flavor -> base-url dispatch (kimi coding vs Moonshot platform) --
    def test_key_base_url_dispatch(self):
        fam = seat.FAMILIES["kimi"]
        # coding-plan flavor -> the kimi.com coding endpoint
        self.assertEqual(seat._key_base_url(fam, "sk-kimi-abc123"),
                         "https://api.kimi.com/coding/v1")
        # platform flavor (plain sk-) -> the Moonshot platform endpoint
        self.assertEqual(seat._key_base_url(fam, "sk-abc123"),
                         "https://api.moonshot.ai/v1")
        # a family with no key_base_urls keeps its single base_url
        plain = {"base_url": "https://example.test/v1"}
        self.assertEqual(seat._key_base_url(plain, "sk-anything"),
                         "https://example.test/v1")

    def test_add_kimi_coding_plan_key_routes_to_coding_endpoint(self):
        os.environ["KIMI_API_KEY"] = "sk-kimi-fake-coding-plan-key"
        rc, out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("kimi"), "config.yaml")) as f:
            cfg = f.read()
        self.assertIn('base-url: "https://api.kimi.com/coding/v1"', cfg)
        self.assertIn('api-key: "sk-kimi-fake-coding-plan-key"', cfg)

    def test_add_kimi_from_env_var(self):
        os.environ["KIMI_API_KEY"] = "fake-kimi-key-for-tests"
        rc, out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("fake-kimi-key-for-tests", out + err)  # never printed
        d = seat.seat_dir("kimi")
        for p, want in ((os.path.join(d, "token"), 0o600),
                        (os.path.join(d, "config.yaml"), 0o600),
                        (os.path.join(d, "launch.sh"), 0o700)):
            self.assertTrue(os.path.exists(p), p)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), want, p)
        self.assertTrue(os.path.isdir(os.path.join(d, "claude")))
        self.assertFalse(os.path.exists(os.path.join(d, "auth")))  # no OAuth dir
        with open(os.path.join(d, "config.yaml")) as f:
            cfg = f.read()
        self.assertIn("openai-compatibility:", cfg)
        self.assertIn('api-key: "fake-kimi-key-for-tests"', cfg)
        # not "sk-kimi-…" -> the Moonshot PLATFORM endpoint (the default)
        self.assertIn('base-url: "https://api.moonshot.ai/v1"', cfg)
        self.assertIn('alias: "kimi-k3"', cfg)
        self.assertNotIn("auth-dir", cfg)
        with open(os.path.join(d, "token")) as f:
            self.assertIn(f.read().strip(), cfg)  # inbound seat token present
        # the hard law holds for proxy-key seats too
        for root, _, files in os.walk(d):
            for name in files:
                with open(os.path.join(root, name)) as f:
                    self.assertNotIn("ANTHROPIC_API_KEY=", f.read())

    def test_add_kimi_key_from_file(self):
        envfile = os.path.join(self.tmp, "fake.env")
        with open(envfile, "w") as f:
            f.write("# comment\nexport KIMI_API_KEY='fake-from-file-key'\n")
        rc, out, err = self._add(("add", "kimi", "--key-from", envfile))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("kimi"), "config.yaml")) as f:
            self.assertIn('api-key: "fake-from-file-key"', f.read())

    def test_add_kimi_env_var_beats_key_from(self):
        os.environ["KIMI_API_KEY"] = "fake-env-wins"
        envfile = os.path.join(self.tmp, "fake.env")
        with open(envfile, "w") as f:
            f.write("KIMI_API_KEY=fake-file-loses\n")
        rc, _, err = self._add(("add", "kimi", "--key-from", envfile))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("kimi"), "config.yaml")) as f:
            self.assertIn('api-key: "fake-env-wins"', f.read())

    def test_add_kimi_missing_key_unblock(self):
        rc, out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 1)
        self.assertIn("KIMI_API_KEY", err)  # names the env-var option
        self.assertIn("--key-from", err)    # and the file option
        self.assertFalse(os.path.exists(os.path.join(seat.seat_dir("kimi"),
                                                     "config.yaml")))

    def test_add_kimi_key_from_missing_line(self):
        envfile = os.path.join(self.tmp, "empty.env")
        with open(envfile, "w") as f:
            f.write("OTHER_VAR=1\n")
        rc, out, err = self._add(("add", "kimi", "--key-from", envfile))
        self.assertEqual(rc, 1)
        self.assertIn("KIMI_API_KEY", err)

    def test_kimi_launch_line_shape(self):
        os.environ["KIMI_API_KEY"] = "fake-kimi-key-for-tests"
        self.assertEqual(self._add(("add", "kimi"))[0], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["launch", "kimi"])
        self.assertEqual(rc, 0)
        line = out.getvalue().strip()
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8318", line)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=kimi-k3", line)
        self.assertIn("HELM_CHAT_NAME=kimi", line)   # joins the roster as 'kimi'
        self.assertIn("HELM_CELL_BIN=" + seat.DREGG_SIGNER_DEFAULT, line)
        self.assertIn("HELM_CELL_PROFILE=kimi", line)  # helm call-site identity
        self.assertIn("DREGG_PROFILE=kimi", line)      # signer fallback identity
        self.assertIn("--dangerously-skip-permissions", line)  # canonical seat
        self.assertTrue(line.endswith(
            "claude --dangerously-skip-permissions --model kimi-k3"))
        self.assertNotIn("fake-kimi-key-for-tests", line)  # key never rides

    # -- ds4pro (pool-keyed proxy-key family, credential_pool bearer) --------
    # The literal bearer token every test bakes/asserts-absent. A single
    # constant so the secret-hygiene assertions (assertNotIn) can never drift
    # out of sync with what was planted.
    _LIVE = "sk-live-ds4pro-bearer-3f9c2a1e6b7d8049aa11bb22cc33dd44"
    # the opencode-authstore fake bearers — distinct per provider and distinct
    # from _LIVE so a test can prove WHICH source (authstore vs hermes) won.
    _AS_OC = "sk-authstore-opencode-go-1122334455667788990011223344ff"
    _AS_DS = "sk-authstore-deepseek-9a8b7c6d5e4f3021ffeeddccbbaa9988ee"

    def _plant_authstore(self, opencode=True, deepseek=True, oauth_ds=False):
        """A fake ~/.local/share/opencode/auth.json shaped like the real one:
        a dict of provider -> {type, key}. `oauth_ds` swaps deepseek to an
        oauth entry (no bakeable static key) to exercise the type filter."""
        store = {}
        if opencode:
            store["opencode-go"] = {"type": "api", "key": self._AS_OC}
        if deepseek and not oauth_ds:
            store["deepseek"] = {"type": "api", "key": self._AS_DS}
        if oauth_ds:
            store["deepseek"] = {"type": "oauth", "access": "a" * 60,
                                 "refresh": "r" * 60, "expires": 9999999999999}
        with open(seat.OPENCODE_AUTHSTORE, "w") as f:
            json.dump(store, f)

    def _plant_pool(self, opencode=None, openrouter=None, extra=None):
        """A fake ~/.hermes/auth.json shaped like the real one: a
        credential_pool of provider -> LIST of bearer entries. Defaults plant
        BOTH ds4pro providers with the live bearer at priority 0 (last_status
        ok). `extra` injects additional entries per provider to exercise
        selection (junk placeholders, expired/non-ok)."""
        pool = {
            "opencode-go": [{"access_token": opencode or self._LIVE,
                             "base_url": "https://opencode.ai/zen/go/v1",
                             "label": "OPENCODE_GO_API_KEY",
                             "last_status": "ok", "priority": 0,
                             "auth_type": "api_key"}],
            "openrouter": [{"access_token": openrouter or self._LIVE,
                            "base_url": "https://openrouter.ai/api/v1",
                            "label": "OPENROUTER_API_KEY",
                            "last_status": "ok", "priority": 0,
                            "auth_type": "api_key"}],
        }
        for prov, entries in (extra or {}).items():
            pool.setdefault(prov, []).extend(entries)
        with open(seat.HERMES_AUTH, "w") as f:
            json.dump({"version": 1, "providers": {},
                       "credential_pool": pool}, f)

    def test_family_ports_unique_with_interleave_headroom(self):
        """The port invariant, extended for ds4pro: every family owns a
        distinct port, ds4pro sits at 8360 — clear of the codex 8317+N
        instance range and kimi's 8318 (no OTHER family within 8350-8370)."""
        ports = {f: fam["port"] for f, fam in seat.FAMILIES.items()}
        self.assertEqual(len(set(ports.values())), len(ports), ports)
        self.assertEqual(ports["ds4pro"], 8360)
        for f, p in ports.items():
            if f != "ds4pro":
                self.assertFalse(8350 <= p <= 8370,
                                 "%s port %d crowds ds4pro's headroom" % (f, p))

    def test_ds4pro_family_shape(self):
        fam = seat.FAMILIES["ds4pro"]
        self.assertEqual(fam["mode"], "proxy-key")
        self.assertEqual(fam["model"], "ds4-pro")           # claude-side alias
        self.assertEqual(fam["key_env"], "DS4PRO_API_KEY")
        self.assertEqual(fam["probe_models"], ("ds4-pro",))
        self.assertIn("max_context", fam)
        # multi-provider: opencode-go is the owner's long-term default;
        # openrouter is the credit-bearing test route. Each carries the REAL
        # model id that provider's /models advertises (probed 2026-07-22).
        self.assertEqual(fam["pool_default"], "opencode-go")
        self.assertEqual(fam["pool_providers"]["opencode-go"],
                         {"base_url": "https://opencode.ai/zen/go/v1",
                          "upstream_model": "deepseek-v4-pro",
                          "authstore": "opencode-go"})
        # native DeepSeek: real model id deepseek-v4-pro (probed 2026-07-22),
        # base api.deepseek.com; key valid but 402 (no balance) so configured
        # not live-default.
        self.assertEqual(fam["pool_providers"]["deepseek"],
                         {"base_url": "https://api.deepseek.com",
                          "upstream_model": "deepseek-v4-pro",
                          "authstore": "deepseek"})
        self.assertEqual(fam["pool_providers"]["openrouter"],
                         {"base_url": "https://openrouter.ai/api/v1",
                          "upstream_model": "deepseek/deepseek-v4-pro",
                          "authstore": "openrouter"})
        # the retired nous portal shape is gone
        self.assertNotIn("hermes_provider", fam)
        self.assertNotIn("provider", fam)

    def test_config_yaml_key_upstream_alias_mapping(self):
        cfg = seat._config_yaml_key(8360, "tok", "openrouter",
                                    "https://openrouter.ai/api/v1",
                                    "ds4-pro", "fake-key",
                                    "deepseek/deepseek-v4-pro")
        self.assertIn('- name: "deepseek/deepseek-v4-pro"\n'
                      '        alias: "ds4-pro"', cfg)

    # -- the auth reader: live-entry selection ------------------------------
    def test_pool_reader_picks_live_bearer_over_junk_and_non_ok(self):
        """_hermes_pool_key selects the real bearer, never the 1-char junk
        placeholder (opencode-go's real [1] entry), and prefers last_status
        ok / lowest priority."""
        self._plant_pool(extra={"opencode-go": [
            {"access_token": "\x1b", "last_status": None, "priority": 1},
            {"access_token": "sk-stale-not-ok", "last_status": "exhausted",
             "priority": 0},
        ]})
        tok, base, err = seat._hermes_pool_key("opencode-go")
        self.assertIsNone(err)
        self.assertEqual(tok, self._LIVE)     # the ok/priority-0 real bearer
        self.assertEqual(base, "https://opencode.ai/zen/go/v1")

    def test_pool_reader_all_junk_reports_no_live_bearer(self):
        with open(seat.HERMES_AUTH, "w") as f:
            json.dump({"credential_pool": {"opencode-go": [
                {"access_token": "\x1b", "priority": 1}]}}, f)
        tok, base, err = seat._hermes_pool_key("opencode-go")
        self.assertIsNone(tok)
        self.assertIn("no live bearer", err)

    def test_pool_reader_missing_provider(self):
        self._plant_pool()
        tok, _, err = seat._hermes_pool_key("nope")
        self.assertIsNone(tok)
        self.assertIn("no credential_pool.nope", err)

    # -- the opencode auth store reader -------------------------------------
    def test_authstore_reader_picks_api_key(self):
        self._plant_authstore()
        key, err = seat._opencode_authstore_key("opencode-go")
        self.assertIsNone(err)
        self.assertEqual(key, self._AS_OC)
        key, err = seat._opencode_authstore_key("deepseek")
        self.assertIsNone(err)
        self.assertEqual(key, self._AS_DS)

    def test_authstore_reader_skips_oauth_entry(self):
        """An oauth entry carries no static bakeable key — the reader reports
        it, never returns the access token as a key."""
        self._plant_authstore(oauth_ds=True)
        key, err = seat._opencode_authstore_key("deepseek")
        self.assertIsNone(key)
        self.assertIn("not a static api key", err)

    def test_authstore_reader_missing_provider_and_file(self):
        self._plant_authstore()
        key, err = seat._opencode_authstore_key("nope")
        self.assertIsNone(key)
        self.assertIn("no nope entry", err)
        os.remove(seat.OPENCODE_AUTHSTORE)
        key, err = seat._opencode_authstore_key("opencode-go")
        self.assertIsNone(key)
        self.assertIn("unreadable", err)

    def test_add_ds4pro_prefers_authstore_over_hermes(self):
        """Both sources present: the authstore bearer is baked, the hermes
        _LIVE bearer is NOT — and neither raw value is ever printed."""
        self._plant_authstore()
        self._plant_pool()          # hermes _LIVE also present
        rc, out, err = self._add(("add", "ds4pro"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn(self._AS_OC, out + err)   # secret: never printed
        self.assertNotIn(self._LIVE, out + err)
        d = seat.seat_dir("ds4pro")
        with open(os.path.join(d, "config.yaml")) as f:
            cfg = f.read()
        self.assertIn('api-key: "%s"' % self._AS_OC, cfg)   # authstore won
        self.assertNotIn(self._LIVE, cfg)                   # not hermes
        self.assertIn('base-url: "https://opencode.ai/zen/go/v1"', cfg)
        with open(os.path.join(d, "launch.sh")) as f:
            self.assertNotIn(self._AS_OC, f.read())         # never in launch.sh

    def test_add_ds4pro_deepseek_provider_from_authstore(self):
        """--provider deepseek bakes the native DeepSeek key + real model id
        deepseek-v4-pro at api.deepseek.com."""
        self._plant_authstore()
        rc, out, err = self._add(("add", "ds4pro", "--provider", "deepseek"))
        self.assertEqual(rc, 0, err)
        self.assertIn("provider deepseek", out)
        self.assertNotIn(self._AS_DS, out + err)
        with open(os.path.join(seat.seat_dir("ds4pro"), "config.yaml")) as f:
            cfg = f.read()
        self.assertIn('api-key: "%s"' % self._AS_DS, cfg)
        self.assertIn('name: "deepseek"', cfg)
        self.assertIn('base-url: "https://api.deepseek.com"', cfg)
        self.assertIn('- name: "deepseek-v4-pro"', cfg)

    def test_add_ds4pro_falls_back_to_hermes_when_authstore_absent(self):
        """No authstore file -> the hermes credential_pool bearer is used."""
        self._plant_pool()          # authstore path does not exist
        rc, _, err = self._add(("add", "ds4pro"))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("ds4pro"), "config.yaml")) as f:
            self.assertIn('api-key: "%s"' % self._LIVE, f.read())

    def test_add_ds4pro_default_provider_is_opencode_go(self):
        self._plant_pool()
        rc, out, err = self._add(("add", "ds4pro"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn(self._LIVE, out + err)   # secret: never printed
        self.assertIn("provider opencode-go", out)
        d = seat.seat_dir("ds4pro")
        for p, want in ((os.path.join(d, "token"), 0o600),
                        (os.path.join(d, "config.yaml"), 0o600),
                        (os.path.join(d, "launch.sh"), 0o700)):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), want, p)
        with open(os.path.join(d, "config.yaml")) as f:
            cfg = f.read()
        self.assertIn('api-key: "%s"' % self._LIVE, cfg)     # baked 0600
        self.assertIn('name: "opencode-go"', cfg)
        self.assertIn('base-url: "https://opencode.ai/zen/go/v1"', cfg)
        self.assertIn('- name: "deepseek-v4-pro"', cfg)      # opencode's id
        self.assertIn('alias: "ds4-pro"', cfg)
        self.assertIn("port: 8360", cfg)
        self.assertNotIn("auth-dir", cfg)     # no OAuth dir for proxy-key

    def test_add_ds4pro_provider_openrouter_route(self):
        self._plant_pool()
        rc, out, err = self._add(("add", "ds4pro", "--provider", "openrouter"))
        self.assertEqual(rc, 0, err)
        self.assertIn("provider openrouter", out)
        with open(os.path.join(seat.seat_dir("ds4pro"), "config.yaml")) as f:
            cfg = f.read()
        self.assertIn('name: "openrouter"', cfg)
        self.assertIn('base-url: "https://openrouter.ai/api/v1"', cfg)
        self.assertIn('- name: "deepseek/deepseek-v4-pro"', cfg)  # openrouter id
        self.assertNotIn(self._LIVE, out + err)

    def test_add_ds4pro_unknown_provider_refused(self):
        self._plant_pool()
        rc, _, err = self._add(("add", "ds4pro", "--provider", "bogus"))
        self.assertEqual(rc, 2)
        self.assertIn("no provider 'bogus'", err)
        self.assertIn("opencode-go", err)     # names the valid choices

    def test_add_ds4pro_pool_entry_base_url_wins(self):
        """The credential_pool entry's own base_url overrides the family
        default, so the seat rides exactly the endpoint the cred was minted
        for (owner moves a gateway without a code change)."""
        self._plant_pool(opencode=self._LIVE)
        with open(seat.HERMES_AUTH) as f:
            data = json.load(f)
        data["credential_pool"]["opencode-go"][0]["base_url"] = \
            "https://opencode.ai/zen/go/v2"
        with open(seat.HERMES_AUTH, "w") as f:
            json.dump(data, f)
        rc, _, err = self._add(("add", "ds4pro"))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("ds4pro"), "config.yaml")) as f:
            self.assertIn('base-url: "https://opencode.ai/zen/go/v2"', f.read())

    def test_add_ds4pro_env_var_beats_pool(self):
        self._plant_pool()
        os.environ["DS4PRO_API_KEY"] = "sk-env-wins-over-pool"
        rc, _, err = self._add(("add", "ds4pro"))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("ds4pro"), "config.yaml")) as f:
            self.assertIn('api-key: "sk-env-wins-over-pool"', f.read())

    def test_add_ds4pro_missing_everything_names_all_sources(self):
        rc, out, err = self._add(("add", "ds4pro"))   # no env/file/pool
        self.assertEqual(rc, 1)
        self.assertIn("DS4PRO_API_KEY", err)
        self.assertIn("--key-from", err)
        self.assertIn(seat.HERMES_AUTH, err)  # names the credential_pool source
        self.assertIn("opencode-go", err)     # names the selected provider
        self.assertFalse(os.path.exists(os.path.join(seat.seat_dir("ds4pro"),
                                                     "config.yaml")))

    def test_ds4pro_launch_line_shape_and_never_leaks_token(self):
        self._plant_pool()
        self.assertEqual(self._add(("add", "ds4pro"))[0], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["launch", "ds4pro"])
        self.assertEqual(rc, 0)
        line = out.getvalue().strip()
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8360", line)
        self.assertIn("HELM_CHAT_NAME=ds4pro", line)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000", line)
        self.assertTrue(line.endswith(
            "claude --dangerously-skip-permissions --model ds4-pro"))
        self.assertNotIn("deepseek", line)    # alias on the wire, not the id
        self.assertNotIn(self._LIVE, line)     # the outbound bearer never rides

    def test_ds4pro_launch_sh_never_contains_the_bearer(self):
        """The 0700 launch.sh is the real launch artifact — the outbound
        bearer lives only in the 0600 config.yaml the proxy reads, never in
        the launch line/argv/script."""
        self._plant_pool()
        self.assertEqual(self._add(("add", "ds4pro"))[0], 0)
        with open(os.path.join(seat.seat_dir("ds4pro"), "launch.sh")) as f:
            self.assertNotIn(self._LIVE, f.read())

    # -- launch line shape --------------------------------------------------
    def test_launch_line_shape(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with open(os.path.join(seat.seat_dir("codex"), "token")) as f:
            token = f.read().strip()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["launch", "codex"])
        self.assertEqual(rc, 0)
        line = out.getvalue().strip()
        # paste-line = token export (builtin, no argv) + env/claude command
        self.assertTrue(line.startswith("ANTHROPIC_AUTH_TOKEN=$(cat "), line)
        self.assertIn("; export ANTHROPIC_AUTH_TOKEN; env -u ANTHROPIC_API_KEY ", line)
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8317", line)
        # no-keys-in-argv (the 7bb422a xrev): the bearer is NEVER the literal —
        # the line reads it from the 0600 token file at exec time, so only the
        # PATH crosses stdout/argv, and the line is mint-order-immune.
        self.assertNotIn(token, line)
        self.assertIn("ANTHROPIC_AUTH_TOKEN=$(cat ", line)
        self.assertIn(os.path.join(seat.seat_dir("codex"), "token"), line)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-sol", line)
        self.assertIn("CLAUDE_CONFIG_DIR=" + os.path.join(seat.seat_dir("codex"), "claude"), line)
        self.assertIn("HELM_CHAT_NAME=codex", line)   # stable seat identity
        self.assertIn("HELM_CELL_BIN=" + seat.DREGG_SIGNER_DEFAULT, line)
        self.assertIn("HELM_CELL_PROFILE=codex", line)  # never inherit owner
        self.assertIn("DREGG_PROFILE=codex", line)
        self.assertIn("--dangerously-skip-permissions", line)  # canonical seat
        self.assertTrue(line.endswith(
            "claude --dangerously-skip-permissions --model gpt-5.6-sol"))
        self.assertNotIn("ANTHROPIC_API_KEY=", line)  # unset, never set
        # --model override rides both slots
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            seat.cmd_seat(["launch", "codex", "--model", "gpt-5.5"])
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.5", out.getvalue())
        self.assertIn(
            "claude --dangerously-skip-permissions --model gpt-5.5",
            out.getvalue())

    def test_launch_line_context_window(self):
        """ctx-window fix: proxy seats mint CLAUDE_CODE_MAX_CONTEXT_TOKENS
        (per-family real window, teaching CC past its hardcoded 200k) +
        CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, so a non-claude seat compacts before
        the unrecoverable 400. Every proxy family mints its real window (kimi
        included — k3 is 1M); signing env intact."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        line = seat.launch_line("codex")
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=360000", line)  # sol's real window
        self.assertIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=78", line)
        # ctxenv appends AFTER the signing env, which stays byte-identical
        self.assertIn("DREGG_PROFILE=codex CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=78", line)
        # kimi's k3 is a 1M-window model — mint the real max so CC's gauge and
        # autocompact stop tracking the hardcoded 200k (kimi was compacting 5x
        # too early).
        kline = seat.launch_line("kimi")
        self.assertIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=78", kline)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000", kline)

    # -- the child-stamp guard (child-stamp-kills-seat-persistence) ---------
    def test_launch_line_strips_child_stamp(self):
        """A pane minted by a daemon born inside a Claude session inherits
        CLAUDE_CODE_CHILD_SESSION + the daemon's SID/bridge id — CC then runs
        the seat as a subprocess child with transcript persistence silently
        OFF. Every launch line unsets the trio BEFORE the first export, for
        every family and instance; the pinned byte layout is untouched."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        for line in (seat.launch_line("codex"), seat.launch_line("kimi"),
                     seat.launch_line("codex", seat="codex-2")):
            for v in seat.CHILD_STAMP_VARS:
                self.assertIn("-u " + v, line)
                self.assertNotIn(v + "=", line)      # unset, never re-exported
            # the unsets ride the env prefix, ahead of the first export
            self.assertLess(line.index("-u CLAUDE_CODE_CHILD_SESSION"),
                            line.index("ANTHROPIC_BASE_URL="))
        # byte-layout pins survive: head, signing adjacency, tail
        line = seat.launch_line("codex")
        self.assertTrue(line.startswith("env -u ANTHROPIC_API_KEY "))
        self.assertIn("DREGG_PROFILE=codex CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=78", line)
        self.assertTrue(line.endswith(
            "claude --dangerously-skip-permissions --model gpt-5.6-sol"))

    def test_launch_sh_unsets_child_stamp_before_exec(self):
        """The minted launch.sh strips the child-session stamp with an explicit
        `unset` line ABOVE the exec (and the exec'd line carries the same -u
        trio) — a seat pane starts top-level whatever env its spawner leaked.
        Instances re-mint through the same _write_launch_assets: same guard."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        unset_line = "unset " + " ".join(seat.CHILD_STAMP_VARS)
        inst = seat._instance_dir("codex", "codex-2")
        seat._write_launch_assets("codex", inst, seat="codex-2")
        for d in (seat.seat_dir("codex"), inst):
            with open(os.path.join(d, "launch.sh")) as f:
                sh = f.read()
            self.assertIn(unset_line, sh)
            self.assertLess(sh.index(unset_line), sh.index("exec "))
            for v in seat.CHILD_STAMP_VARS:
                self.assertIn("-u " + v, sh)          # belt: the exec line too
                self.assertNotIn(v + "=", sh)

    def test_seat_env_strips_child_stamp(self):
        """smoke's subprocess env mirrors launch_line — the inherited stamp
        must not ride into a smoke claude either."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        stamped = {v: "leaked" for v in seat.CHILD_STAMP_VARS}
        with mock.patch.dict(os.environ, stamped):
            env = seat._seat_env("codex", os.path.join(self.tmp, "smoke"))
        for v in seat.CHILD_STAMP_VARS:
            self.assertNotIn(v, env)

    def test_seat_gets_host_skills(self):
        """A seat's fresh config dir has no skills of its own, so seat agents
        couldn't /learn — _write_launch_assets links skills in: canonical-first
        (skillsync), and when NO canonical exists on the host (pinned here via
        a dead HELM_SKILLS_CANONICAL) it falls back to the minting host's
        CLAUDE_CONFIG_DIR skills; a real skills dir on a seat is never
        clobbered at mint (that repair is `helm skills sync`'s job)."""
        host = os.path.join(self.tmp, "host-config")
        os.makedirs(os.path.join(host, "skills", "learn"))
        d = seat.seat_dir("codex")
        os.makedirs(d, exist_ok=True)
        no_deck = {"CLAUDE_CONFIG_DIR": host,
                   "HELM_SKILLS_CANONICAL": os.path.join(self.tmp, "no-such-deck")}
        with mock.patch.dict(os.environ, no_deck):
            seat._write_launch_assets("codex", d)
        link = os.path.join(d, "claude", "skills")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(host, "skills")))
        self.assertTrue(os.path.isdir(os.path.join(link, "learn")))  # resolves through
        # a real skills dir on the seat is never replaced
        os.unlink(link)
        os.makedirs(link)
        open(os.path.join(link, "own.md"), "w").close()
        with mock.patch.dict(os.environ, no_deck):
            seat._write_launch_assets("codex", d)
        self.assertFalse(os.path.islink(link))
        self.assertTrue(os.path.exists(os.path.join(link, "own.md")))

    def test_seat_onboarding_seeded(self):
        """A fresh seat config dir would trigger CC's first-run wizard and stall
        the seat before it joins chat — _write_launch_assets seeds .claude.json
        with the onboarding-complete flags (copied from an onboarded host, no
        host state leaked); a seat's own .claude.json is never clobbered."""
        host = os.path.join(self.tmp, "host2")
        os.makedirs(host)
        with open(os.path.join(host, ".claude.json"), "w") as f:
            json.dump({"hasCompletedOnboarding": True,
                       "lastOnboardingVersion": "9.9.9", "theme": "light",
                       "secretProjects": {"x": 1},
                       "projects": {
                           "/trusted/repo": {"hasTrustDialogAccepted": True,
                                             "lastCost": 4.2, "lastSessionId": "s"},
                           "/untrusted/repo": {"hasTrustDialogAccepted": False}}}, f)
        d = seat.seat_dir("codex")
        os.makedirs(d, exist_ok=True)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": host}):
            seat._write_launch_assets("codex", d, workdir=self.tmp)
        p = os.path.join(d, "claude", ".claude.json")
        with open(p) as f:
            seeded = json.load(f)
        self.assertTrue(seeded["hasCompletedOnboarding"])
        self.assertEqual(seeded["lastOnboardingVersion"], "9.9.9")  # version copied
        self.assertNotIn("secretProjects", seeded)   # only onboarding keys, no host state
        self.assertEqual(seeded["projects"]["/trusted/repo"],
                         {"hasTrustDialogAccepted": True, "projectOnboardingSeenCount": 1})
        self.assertNotIn("/untrusted/repo", seeded["projects"])   # only trusted paths
        self.assertNotIn("lastCost", seeded["projects"]["/trusted/repo"])  # no session state
        # the intended workdir's trust is SYNTHESIZED (exact-match key the dialog
        # needs; no ref carries it — the codex-3 stall's root cause)
        self.assertEqual(seeded["projects"][os.path.realpath(self.tmp)],
                         {"hasTrustDialogAccepted": True, "projectOnboardingSeenCount": 1})
        # bypass acceptance lands in settings.json (CC 2.1.216), not .claude.json
        with open(os.path.join(d, "claude", "settings.json")) as f:
            st = json.load(f)
        self.assertTrue(st["skipDangerousModePermissionPrompt"])
        with open(p, "w") as f:
            json.dump({"mine": True}, f)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": host}):
            seat._write_launch_assets("codex", d)
        with open(p) as f:
            self.assertEqual(json.load(f), {"mine": True})  # never clobbered

    def test_fresh_instance_seeds_freshest_same_family_feature_cache(self):
        """A new instance inherits ONLY the freshest complete cache carrying
        Claude's real deferred-tool gate. Cache timestamp, not unrelated file
        mtime, owns freshness; a newer partial map is ignored."""
        host = os.path.join(self.tmp, "host-feature-source")
        os.makedirs(host)
        with open(os.path.join(host, ".claude.json"), "w") as f:
            json.dump({"hasCompletedOnboarding": True}, f)

        now = int(time.time() * 1000)
        parent = os.path.join(seat.seat_dir("codex"), "claude", ".claude.json")
        sibling = os.path.join(seat._instance_dir("codex", "codex-3"),
                               "claude", ".claude.json")
        partial = os.path.join(seat._instance_dir("codex", "codex-4"),
                               "claude", ".claude.json")
        rows = (
            (parent, _warm_features(parent=True), [], now - 2000,
             {"oauthAccount": {"email": "must-not-cross@example.com"}}),
            (sibling, _warm_features(winner=True),
             ["exp-a"], now - 1000,
             {"lastSessionId": "must-not-cross"}),
            (partial, {"unrelated": True}, [], now, {}),
        )
        for p, features, experiments, fetched, extra in rows:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                json.dump(dict(extra,
                               cachedGrowthBookFeatures=features,
                               cachedExperimentFeatures=experiments,
                               cachedGrowthBookFeaturesAt=fetched), f)
        os.utime(parent, (300, 300))
        os.utime(sibling, (100, 100))

        target = seat._instance_dir("codex", "codex-2")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": host}), \
                contextlib.redirect_stderr(err):
            seat._write_launch_assets("codex", target, seat="codex-2",
                                      workdir=self.tmp)
        with open(os.path.join(target, "claude", ".claude.json")) as f:
            seeded = json.load(f)
        self.assertEqual(seat._FEATURE_CACHE_GATE, "tengu_deferred_stub_tool")
        self.assertGreaterEqual(len(seeded["cachedGrowthBookFeatures"]), 100)
        self.assertTrue(seeded["cachedGrowthBookFeatures"]["winner"])
        self.assertNotIn("parent", seeded["cachedGrowthBookFeatures"])
        self.assertEqual(seeded["cachedExperimentFeatures"], ["exp-a"])
        self.assertEqual(seeded["cachedGrowthBookFeaturesAt"], now - 1000)
        self.assertNotIn("oauthAccount", seeded)
        self.assertNotIn("lastSessionId", seeded)
        self.assertNotIn("feature cache not seeded", err.getvalue())

    def test_cacheless_existing_instance_repairs_when_source_appears(self):
        """A first cacheless mint is not terminal: each launch warns until a
        valid sibling appears, then merges only the allowlisted tuple while
        preserving every seat-owned field."""
        host = os.path.join(self.tmp, "host-repair")
        os.makedirs(host)
        with open(os.path.join(host, ".claude.json"), "w") as f:
            json.dump({"hasCompletedOnboarding": True}, f)
        target = seat._instance_dir("codex", "codex-2")
        env = {"CLAUDE_CONFIG_DIR": host}
        first = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(first):
            seat._write_launch_assets("codex", target, seat="codex-2",
                                      workdir=self.tmp)
        state_path = os.path.join(target, "claude", ".claude.json")
        with open(state_path) as f:
            state = json.load(f)
        state.update({"mine": True, "lastSessionId": "seat-owned"})
        with open(state_path, "w") as f:
            json.dump(state, f)
        os.chmod(state_path, 0o600)
        self.assertIn("feature cache not seeded", first.getvalue())

        source = os.path.join(seat.seat_dir("codex"), "claude", ".claude.json")
        os.makedirs(os.path.dirname(source), exist_ok=True)
        now = int(time.time() * 1000)
        with open(source, "w") as f:
            json.dump({"cachedGrowthBookFeatures": _warm_features(),
                       "cachedExperimentFeatures": [],
                       "cachedGrowthBookFeaturesAt": now,
                       "oauthAccount": {"email": "must-not-cross@example.com"}}, f)
        second = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(second):
            seat._write_launch_assets("codex", target, seat="codex-2",
                                      workdir=self.tmp)
        with open(state_path) as f:
            repaired = json.load(f)
        self.assertTrue(repaired["mine"])
        self.assertEqual(repaired["lastSessionId"], "seat-owned")
        self.assertTrue(repaired["cachedGrowthBookFeatures"]
                        [seat._FEATURE_CACHE_GATE])
        self.assertEqual(repaired["cachedGrowthBookFeaturesAt"], now)
        self.assertEqual(stat.S_IMODE(os.stat(state_path).st_mode), 0o600)
        self.assertNotIn("oauthAccount", repaired)
        self.assertNotIn("feature cache not seeded", second.getvalue())

    def test_cacheless_instance_rejects_poisoned_and_escaped_sources(self):
        """Partial, stale, future, non-finite, and symlink-escaped caches cannot
        suppress the loud warning or cross the same-family trust boundary."""
        host = os.path.join(self.tmp, "host-without-cache")
        os.makedirs(host)
        with open(os.path.join(host, ".claude.json"), "w") as f:
            json.dump({"hasCompletedOnboarding": True}, f)
        now = int(time.time() * 1000)
        bad = (
            ({"unrelated": True}, now),
            ({seat._FEATURE_CACHE_GATE: True}, now),  # shallow sticky stub
            (_warm_features(), now - seat._FEATURE_CACHE_MAX_AGE_MS - 1),
            (_warm_features(),
             now + seat._FEATURE_CACHE_FUTURE_SKEW_MS + 60_000),
            (_warm_features(), float("nan")),
        )
        for i, (features, fetched) in enumerate(bad, 3):
            p = os.path.join(seat._instance_dir("codex", "codex-%d" % i),
                             "claude", ".claude.json")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                json.dump({"cachedGrowthBookFeatures": features,
                           "cachedExperimentFeatures": [],
                           "cachedGrowthBookFeaturesAt": fetched}, f)

        foreign = os.path.join(seat.seat_dir("kimi"), "claude", ".claude.json")
        os.makedirs(os.path.dirname(foreign), exist_ok=True)
        with open(foreign, "w") as f:
            json.dump({"cachedGrowthBookFeatures": _warm_features(foreign=True),
                       "cachedExperimentFeatures": [],
                       "cachedGrowthBookFeaturesAt": now}, f)
        escaped = os.path.join(seat._instance_dir("codex", "codex-8"),
                               "claude", ".claude.json")
        os.makedirs(os.path.dirname(escaped), exist_ok=True)
        os.symlink(foreign, escaped)

        target = seat._instance_dir("codex", "codex-2")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": host}), \
                contextlib.redirect_stderr(err):
            seat._write_launch_assets("codex", target, seat="codex-2",
                                      workdir=self.tmp)
        with open(os.path.join(target, "claude", ".claude.json")) as f:
            seeded = json.load(f)
        for key in seat._FEATURE_CACHE_KEYS:
            self.assertNotIn(key, seeded)
        self.assertIn("WARNING — feature cache not seeded", err.getvalue())
        self.assertIn(seat._FEATURE_CACHE_GATE, err.getvalue())
        self.assertIn("Monitor", err.getvalue())

    def test_launch_line_room_homing(self):
        """Seat presets clear ambient homing, then bake either an explicit room
        or a project-derived room with truthful provenance into launch.sh."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        bare = seat.launch_line("codex")
        self.assertNotIn("HELM_CHAT_ROOM=", bare)
        self.assertIn("-u HELM_CHAT_ROOM", bare)
        self.assertIn("-u MELD_CHAT_ROOM", bare)
        self.assertIn("-u HELM_CHAT_ROOM_SOURCE", bare)
        self.assertIn("-u MELD_CHAT_ROOM_SOURCE", bare)
        line = seat.launch_line("codex", room="team-x")
        self.assertIn("HELM_CHAT_ROOM=team-x", line)
        self.assertNotIn("HELM_CHAT_ROOM_SOURCE=", line)
        self.assertIn("HELM_CHAT_NAME=codex", line)          # identity intact
        self.assertIn("HELM_CELL_BIN=" + seat.DREGG_SIGNER_DEFAULT, line)
        self.assertIn("HELM_CELL_PROFILE=codex", line)       # signing intact
        self.assertIn("DREGG_PROFILE=codex", line)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["launch", "codex", "--room", "team-x"])
        self.assertEqual(rc, 0)
        self.assertIn("HELM_CHAT_ROOM=team-x", out.getvalue())
        with open(os.path.join(seat.seat_dir("codex"), "launch.sh")) as f:
            preset = f.read()
        self.assertIn("HELM_CHAT_ROOM=team-x", preset)
        self.assertNotIn("HELM_CHAT_ROOM_SOURCE=", preset)
        out = io.StringIO()
        with mock.patch.object(seat, "_resolve_homing",
                               return_value=("helm", "derived")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(seat.cmd_seat(["launch", "codex"]), 0)
        self.assertIn("HELM_CHAT_ROOM=helm", out.getvalue())
        self.assertIn("HELM_CHAT_ROOM_SOURCE=derived", out.getvalue())
        with open(os.path.join(seat.seat_dir("codex"), "launch.sh")) as f:
            preset = f.read()
        self.assertIn("HELM_CHAT_ROOM_SOURCE=derived", preset)

    def test_homing_resolution_precedence_and_source(self):
        with mock.patch.dict(os.environ, {
                "HELM_CHAT_ROOM": "project-a",
                "HELM_CHAT_ROOM_SOURCE": "derived"}):
            self.assertEqual(seat._resolve_homing(), ("project-a", "derived"))
            self.assertEqual(seat._resolve_homing("main"), ("main", None))
        with mock.patch.dict(os.environ, {
                "MELD_CHAT_ROOM": "legacy-project",
                "MELD_CHAT_ROOM_SOURCE": "derived"}):
            self.assertEqual(seat._resolve_homing(),
                             ("legacy-project", "derived"))
        with mock.patch.dict(os.environ, {
                "HELM_CHAT_ROOM": "explicit-new",
                "MELD_CHAT_ROOM_SOURCE": "derived"}):
            self.assertEqual(seat._resolve_homing(),
                             ("explicit-new", None))
        with mock.patch("helm.seats.derive_home_room", return_value="helm"):
            self.assertEqual(seat._resolve_homing(), ("helm", "derived"))

    def test_seat_subprocess_env_overrides_ambient_owner_signer_identity(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.dict(os.environ, {
                "HELM_CHAT_NAME": "owner", "HELM_CELL_PROFILE": "owner",
                "DREGG_PROFILE": "owner", "HELM_CELL_BIN": "/tmp/legacy-signer"}):
            env = seat._seat_env("codex", os.path.join(self.tmp, "smoke"))
        self.assertEqual(env["HELM_CHAT_NAME"], "codex")
        self.assertEqual(env["HELM_CELL_PROFILE"], "codex")
        self.assertEqual(env["DREGG_PROFILE"], "codex")
        self.assertEqual(env["HELM_CELL_BIN"], seat.DREGG_SIGNER_DEFAULT)

    # -- the scrub guard ----------------------------------------------------
    def test_scrub_env_strips_the_triple(self):
        env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
               "ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_API_KEY": "sk-x",
               "PATH": "/usr/bin", "HOME": "/home/x"}
        scrubbed = seat.scrub_env(env)
        for v in seat.SCRUB_VARS:
            self.assertNotIn(v, scrubbed)
        self.assertEqual(scrubbed["PATH"], "/usr/bin")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-x")  # input untouched
        for v in seat.SCRUB_VARS:
            self.assertIn("-u " + v, seat.scrub_prefix())

    # -- up: double-start refusal + missing-binary line ---------------------
    def test_double_start_refused_no_spawn(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        # a live pid + its MATCHING birth identity = a verifiably-running proxy
        # (the pidfile shape `_up` writes post-fix; a bare pid is now refused
        # as unauthenticated, so this record must carry the identity to count).
        live = os.getpid()
        with open(os.path.join(seat.seat_dir("codex"), "proxy.pid"), "w") as f:
            f.write("%d %s\n" % (live, seat._pid_identity(live)))
        booby = seat.subprocess.Popen
        seat.subprocess.Popen = lambda *a, **k: self.fail("Popen called on double-start")
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = seat.cmd_seat(["up", "codex"])
        finally:
            seat.subprocess.Popen = booby
        self.assertEqual(rc, 1)
        self.assertIn("already running", err.getvalue())

    def test_up_missing_binary_points_at_doctor(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        os.environ["HELM_PROXY_BIN"] = os.path.join(self.tmp, "no-such-binary")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["up", "codex"])
        self.assertEqual(rc, 1)
        self.assertIn("helm seat doctor", err.getvalue())

    def test_up_without_seat_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["up", "codex"])
        self.assertEqual(rc, 1)
        self.assertIn("helm seat add codex", err.getvalue())

    # -- status -------------------------------------------------------------
    def test_status_shows_liveness_and_expiry(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["status"])
        self.assertEqual(rc, 0)
        row = out.getvalue()
        self.assertIn("codex", row)
        self.assertIn("proxy down", row)
        self.assertIn("fake@example.com", row)
        self.assertIn("valid until", row)

    def test_status_no_seats(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("no seats yet", out.getvalue())


class SeatBornWiredTest(unittest.TestCase):
    """G-seatlaunch-installs: add/launch leave the seat's claude dir carrying
    the delivery lane + beacon permit; launch refreshes stale assets.
    Borrows SeatTest's setUp/helpers WITHOUT subclassing it (a subclass
    would silently re-run the whole parent suite twice)."""

    setUp = SeatTest.setUp
    tearDown = SeatTest.tearDown
    _plant = SeatTest._plant
    _add = SeatTest._add

    def _settings(self):
        p = os.path.join(seat.seat_dir("codex"), "claude", "settings.json")
        with open(p) as f:
            return json.load(f)

    def test_add_wires_delivery_lane_and_beacon_permit(self):
        self._plant("home-a")
        rc, _out, err = self._add()
        self.assertEqual(rc, 0, err)
        from helm import hooks
        got = self._settings()
        for s in hooks.DELIVERY_SPECS:
            self.assertIn(hooks.spec_command(s),
                          hooks._hook_cmds(got, s["event"]))
        for rule in hooks.PERMIT_RULES:
            self.assertIn(rule, got["permissions"]["allow"])
        self.assertEqual(hooks._hook_cmds(got, "UserPromptSubmit"), [])

    def test_launch_refreshes_hooks_and_identity_stdout_stays_pure(self):
        """The live kimi shape: a seat minted before HELM_CHAT_NAME/hook
        install existed — launch retrofits both; stdout is ONLY the line."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        d = seat.seat_dir("codex")
        os.remove(os.path.join(d, "claude", "settings.json"))   # the dark seat
        with open(os.path.join(d, "launch.sh"), "w") as f:
            f.write("#!/bin/sh\n# stale — pre-identity\nexec claude\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["launch", "codex"])
        self.assertEqual(rc, 0)
        line = out.getvalue().strip()
        self.assertTrue(line.startswith("ANTHROPIC_AUTH_TOKEN=$(cat "), line)
        self.assertIn("; export ANTHROPIC_AUTH_TOKEN; env -u ANTHROPIC_API_KEY", line)
        self.assertNotIn("\n", line)                 # pasteable — one line
        got = self._settings()                       # hooks are back
        from helm import hooks
        self.assertIn("chat join --hook-json",
                      " ".join(hooks._hook_cmds(got, "SessionStart")))
        with open(os.path.join(d, "launch.sh")) as f:
            self.assertIn("HELM_CHAT_NAME=codex", f.read())   # identity restored
        self.assertIn("wired for fleet delivery", err.getvalue())


class SeatMultiTest(unittest.TestCase):
    """--multi (0.2 mixed-model fleets, premise multimodel-one-cc-proven-
    per-agent-frontmatter-no-fork): the launch line/env DROP the
    CLAUDE_CODE_SUBAGENT_MODEL blunt pin (it overrides per-agent frontmatter),
    probe agents with per-model `model:` frontmatter are minted, and the smoke
    gate grows a conductor-log-verified fan-out leg. Hermetic: no proxy, no
    claude, no network. Borrows SeatTest's fixtures without subclassing."""

    setUp = SeatTest.setUp
    tearDown = SeatTest.tearDown
    _plant = SeatTest._plant
    _add = SeatTest._add

    def test_launch_line_multi_drops_subagent_pin_only(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        line = seat.launch_line("codex", multi=True)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", line)  # the proven law
        base = seat.launch_line("codex")
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-sol", base)  # default intact
        # everything else is byte-identical: removing the pin is the ONLY delta
        self.assertEqual(base.replace(" CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-sol", ""),
                         line)
        # parent --model still rides; identity + scrub + ctx env intact
        self.assertIn("--model gpt-5.6-sol", line)
        # launch_line itself is the env/claude command (no token, no export —
        # the export prefix is added by the stdout print / launch.sh caller)
        self.assertTrue(line.startswith("env -u ANTHROPIC_API_KEY "), line)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", line)   # no env NAME=value secret
        self.assertIn("HELM_CHAT_NAME=codex", line)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=360000", line)

    def test_probe_agents_names_and_frontmatter(self):
        probes = seat.probe_agents("codex")
        self.assertEqual(probes, [("helm-probe-gpt-5-6-sol", "gpt-5.6-sol"),
                                  ("helm-probe-gpt-5-6-terra", "gpt-5.6-terra")])
        self.assertEqual(seat.probe_agents("kimi"),
                         [("helm-probe-kimi-k3", "kimi-k3")])
        cdir = os.path.join(self.tmp, "cfg")
        got = seat._mint_probe_agents(cdir, "codex")
        self.assertEqual(got, probes)
        for name, model in probes:
            with open(os.path.join(cdir, "agents", name + ".md")) as f:
                body = f.read()
            self.assertTrue(body.startswith("---\n"))
            self.assertIn("name: %s\n" % name, body)
            self.assertIn("model: %s\n" % model, body)  # the wire-riding pin
        # re-mint overwrites in place, never accretes
        seat._mint_probe_agents(cdir, "codex")
        self.assertEqual(len(os.listdir(os.path.join(cdir, "agents"))), 2)

    def test_launch_verb_multi_mints_agents_and_launch_sh(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["launch", "codex", "--multi"])
        self.assertEqual(rc, 0)
        line = out.getvalue().strip()
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", line)
        d = seat.seat_dir("codex")
        agents = sorted(os.listdir(os.path.join(d, "claude", "agents")))
        self.assertEqual(agents, ["helm-probe-gpt-5-6-sol.md",
                                  "helm-probe-gpt-5-6-terra.md"])
        with open(os.path.join(d, "launch.sh")) as f:
            sh = f.read()
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", sh)  # preset matches
        # a plain launch afterwards restores the pinned single-model preset
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(seat.cmd_seat(["launch", "codex"]), 0)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-sol", out.getvalue())
        with open(os.path.join(d, "launch.sh")) as f:
            self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-sol", f.read())

    def test_resume_preserves_multi_shape(self):
        """A --multi seat re-minted on resume must NOT regain the pin — the pin's
        absence is the only marker, so _multi_from_launch reads it back and the
        re-mint stays pinless. Guards the mixed-model fleet against silent
        collapse to single-model on any resume/env-refresh."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        d = seat.seat_dir("codex")
        launch_sh = os.path.join(d, "launch.sh")
        # multi mint: pinless launch.sh, detector reads True
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(seat.cmd_seat(["launch", "codex", "--multi"]), 0)
        self.assertTrue(seat._multi_from_launch(launch_sh))
        # the resume re-mint path (env refresh) must keep it pinless
        seat._write_launch_assets(
            "codex", d, seat._room_from_launch(launch_sh), "codex",
            multi=seat._multi_from_launch(launch_sh))
        with open(launch_sh) as f:
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", f.read())
        # a pinned (non-multi) seat reads False and re-mints WITH the pin
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(seat.cmd_seat(["launch", "codex"]), 0)
        self.assertFalse(seat._multi_from_launch(launch_sh))

    def test_resume_roomless_launch_falls_back_to_project_room(self):
        """The kimi room-drop regression (2026-07-23): a launch.sh minted
        WITHOUT a room stamp (HELM_CHAT_ROOM-less) made _resume recover
        (None, None) and re-mint room=None — the SessionStart join then
        defaulted the seat to #main, silently dropping it out of its project
        room. The resume seam now falls back to seats.resolve_homing(cwd), so
        the cwd-derived project room is preserved. Pin the two halves: (1)
        _homing_from_launch really is empty on a room-less launch.sh (the
        precondition), and (2) resolve_homing recovers the project room for
        the seat's cwd — the value _resume now re-mints."""
        from helm import seats
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        d = seat.seat_dir("codex")
        launch_sh = os.path.join(d, "launch.sh")
        # mint a launch, then strip its room stamp: the regression's
        # precondition (a launch.sh carrying NO HELM_CHAT_ROOM= line)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(seat.cmd_seat(["launch", "codex"]), 0)
        with open(launch_sh) as f:
            txt = f.read()
        import re as _re
        txt = _re.sub(r"\s*HELM_CHAT_ROOM(_SOURCE)?=\S+", "", txt)
        with open(launch_sh, "w") as f:
            f.write(txt)
        room, room_source = seat._homing_from_launch(launch_sh)
        self.assertIsNone(room)
        self.assertIsNone(room_source)
        # the resume fallback recovers the cwd-derived project room, not None
        # (_git_project needs a true git root — resolve this repo dynamically)
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        fb_room, fb_source = seats.resolve_homing(cwd=repo_root)
        self.assertIsNotNone(fb_room)
        self.assertEqual(fb_source, "derived")
        # and that room mints HELM_CHAT_ROOM into the relaunch line (not #main)
        line = seat.launch_line("codex", room=fb_room, room_source=fb_source)
        self.assertIn("HELM_CHAT_ROOM=%s" % fb_room, line)

    def test_seat_env_multi_no_pin_and_no_inherited_pin(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.dict(os.environ,
                             {"CLAUDE_CODE_SUBAGENT_MODEL": "ambient-pin"}):
            env = seat._seat_env("codex", os.path.join(self.tmp, "smoke"),
                                 multi=True)
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
            # default (pinned) shape: the seat's OWN pin, never the ambient one
            env = seat._seat_env("codex", os.path.join(self.tmp, "smoke"))
            self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "gpt-5.6-sol")

    def test_smoke_multi_leg_skips_single_model_family(self):
        """kimi has one probe model — the fan-out leg SKIPs loudly and passes,
        never launching a router or a claude."""
        smoke_dir = os.path.join(self.tmp, "smoke-kimi")
        os.makedirs(smoke_dir)
        booby = seat.subprocess.run
        seat.subprocess.run = lambda *a, **k: self.fail("claude launched on SKIP")
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                passed = seat._smoke_multi_leg("kimi", seat.FAMILIES["kimi"],
                                               smoke_dir, "12345")
        finally:
            seat.subprocess.run = booby
        self.assertTrue(passed)
        self.assertIn("SKIP", out.getvalue())
        self.assertIn("probe_models", out.getvalue())

    def test_smoke_multi_leg_verifies_against_conductor_log(self):
        """The fan-out leg trusts the WIRE, not the reply: a fake claude whose
        stdout carries both markers passes only when the router's conductor
        log also saw both probe models (we replay the request shapes through
        the leg's own router); a marker-perfect reply with a one-model log
        FAILS (the blunt-pin regression shape)."""
        import http.client
        import subprocess
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        smoke_dir = os.path.join(self.tmp, "smoke-codex")
        os.makedirs(smoke_dir)
        calls = {}

        def fake_run(cmd, env=None, **kw):
            calls["cmd"], calls["env"] = cmd, env
            # drive the leg's OWN router exactly as the proven fan-out would:
            # one request per model the fake fleet puts on the wire
            port = int(env["ANTHROPIC_BASE_URL"].rsplit(":", 1)[1])
            for m in calls.get("wire_models", []):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    conn.request("POST", "/v1/messages",
                                 body=json.dumps({"model": m}).encode(),
                                 headers={"Content-Type": "application/json"})
                    conn.getresponse().read()
                finally:
                    conn.close()
            return subprocess.CompletedProcess(
                cmd, 0, stdout="helm-seat-multi-a-777 helm-seat-multi-b-777", stderr="")

        # a tiny fake CLIProxyAPI so routed requests land somewhere real
        fake_proxy_seen = []

        class _P(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake_proxy_seen.append(self.rfile.read(n))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), _P)
        proxy.daemon_threads = True
        threading.Thread(target=proxy.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()
        old_port = seat.FAMILIES["codex"]["port"]
        seat.FAMILIES["codex"]["port"] = proxy.server_address[1]
        try:
            # leg 1: wire carries BOTH probe models -> PASS
            calls["wire_models"] = ["gpt-5.6-sol", "gpt-5.6-terra"]
            with mock.patch.object(seat.subprocess, "run", fake_run):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    passed = seat._smoke_multi_leg(
                        "codex", seat.FAMILIES["codex"], smoke_dir, "777")
            self.assertTrue(passed, out.getvalue())
            self.assertIn("multi", out.getvalue())
            self.assertIn("PASS", out.getvalue())
            # the leg ran claude with NO pin and Task allowed, through its router
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", calls["env"])
            self.assertIn("Task", calls["cmd"])
            self.assertIn("helm-probe-gpt-5-6-sol",
                          calls["cmd"][calls["cmd"].index("-p") + 1])
            # probe agents were minted into the smoke config dir
            self.assertTrue(os.path.exists(os.path.join(
                smoke_dir, "agents", "helm-probe-gpt-5-6-terra.md")))
            # leg 2: reply perfect but the wire saw ONE model -> FAIL
            calls["wire_models"] = ["gpt-5.6-sol"]
            with mock.patch.object(seat.subprocess, "run", fake_run):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    passed = seat._smoke_multi_leg(
                        "codex", seat.FAMILIES["codex"], smoke_dir, "777")
            self.assertFalse(passed)
            self.assertIn("conductor log saw", out.getvalue())
        finally:
            seat.FAMILIES["codex"]["port"] = old_port
            proxy.shutdown()
            proxy.server_close()

    def test_usage_names_multi(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat.cmd_seat([])
        self.assertEqual(rc, 2)
        self.assertIn("--multi", err.getvalue())


class SeatEnsureTest(unittest.TestCase):
    """doctor --ensure (the proxy watchdog, codex-2 silent-starvation class):
    supervise every minted family+instance proxy — healthy rows pass through,
    dead/wedged rows respawn via the LANDED _up (never a second spawn path),
    and any row the watchdog cannot PROVE healthy surfaces UNKNOWN (rc 2),
    never a silent down/up. Hermetic: primitives mocked, no real proxy.
    Borrows SeatTest's fixtures without subclassing."""

    setUp = SeatTest.setUp
    tearDown = SeatTest.tearDown
    _plant = SeatTest._plant
    _add = SeatTest._add

    def _row(self, family="codex", seat_name="codex"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            verdict = seat._ensure_row(family, seat_name)
        return verdict

    # -- the acceptance path: a DOWN proxy is respawned ---------------------
    def test_ensure_respawns_dead_proxy(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        # no pidfile -> down; a successful _up flips the row to live+open so the
        # post-respawn probe proves healthy (the seq-flip pattern: the initial
        # probe must read DOWN, only the re-probe after _up reads live).
        seq = {"live": False, "open": False}
        up_calls = []

        def fake_up(f, quiet=False, seat=None):
            up_calls.append((f, seat))
            seq.update(live=True, open=True)
            return 0

        with mock.patch.object(seat, "_proxy_pid_record", return_value=None), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 4321 if seq["live"] else None), \
                mock.patch.object(seat, "_port_open",
                                  lambda p, timeout=0.5: seq["open"]), \
                mock.patch.object(seat, "_up", fake_up), \
                mock.patch.object(seat, "_down") as down:
            label, state, detail = self._row()
        self.assertEqual(label, "codex")
        self.assertEqual(state, "respawned")
        self.assertEqual(up_calls, [("codex", "codex")])
        down.assert_not_called()          # nothing live to signal away

    def test_ensure_healthy_row_untouched(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 4321, "identity": "x"}), \
                mock.patch.object(seat, "_running_pid", return_value=4321), \
                mock.patch.object(seat, "_port_open", return_value=True), \
                mock.patch.object(seat, "_up") as up, \
                mock.patch.object(seat, "_down") as down:
            label, state, detail = self._row()
        self.assertEqual(state, "healthy")
        up.assert_not_called()
        down.assert_not_called()

    def test_ensure_wedged_proxy_signaled_then_respawned(self):
        # live verified pid but the port does not answer -> _down clears it,
        # then _up brings a fresh one up.
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        seq = {"live": True, "open": False}
        down_calls, up_calls = [], []

        def fake_up(f, quiet=False, seat=None):
            up_calls.append((f, seat))
            seq.update(live=True, open=True)   # fresh proxy up + answering
            return 0

        def fake_down(f, seat=None):
            down_calls.append((f, seat))
            seq.update(live=False, open=False)
            return 0

        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 4321, "identity": "x"}), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 4321 if seq["live"] else None), \
                mock.patch.object(seat, "_port_open",
                                  lambda p, timeout=0.5: seq["open"]), \
                mock.patch.object(seat, "_proxy_age_s",
                                  return_value=120.0), \
                mock.patch.object(seat, "_up", fake_up), \
                mock.patch.object(seat, "_down", fake_down):
            label, state, detail = self._row()
        self.assertEqual(state, "respawned")
        self.assertEqual(down_calls, [("codex", "codex")])
        self.assertEqual(len(up_calls), 1)

    def test_ensure_starting_proxy_within_grace_is_never_killed(self):
        # THE fable adversarial MED: a HEALTHY just-launched proxy still binding
        # its port reads 'live pid + port not answering' -> the OLD code SIGTERMed
        # it. Within the startup-grace window it must be left alone (unknown /
        # STARTING), _down NEVER called — cron firing in the boot window must not
        # churn kill->respawn->kill.
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 4321, "identity": "x"}), \
                mock.patch.object(seat, "_running_pid", return_value=4321), \
                mock.patch.object(seat, "_port_open", return_value=False), \
                mock.patch.object(seat, "_proxy_age_s", return_value=3.0), \
                mock.patch.object(seat, "_up") as up, \
                mock.patch.object(seat, "_down") as down:
            label, state, detail = self._row()
        self.assertEqual(state, "unknown")
        self.assertIn("STARTING", detail)
        down.assert_not_called()          # the whole point: never SIGTERM a boot
        up.assert_not_called()            # nor double-spawn over it

    def test_ensure_grace_boundary_old_proxy_still_wedged(self):
        # past grace AND still not answering -> genuinely wedged -> signal+respawn
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        seq = {"live": True}
        down_calls = []

        def fake_down(f, seat=None):
            down_calls.append((f, seat))
            seq["live"] = False
            return 0

        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 4321, "identity": "x"}), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 4321 if seq["live"] else None), \
                mock.patch.object(seat, "_port_open", return_value=False), \
                mock.patch.object(seat, "_proxy_age_s",
                                  return_value=seat._ENSURE_STARTUP_GRACE_S + 1), \
                mock.patch.object(seat, "_up", return_value=1), \
                mock.patch.object(seat, "_down", fake_down):
            label, state, detail = self._row()
        self.assertEqual(down_calls, [("codex", "codex")])
        self.assertEqual(state, "unknown")   # _up refused -> unresolved -> UNKNOWN

    def test_ensure_unverifiable_live_pid_is_unknown_never_touched(self):
        # a record whose pid is ALIVE but fails identity verification (a reused
        # pid now owned by a stranger, or a legacy bare-pid proxy): the watchdog
        # refuses to signal it AND refuses to respawn over a live foreign
        # listener — UNKNOWN for a human. (The ds4pro-SIGKILL fix: only an
        # ALIVE-but-unverifiable pid refuses; a DEAD one respawns.)
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 4321, "identity": None}), \
                mock.patch.object(seat, "_running_pid", return_value=None), \
                mock.patch.object(seat, "_pid_alive", return_value=True), \
                mock.patch.object(seat, "_up") as up, \
                mock.patch.object(seat, "_down") as down:
            label, state, detail = self._row()
        self.assertEqual(state, "unknown")
        self.assertIn("unverifiable", detail)
        up.assert_not_called()
        down.assert_not_called()

    def test_ensure_stale_dead_pidfile_respawns(self):
        # the codex-2 silent-starvation case: a well-formed identity record
        # whose pid has since DIED. _running_pid fails closed to None (corpse),
        # but the record still parses — the watchdog must NOT read it as
        # 'unverifiable, refuse'; the pid is dead, so this is a plain DOWN ->
        # respawn via _up.
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        seq = {"live": False}
        up_calls = []

        def fake_up(f, quiet=False, seat=None):
            up_calls.append((f, seat))
            seq["live"] = True
            return 0

        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 9999, "identity": "proc:x"}), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 5555 if seq["live"] else None), \
                mock.patch.object(seat, "_pid_alive", return_value=False), \
                mock.patch.object(seat, "_port_open",
                                  lambda p, timeout=0.5: seq["live"]), \
                mock.patch.object(seat, "_up", fake_up), \
                mock.patch.object(seat, "_down") as down:
            label, state, detail = self._row()
        self.assertEqual(state, "respawned")
        self.assertEqual(up_calls, [("codex", "codex")])
        down.assert_not_called()          # corpse already gone; nothing to signal

    def test_ensure_respawn_failure_is_unknown(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_proxy_pid_record", return_value=None), \
                mock.patch.object(seat, "_running_pid", return_value=None), \
                mock.patch.object(seat, "_port_open", return_value=False), \
                mock.patch.object(seat, "_up", return_value=1):
            label, state, detail = self._row()
        self.assertEqual(state, "unknown")
        self.assertIn("respawn failed", detail)

    def test_ensure_concurrent_up_loser_reads_healthy_not_unknown(self):
        # fable LOW: a seat launching in the same instant wins the flock; our
        # _up returns rc 1 ('already running'). That is NOT a respawn failure —
        # the row is now HEALTHY under the winner. Re-probe must recover it.
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        seq = {"live": False}   # winner flips this when it steals the start

        def losing_up(f, quiet=False, seat=None):
            seq["live"] = True   # the concurrent winner's proxy is now up
            return 1             # ... so OUR _up loses the race

        with mock.patch.object(seat, "_proxy_pid_record", return_value=None), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 7777 if seq["live"] else None), \
                mock.patch.object(seat, "_port_open",
                                  lambda p, timeout=0.5: seq["live"]), \
                mock.patch.object(seat, "_up", losing_up), \
                mock.patch.object(seat, "_down"):
            label, state, detail = self._row()
        self.assertEqual(state, "healthy")
        self.assertIn("concurrent starter won", detail)

    def test_ensure_rc2_on_any_unknown_row(self):
        # one healthy family + one wedged respawn that fails -> rc 2 overall
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_proxy_pid_record", return_value=None), \
                mock.patch.object(seat, "_running_pid", return_value=None), \
                mock.patch.object(seat, "_port_open", return_value=False), \
                mock.patch.object(seat, "_up", return_value=1):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = seat._ensure([])
        self.assertEqual(rc, 2)

    def test_doctor_ensure_dispatches_and_guards_tail(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_ensure", return_value=0) as ens:
            rc = seat.cmd_seat(["doctor", "--ensure"])
        self.assertEqual(rc, 0)
        ens.assert_called_once()
        # junk after --ensure still refuses BEFORE any work
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["doctor", "--ensure", "--bogus"])
        self.assertEqual(rc, 2)


class SeatCpuCanaryTest(unittest.TestCase):
    """The proxy-CPU canary (struggling-backend leading indicator): htop showed
    cli-proxy-api pids pegged at 152%/90.6% while healthy siblings idled ~0% —
    sustained-high CPU on a proxy = a thrashing backend BEFORE it goes silent.
    Classification is windowed, never a point: stored-prior span when one
    exists, else a double-read; startup bursts are grace; unreadable /proc is
    UNKNOWN, not OK. Borrows SeatTest's fixtures without subclassing."""

    def setUp(self):
        SeatTest.setUp(self)
        self._cpu_dir_prev = os.environ.get("HELM_PROXY_CPU_DIR")
        os.environ["HELM_PROXY_CPU_DIR"] = os.path.join(self.tmp, "cpu-canary")

    def tearDown(self):
        if self._cpu_dir_prev is None:
            os.environ.pop("HELM_PROXY_CPU_DIR", None)
        else:
            os.environ["HELM_PROXY_CPU_DIR"] = self._cpu_dir_prev
        SeatTest.tearDown(self)

    _plant = SeatTest._plant
    _add = SeatTest._add

    @staticmethod
    def _sample(jiffies, ts, age_s=300.0, clk=100):
        return {"jiffies": jiffies, "age_s": age_s, "clk": clk, "ts": ts}

    def _prior(self, seat_name, pid, jiffies, ts):
        path = seat._cpu_sample_path(seat_name)
        with open(path, "w") as f:
            json.dump({"pid": pid, "jiffies": jiffies, "ts": ts}, f)
        return path

    # -- the sample reader itself: real /proc, our own pid ------------------
    def test_proc_cpu_sample_reads_self(self):
        s = seat._proc_cpu_sample(os.getpid())
        self.assertIsNotNone(s)
        self.assertIsInstance(s["jiffies"], int)
        self.assertGreaterEqual(s["age_s"], 0.0)
        self.assertGreater(s["clk"], 0)

    def test_proc_cpu_sample_unreadable_is_none(self):
        # a pid that cannot exist: /proc/<huge>/stat is unreadable — None,
        # so the canary surfaces UNKNOWN, never a silent OK.
        self.assertIsNone(seat._proc_cpu_sample(2 ** 22 + 12345678))

    # -- classification: OK vs THRASHING vs UNKNOWN, sustained not spike ----
    def test_canary_unreadable_proc_is_unknown(self):
        with mock.patch.object(seat, "_proc_cpu_sample", return_value=None):
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "unknown")
        self.assertIn("unreadable", note)

    def test_canary_first_sight_double_reads_low_cpu_ok(self):
        # no stored prior: the canary takes TWO readings a window apart —
        # 10 jiffies over 1s at clk 100 = 10% CPU -> ok.
        samples = [self._sample(1000, 100.0), self._sample(1010, 101.0)]
        with mock.patch.object(seat, "_proc_cpu_sample",
                               side_effect=samples) as reads, \
                mock.patch.object(seat.time, "sleep") as slept:
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "ok")
        self.assertAlmostEqual(pct, 10.0)
        self.assertAlmostEqual(window, 1.0)
        self.assertEqual(reads.call_count, 2)
        slept.assert_called_once()        # the double-read IS the window

    def test_canary_first_sight_high_cpu_thrashing(self):
        # 150 jiffies over 1s at clk 100 = 150% (the htop evidence shape),
        # process 300s old (past grace) -> THRASHING.
        samples = [self._sample(1000, 100.0), self._sample(1150, 101.0)]
        with mock.patch.object(seat, "_proc_cpu_sample", side_effect=samples), \
                mock.patch.object(seat.time, "sleep"):
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "thrashing")
        self.assertAlmostEqual(pct, 150.0)
        self.assertIn("80", note)         # the threshold rides the note

    def test_canary_startup_burst_within_grace_is_ok(self):
        # same pegged reading but the process is 5s old: model-load burst,
        # not thrash — grace says OK and the note says why.
        samples = [self._sample(1000, 100.0, age_s=4.0),
                   self._sample(1150, 101.0, age_s=5.0)]
        with mock.patch.object(seat, "_proc_cpu_sample", side_effect=samples), \
                mock.patch.object(seat.time, "sleep"):
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "ok")
        self.assertIn("startup", note)

    def test_canary_sustained_via_stored_prior_no_sleep(self):
        # a stored prior 60s back turns the reading into a REAL sustain:
        # 9000 jiffies / clk 100 / 60s = 150% held for a minute -> THRASHING,
        # no in-process sleep (the cron cadence was the window), and the store
        # rolls forward so the next run measures the next span.
        self._prior("codex", 4321, 1000, 1000.0)
        now = self._sample(10000, 1060.0)
        with mock.patch.object(seat, "_proc_cpu_sample",
                               return_value=now) as reads, \
                mock.patch.object(seat.time, "sleep") as slept:
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "thrashing")
        self.assertAlmostEqual(pct, 150.0)
        self.assertAlmostEqual(window, 60.0)
        self.assertEqual(reads.call_count, 1)
        slept.assert_not_called()
        with open(seat._cpu_sample_path("codex")) as f:
            rolled = json.load(f)
        self.assertEqual(rolled, {"pid": 4321, "jiffies": 10000, "ts": 1060.0})

    def test_canary_prior_for_other_pid_falls_back_to_double_read(self):
        # the proxy respawned since the last sample: a prior keyed to the OLD
        # pid must never fabricate a window for the new one.
        self._prior("codex", 9999, 1000, 1000.0)
        samples = [self._sample(1000, 1060.0), self._sample(1005, 1061.0)]
        with mock.patch.object(seat, "_proc_cpu_sample",
                               side_effect=samples) as reads, \
                mock.patch.object(seat.time, "sleep") as slept:
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "ok")
        self.assertEqual(reads.call_count, 2)
        slept.assert_called_once()

    def test_canary_threshold_env_tunable(self):
        # HELM_PROXY_CPU_CANARY_PCT=95: a 90% reading (the htop 90.6% pid)
        # stays OK under a raised bar — the knob is live, not decorative.
        os.environ["HELM_PROXY_CPU_CANARY_PCT"] = "95"
        try:
            samples = [self._sample(1000, 100.0), self._sample(1090, 101.0)]
            with mock.patch.object(seat, "_proc_cpu_sample",
                                   side_effect=samples), \
                    mock.patch.object(seat.time, "sleep"):
                state, pct, _, _ = seat._cpu_canary("codex", "codex", 4321)
        finally:
            os.environ.pop("HELM_PROXY_CPU_CANARY_PCT", None)
        self.assertEqual(state, "ok")
        self.assertAlmostEqual(pct, 90.0)

    def test_canary_pid_vanishing_mid_sample_is_unknown(self):
        samples = [self._sample(1000, 100.0), None]
        with mock.patch.object(seat, "_proc_cpu_sample", side_effect=samples), \
                mock.patch.object(seat.time, "sleep"):
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "unknown")
        self.assertIn("vanished", note)

    # -- the surface: --ensure rows carry the canary, rc semantics ----------
    def _ensure_with(self, row, canary, args=(), pid=4321):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_ensure_row", return_value=row), \
                mock.patch.object(seat, "_running_pid", return_value=pid), \
                mock.patch.object(seat, "_cpu_canary", return_value=canary), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat._ensure(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_ensure_thrashing_row_surfaces_and_warns_rc1(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        rc, out, err = self._ensure_with(
            ("codex", "healthy", "pid 4321 port 8317"),
            ("thrashing", 152.0, 60.0, ">=80% threshold"))
        self.assertEqual(rc, 1)           # THRASHING is a WARN, not a page
        self.assertIn("THRASHING", out)
        self.assertIn("152", out)
        self.assertIn("struggling", err)

    def test_ensure_ok_canary_rc0_with_cpu_suffix(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        rc, out, err = self._ensure_with(
            ("codex", "healthy", "pid 4321 port 8317"),
            ("ok", 3.0, 60.0, ""))
        self.assertEqual(rc, 0)
        self.assertIn("HEALTHY", out)
        self.assertIn("cpu 3%", out)

    def test_ensure_cpu_unknown_is_never_ok_rc1(self):
        # requirement: an unreadable /proc is UNKNOWN, not OK — surfaced on
        # the row and WARN-carried in rc, while liveness stays healthy.
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        rc, out, err = self._ensure_with(
            ("codex", "healthy", "pid 4321 port 8317"),
            ("unknown", None, None, "unreadable /proc/4321/stat"))
        self.assertEqual(rc, 1)
        self.assertIn("cpu UNKNOWN", out)

    def test_ensure_liveness_unknown_still_rc2_over_warn(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_ensure_row",
                               return_value=("codex", "unknown", "x")), \
                mock.patch.object(seat, "_cpu_canary") as canary, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat._ensure([])
        self.assertEqual(rc, 2)           # the page outranks the warn
        canary.assert_not_called()        # never canary an unproven row

    def test_ensure_json_carries_canary_per_seat(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        rc, out, err = self._ensure_with(
            ("codex", "healthy", "pid 4321 port 8317"),
            ("thrashing", 152.0, 60.0, ">=80% threshold"),
            args=("--ensure", "--json"))
        self.assertEqual(rc, 1)
        doc = json.loads(out)
        self.assertEqual(doc["rc"], 1)
        self.assertEqual(doc["thrashing"], 1)
        row = doc["rows"][0]
        self.assertEqual(row["seat"], "codex")
        self.assertEqual(row["state"], "healthy")
        self.assertEqual(row["shown"], "thrashing")
        self.assertEqual(row["cpu"]["state"], "thrashing")
        self.assertAlmostEqual(row["cpu"]["pct"], 152.0)
        self.assertAlmostEqual(row["cpu"]["window_s"], 60.0)

    def test_doctor_json_without_ensure_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["doctor", "--json"])
        self.assertEqual(rc, 2)
        self.assertIn("--ensure", err.getvalue())

    def test_doctor_prints_canary_line(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        out = io.StringIO()
        with mock.patch.object(seat, "_proxy_bin", return_value=None), \
                mock.patch.object(seat, "_running_pid", return_value=4321), \
                mock.patch.object(
                    seat, "_cpu_canary",
                    return_value=("thrashing", 152.0, 60.0,
                                  ">=80% threshold")), \
                contextlib.redirect_stdout(out):
            seat._doctor([])
        self.assertIn("cpu canary", out.getvalue())
        self.assertIn("THRASHING", out.getvalue())


if __name__ == "__main__":
    unittest.main()
