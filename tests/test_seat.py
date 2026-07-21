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

from helm import seat


def _b64seg(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims):
    """header.payload.sig — enough structure for unverified payload decode."""
    return _b64seg({"alg": "RS256", "typ": "JWT"}) + "." + _b64seg(claims) + ".fake-sig"


class SeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seat-")
        self._env = {k: os.environ.get(k) for k in
                     ("HELM_HOME", "MELD_HOME", "HELM_PROXY_BIN",
                      "MELD_PROXY_BIN", "KIMI_API_KEY", "HELM_PROC",
                      "HELM_CHAT_DIR", "HELM_CODEX_HOMES_DIR",
                      "MELD_CODEX_HOMES_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ.pop("MELD_HOME", None)
        os.environ.pop("HELM_CODEX_HOMES_DIR", None)
        os.environ.pop("MELD_CODEX_HOMES_DIR", None)
        os.environ.pop("HELM_PROXY_BIN", None)
        os.environ.pop("MELD_PROXY_BIN", None)
        os.environ.pop("KIMI_API_KEY", None)  # hermetic: never the real key
        # launch's retrofit surface scans /proc + the roster — keep both tmp
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        self._codex_homes = seat.CODEX_HOMES
        seat.CODEX_HOMES = os.path.join(self.tmp, "codex-homes")
        os.makedirs(seat.CODEX_HOMES)

    def tearDown(self):
        seat.CODEX_HOMES = self._codex_homes
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
            '        alias: "kimi-k3"\n'))
        self.assertNotIn("auth-dir", cfg)

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
        self.assertIn('base-url: "https://api.kimi.com/coding/v1"', cfg)
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
        self.assertIn("--dangerously-skip-permissions", line)  # canonical seat
        self.assertTrue(line.endswith(
            "claude --dangerously-skip-permissions --model kimi-k3"))
        self.assertNotIn("fake-kimi-key-for-tests", line)  # key never rides

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
        self.assertTrue(line.startswith("env -u ANTHROPIC_API_KEY "))
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8317", line)
        self.assertIn("ANTHROPIC_AUTH_TOKEN=" + token, line)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-sol", line)
        self.assertIn("CLAUDE_CONFIG_DIR=" + os.path.join(seat.seat_dir("codex"), "claude"), line)
        self.assertIn("HELM_CHAT_NAME=codex", line)   # stable seat identity
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
        with open(os.path.join(seat.seat_dir("codex"), "proxy.pid"), "w") as f:
            f.write("%d\n" % os.getpid())  # an alive pid: this test process
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
        self.assertTrue(line.startswith("env -u ANTHROPIC_API_KEY"), line)
        self.assertNotIn("\n", line)                 # pasteable — one line
        got = self._settings()                       # hooks are back
        from helm import hooks
        self.assertIn("chat join --hook-json",
                      " ".join(hooks._hook_cmds(got, "SessionStart")))
        with open(os.path.join(d, "launch.sh")) as f:
            self.assertIn("HELM_CHAT_NAME=codex", f.read())   # identity restored
        self.assertIn("wired for fleet delivery", err.getvalue())


if __name__ == "__main__":
    unittest.main()
