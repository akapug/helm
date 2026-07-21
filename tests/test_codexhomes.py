"""Hermetic tests for helm.codexhomes — HELM_HOME and HELM_CODEX_HOMES_DIR
both point at tmp dirs; fake JWTs are minted in-test. The real ~/.codex-homes
and the real seat pool are never read, no codex CLI is ever run."""
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

from helm import codexhomes


def _b64seg(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims):
    """header.payload.sig — enough structure for unverified payload decode."""
    return _b64seg({"alg": "RS256", "typ": "JWT"}) + "." + _b64seg(claims) + ".fake-sig"


class CodexHomesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-codexhomes-")
        self._env = {k: os.environ.get(k) for k in
                     ("HELM_HOME", "MELD_HOME", "HELM_CODEX_HOMES_DIR",
                      "MELD_CODEX_HOMES_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CODEX_HOMES_DIR"] = os.path.join(self.tmp, "codex-homes")
        os.environ.pop("MELD_HOME", None)
        os.environ.pop("MELD_CODEX_HOMES_DIR", None)
        os.makedirs(os.environ["HELM_CODEX_HOMES_DIR"])

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def _plant(self, name, email="fake@example.com", plan="pro",
               plan_in="access", exp_offset=3600, account_id=None,
               acct_in="tokens"):
        """A fake codexhome with a CLI-native auth.json. plan_in selects which
        token carries the plan claim (the premise reads access first; the
        live id_token carries it too — both paths must classify). acct_in
        plants the account id in tokens.account_id (today's CLI) or ONLY in
        the id_token chatgpt_account_id claim (a shape the CLI has emitted —
        the kimi-review FIX-1 fixture)."""
        d = os.path.join(codexhomes.homes_root(), name)
        os.makedirs(d, exist_ok=True)
        exp = int(time.time()) + exp_offset
        acct = account_id or ("acct-" + name)
        id_auth, acc_auth = {}, {}
        (acc_auth if plan_in == "access" else id_auth)["chatgpt_plan_type"] = plan
        if acct_in == "id":
            id_auth["chatgpt_account_id"] = acct
        tokens = {
            "id_token": _jwt({"email": email,
                              "https://api.openai.com/auth": id_auth}),
            "access_token": _jwt({"exp": exp, "sub": "fake",
                                  "https://api.openai.com/auth": acc_auth}),
            "refresh_token": "fake-refresh-token-" + name,
        }
        if acct_in == "tokens":
            tokens["account_id"] = acct
        auth = {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "tokens": tokens,
            "last_refresh": "2026-07-09T14:52:47.713051089Z",
        }
        path = os.path.join(d, "auth.json")
        with open(path, "w") as f:
            json.dump(auth, f)
        return path, auth, exp

    def _cmd(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = codexhomes.cmd_codex(list(args))
        return rc, out.getvalue(), err.getvalue()

    def _assert_no_secrets(self, text):
        """Token material never reaches a printed surface."""
        self.assertNotIn("fake-refresh-token", text)
        self.assertNotIn("fake-sig", text)
        self.assertNotIn("eyJ", text)  # no base64 JWT segment leaks either

    # -- list: tier classification, aliases, pooled column ------------------
    def test_list_classifies_ultra_and_team(self):
        self._plant("cto-example", email="cto@mv.test", plan="pro", plan_in="access")
        self._plant("team-example", email="hey@simbi.test", plan="team", plan_in="id")
        rows = {r["name"]: r for r in codexhomes.codex_list()}
        self.assertEqual(rows["cto-example"]["tier"], "ultra")
        self.assertEqual(rows["cto-example"]["plan"], "pro")
        self.assertEqual(rows["team-example"]["tier"], "team")
        self.assertEqual(rows["team-example"]["email"], "hey@simbi.test")
        self.assertFalse(rows["cto-example"]["pooled"])

    def test_list_folds_symlink_alias_and_same_account_dirs(self):
        self._plant("real-home", email="one@x.test")
        os.symlink(os.path.join(codexhomes.homes_root(), "real-home"),
                   os.path.join(codexhomes.homes_root(), "alias-home"))
        self._plant("copy-home", email="one@x.test", account_id="acct-real-home")
        rows = codexhomes.codex_list()
        self.assertEqual(len(rows), 1)  # one account = one row
        self.assertEqual(rows[0]["name"], "copy-home")  # first sorted real dir wins
        self.assertEqual(sorted(rows[0]["aliases"]), ["alias-home", "real-home"])

    def test_list_no_auth_home_visible(self):
        os.makedirs(os.path.join(codexhomes.homes_root(), "empty-home"))
        rows = {r["name"]: r for r in codexhomes.codex_list()}
        self.assertIn("empty-home", rows)
        self.assertFalse(rows["empty-home"]["authed"])
        self.assertIsNone(rows["empty-home"]["email"])

    def test_list_cli_table_no_secrets(self):
        self._plant("cto-example", email="cto@mv.test", plan="pro")
        rc, out, err = self._cmd("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("cto-example", out)
        self.assertIn("cto@mv.test", out)
        self.assertIn("ultra", out)
        self._assert_no_secrets(out + err)

    # -- pool: flat 0600 record, right fields, source untouched -------------
    def test_pool_writes_flat_0600_record(self):
        path, auth, exp = self._plant("cto-example", email="cto@mv.test", plan="pro")
        with open(path, "rb") as f:
            before = f.read()
        res = codexhomes.codex_pool("cto-example")
        self.assertTrue(res.get("ok"), res)
        dest = os.path.join(codexhomes.pool_dir(), "codex-cto-example.json")
        self.assertEqual(res["path"], dest)
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o600)
        with open(dest) as f:
            rec = json.load(f)
        t = auth["tokens"]
        self.assertEqual(rec["type"], "codex")
        self.assertEqual(rec["email"], "cto@mv.test")
        self.assertEqual(rec["account_id"], t["account_id"])
        self.assertEqual(rec["access_token"], t["access_token"])
        self.assertEqual(rec["id_token"], t["id_token"])
        self.assertEqual(rec["refresh_token"], t["refresh_token"])
        self.assertIs(rec["disabled"], False)
        self.assertEqual(rec["last_refresh"], auth["last_refresh"])
        self.assertEqual(rec["expired"],
                         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)))
        self.assertNotIn("OPENAI_API_KEY", rec)  # the flat record carries no key field
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)  # source byte-identical
        # the result dict is print-safe: no token material rides in it
        self._assert_no_secrets(json.dumps(res))

    def test_pool_account_id_from_id_token_claim_only(self):
        """kimi FIX 1: an auth.json carrying the account id ONLY in the
        id_token claim must still pool with account_id present — the naive
        tokens.account_id copy dropped it, silently breaking dedup + the
        list pooled-linkage."""
        self._plant("claim-only", email="claim@x.test", acct_in="id")
        res = codexhomes.codex_pool("claim-only")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["account_id"], "acct-claim-only")
        with open(os.path.join(codexhomes.pool_dir(),
                               "codex-claim-only.json")) as f:
            self.assertEqual(json.load(f)["account_id"], "acct-claim-only")
        # linkage: list keys the pooled column off the same resolution
        rows = {r["name"]: r for r in codexhomes.codex_list()}
        self.assertEqual(rows["claim-only"]["pooled"], "codex-claim-only.json")
        # dedup: a second home of the SAME account no longer pools blind
        self._plant("claim-twin", email="claim@x.test",
                    account_id="acct-claim-only", acct_in="id")
        res2 = codexhomes.codex_pool("claim-twin")
        self.assertEqual(res2["also_pooled_as"], ["codex-claim-only.json"])
        # and the pooled roster shows the account, not an accountless row
        by_file = {r["file"]: r for r in codexhomes.codex_pooled()}
        self.assertEqual(by_file["codex-claim-only.json"]["account_id"],
                         "acct-claim-only")

    def test_pool_idempotent_refresh(self):
        self._plant("cto-example")
        first = codexhomes.codex_pool("cto-example")
        self.assertFalse(first["updated"])
        second = codexhomes.codex_pool("cto-example")
        self.assertTrue(second.get("ok"), second)
        self.assertTrue(second["updated"])
        pool = os.listdir(codexhomes.pool_dir())
        self.assertEqual(pool, ["codex-cto-example.json"])

    def test_pool_via_alias_writes_canonical(self):
        self._plant("real-home")
        os.symlink(os.path.join(codexhomes.homes_root(), "real-home"),
                   os.path.join(codexhomes.homes_root(), "alias-home"))
        res = codexhomes.codex_pool("alias-home")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["pooled"], "codex-real-home.json")

    def test_pool_missing_home_refused(self):
        res = codexhomes.codex_pool("no-such")
        self.assertIn("error", res)
        self.assertIn("no codexhome", res["error"])

    def test_pool_home_without_auth_refused_with_login_line(self):
        os.makedirs(os.path.join(codexhomes.homes_root(), "empty-home"))
        res = codexhomes.codex_pool("empty-home")
        self.assertIn("error", res)
        self.assertIn("codex login --device-auth", res["error"])

    def test_pool_stale_exp_warns_never_refuses(self):
        """The premise's law: a past exp is a refresh-first signal, not death."""
        self._plant("stale-home", exp_offset=-60)
        res = codexhomes.codex_pool("stale-home")
        self.assertTrue(res.get("ok"), res)
        self.assertIn("autorefreshes", res["warn"])
        self.assertTrue(os.path.exists(
            os.path.join(codexhomes.pool_dir(), "codex-stale-home.json")))

    def test_pool_cli_output_no_secrets(self):
        self._plant("cto-example", email="cto@mv.test")
        rc, out, err = self._cmd("pool", "cto-example")
        self.assertEqual(rc, 0, err)
        self.assertIn("cto@mv.test", out)
        self.assertIn("hot-reload", out)
        self._assert_no_secrets(out + err)

    # -- pooled / unpool round-trip ------------------------------------------
    def test_pooled_unpool_roundtrip(self):
        self._plant("cto-example", email="cto@mv.test", plan="pro")
        self._plant("team-example", email="hey@simbi.test", plan="team")
        codexhomes.codex_pool("cto-example")
        codexhomes.codex_pool("team-example")
        rows = {r["file"]: r for r in codexhomes.codex_pooled()}
        self.assertEqual(len(rows), 2)
        r = rows["codex-cto-example.json"]
        self.assertEqual((r["email"], r["tier"], r["disabled"]),
                         ("cto@mv.test", "ultra", False))
        self.assertEqual(rows["codex-team-example.json"]["tier"], "team")
        # list now shows the pooled linkage
        by_name = {x["name"]: x for x in codexhomes.codex_list()}
        self.assertEqual(by_name["cto-example"]["pooled"], "codex-cto-example.json")
        # unpool one — the other survives
        res = codexhomes.codex_unpool("cto-example")
        self.assertEqual(res["removed"], ["codex-cto-example.json"])
        self.assertEqual([r["file"] for r in codexhomes.codex_pooled()],
                         ["codex-team-example.json"])
        # fail-open: unpooling the already-absent name is ok, not error
        again = codexhomes.codex_unpool("cto-example")
        self.assertTrue(again["ok"])
        self.assertEqual(again["removed"], [])
        self.assertIn("fail-open", again["note"])

    def test_unpool_via_alias_removes_canonical(self):
        self._plant("real-home")
        os.symlink(os.path.join(codexhomes.homes_root(), "real-home"),
                   os.path.join(codexhomes.homes_root(), "alias-home"))
        codexhomes.codex_pool("real-home")
        res = codexhomes.codex_unpool("alias-home")
        self.assertEqual(res["removed"], ["codex-real-home.json"])

    def test_pooled_cli_no_secrets_and_unparseable_reported(self):
        self._plant("cto-example", email="cto@mv.test")
        codexhomes.codex_pool("cto-example")
        os.makedirs(codexhomes.pool_dir(), exist_ok=True)
        with open(os.path.join(codexhomes.pool_dir(), "junk.json"), "w") as f:
            f.write("{not json")
        rc, out, err = self._cmd("pooled")
        self.assertEqual(rc, 0, err)
        self.assertIn("cto@mv.test", out)
        self.assertIn("unparseable", out)
        self._assert_no_secrets(out + err)

    # -- CLI edges -----------------------------------------------------------
    def test_cli_bare_defaults_to_list(self):
        rc, out, _ = self._cmd()
        self.assertEqual(rc, 0)
        self.assertIn("no codexhomes", out)

    def test_cli_pool_without_name_usage(self):
        rc, _, err = self._cmd("pool")
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_cli_unknown_subverb(self):
        rc, _, err = self._cmd("frobnicate")
        self.assertEqual(rc, 2)
        self.assertIn("unknown subverb", err)

    def test_dispatcher_wired(self):
        from helm import cli
        self.assertIn("codex", cli.VERBS)
        self.assertIn("codex", cli._VERB_HELP)


