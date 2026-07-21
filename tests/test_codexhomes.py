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
               plan_in="access", exp_offset=3600, account_id=None):
        """A fake codexhome with a CLI-native auth.json. plan_in selects which
        token carries the plan claim (the premise reads access first; the
        live id_token carries it too — both paths must classify)."""
        d = os.path.join(codexhomes.homes_root(), name)
        os.makedirs(d, exist_ok=True)
        exp = int(time.time()) + exp_offset
        plan_claim = {"https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
        id_claims = {"email": email}
        acc_claims = {"exp": exp, "sub": "fake"}
        (acc_claims if plan_in == "access" else id_claims).update(plan_claim)
        auth = {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": _jwt(id_claims),
                "access_token": _jwt(acc_claims),
                "refresh_token": "fake-refresh-token-" + name,
                "account_id": account_id or ("acct-" + name),
            },
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


if __name__ == "__main__":
    unittest.main()