def _rollout(name, primary_pct, secondary_pct=0.0, reached=None,
             resets_offset=18000, age_s=0):
    """One fake rollout line the way the codex CLI appends it: an event_msg
    whose payload carries rate_limits with primary/secondary windows."""
    now = int(time.time())
    rl = {"limit_id": "codex", "limit_name": None,
          "primary": {"used_percent": primary_pct, "window_minutes": 300,
                      "resets_at": now + resets_offset},
          "secondary": {"used_percent": secondary_pct, "window_minutes": 10080,
                        "resets_at": now + resets_offset * 10},
          "credits": None, "individual_limit": None,
          "plan_type": "pro", "rate_limit_reached_type": reached}
    line = json.dumps({"timestamp": "2026-07-21T00:00:00.000Z",
                       "type": "event_msg",
                       "payload": {"type": "token_count", "info": {},
                                   "rate_limits": rl}})
    home = os.path.join(codexhomes.homes_root(), name)
    d = os.path.join(home, "sessions", "2026", "07", "21")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "rollout-2026-07-21T00-00-00-%s.jsonl" % name)
    with open(p, "w") as f:
        f.write('{"type":"message","payload":{}}\n' + line + "\n")
    if age_s:
        old = time.time() - age_s
        os.utime(p, (old, old))
    return p


class LaunchGateTest(CodexHomesTest):
    """The `helm codex launch` cred-% gate (runbook fix #3): refuse-by-default
    when no POOLED cred reads ok from its own rollout rate_limits; --force
    overrides; a green gate delegates to the seat-launch mint."""

    def setUp(self):
        super().setUp()
        from helm import seat
        # the delegate path needs a minted codex seat (config.yaml + token)
        os.makedirs(seat.seat_dir("codex"), exist_ok=True)
        seat._write_private(os.path.join(seat.seat_dir("codex"), "config.yaml"),
                            "port: 8317\n", mode=0o600)
        seat._write_private(os.path.join(seat.seat_dir("codex"), "token"),
                            "gate-test-token\n", mode=0o600)

    # -- usage_gate classification -------------------------------------------
    def test_gate_classifies_ok_near_exhausted(self):
        self._plant("okhome", email="ok@x.test")
        _rollout("okhome", 12.0, 40.0)
        self._plant("nearhome", email="near@x.test")
        _rollout("nearhome", 85.0)
        self._plant("caphome", email="cap@x.test")
        _rollout("caphome", 100.0)
        rows = {g["name"]: g for g in codexhomes.usage_gate()}
        self.assertEqual(rows["okhome"]["status"], "ok")
        self.assertEqual(rows["nearhome"]["status"], "near")
        self.assertEqual(rows["caphome"]["status"], "exhausted")

    def test_gate_reached_type_is_exhausted(self):
        self._plant("rhome", email="r@x.test")
        _rollout("rhome", 40.0, reached="primary")
        row = [g for g in codexhomes.usage_gate() if g["name"] == "rhome"][0]
        self.assertEqual(row["status"], "exhausted")
        self.assertEqual(row["reached"], "primary")

    def test_gate_expired_window_not_binding(self):
        self._plant("oldhome", email="old@x.test")
        _rollout("oldhome", 99.0, resets_offset=-60)  # window already over
        row = [g for g in codexhomes.usage_gate()
               if g["name"] == "oldhome"][0]
        self.assertEqual(row["status"], "ok")  # no live window binds
        self.assertEqual(row["pct"], 0.0)

    def test_gate_no_rollout_is_unknown(self):
        self._plant("quiet", email="q@x.test")
        row = [g for g in codexhomes.usage_gate()
               if g["name"] == "quiet"][0]
        self.assertEqual(row["status"], "unknown")

    def test_gate_stale_rollout_is_unknown(self):
        self._plant("stale", email="s@x.test")
        _rollout("stale", 5.0, age_s=codexhomes.STALE_S + 120)
        row = [g for g in codexhomes.usage_gate()
               if g["name"] == "stale"][0]
        self.assertEqual(row["status"], "unknown")
        self.assertIn("stale", row["note"])

    def test_gate_tolerates_truncated_tail_line(self):
        """A tail cut mid-line (first partial line of the window) must not
        kill the read — older whole lines in the same tail still parse."""
        self._plant("cut", email="c@x.test")
        p = _rollout("cut", 7.0)
        with open(p, "a") as f:
            f.write('{"type":"event_msg","payload":{"rate_limits":{"prim')
        row = [g for g in codexhomes.usage_gate() if g["name"] == "cut"][0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["pct"], 7.0)

    # -- launch verdict --------------------------------------------------------
    def test_launch_refuses_when_all_pooled_not_ok(self):
        self._plant("a", email="a@x.test")
        _rollout("a", 95.0)                      # near
        self._cmd("pool", "a")
        self._plant("b", email="b@x.test")       # unknown, unpooled
        rc, out, err = self._cmd("launch")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSE", err)
        self.assertIn("helm codex pool b", err)  # the concrete fix
        self.assertNotIn("claude --dangerously", out)  # no mint happened

    def test_launch_allows_with_one_ok_pooled_cred(self):
        self._plant("good", email="g@x.test")
        _rollout("good", 3.0)
        self._cmd("pool", "good")
        self._plant("bad", email="b@x.test")
        _rollout("bad", 100.0)
        self._cmd("pool", "bad")
        rc, out, err = self._cmd("launch", "-i", "2")
        self.assertEqual(rc, 0, err)
        self.assertIn("HELM_CHAT_NAME=codex-2", out)  # mint delegated
        self.assertIn("gate", err)

    def test_launch_force_overrides_refusal(self):
        self._plant("a", email="a@x.test")       # unknown (no rollout)
        self._cmd("pool", "a")
        rc, out, err = self._cmd("launch", "--force")
        self.assertEqual(rc, 0, err)
        self.assertIn("--force", err)
        self.assertIn("claude --dangerously-skip-permissions", out)

    def test_launch_bad_instance_flag_rc2(self):
        rc, _, err = self._cmd("launch", "-i", "two")
        self.assertEqual(rc, 2)
        self.assertIn("integer", err)

    def test_gate_output_no_secrets(self):
        self._plant("a", email="a@x.test")
        _rollout("a", 10.0)
        self._cmd("pool", "a")
        rc, out, err = self._cmd("launch")
        self.assertEqual(rc, 0, err)
        self._assert_no_secrets(out + err)


if __name__ == "__main__":
    unittest.main()
