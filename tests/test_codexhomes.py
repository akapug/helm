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
from tests._fakeorca import FakeDaemon


def _b64seg(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims):
    """header.payload.sig — enough structure for unverified payload decode."""
    return _b64seg({"alg": "RS256", "typ": "JWT"}) + "." + _b64seg(claims) + ".fake-sig"


def _auth_json(tag, email="fake@example.com", plan="pro", plan_in="access",
               exp_offset=3600, account_id=None, acct_in="tokens"):
    """One CLI-native auth.json dict + its exp. plan_in selects which token
    carries the plan claim (the premise reads access first; the live id_token
    carries it too — both paths must classify). acct_in plants the account id
    in tokens.account_id (today's CLI) or ONLY in the id_token
    chatgpt_account_id claim (a shape the CLI has emitted — the kimi-review
    FIX-1 fixture). tag keys the refresh token so byte-provenance is
    assertable across two stores holding the same account."""
    exp = int(time.time()) + exp_offset
    acct = account_id or ("acct-" + tag)
    id_auth, acc_auth = {}, {}
    (acc_auth if plan_in == "access" else id_auth)["chatgpt_plan_type"] = plan
    if acct_in == "id":
        id_auth["chatgpt_account_id"] = acct
    tokens = {
        "id_token": _jwt({"email": email,
                          "https://api.openai.com/auth": id_auth}),
        "access_token": _jwt({"exp": exp, "sub": "fake",
                              "https://api.openai.com/auth": acc_auth}),
        "refresh_token": "fake-refresh-token-" + tag,
    }
    if acct_in == "tokens":
        tokens["account_id"] = acct
    return {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "tokens": tokens,
        "last_refresh": "2026-07-09T14:52:47.713051089Z",
    }, exp


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
    def _plant(self, name, **kw):
        """A fake codexhome with a CLI-native auth.json (_auth_json's shape;
        its kwargs pass through)."""
        d = os.path.join(codexhomes.homes_root(), name)
        os.makedirs(d, exist_ok=True)
        auth, exp = _auth_json(name, **kw)
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
        self._plant("admin-example", email="admin@example.test", plan="pro", plan_in="access")
        self._plant("member-example", email="member@example.test", plan="team", plan_in="id")
        rows = {r["name"]: r for r in codexhomes.codex_list()}
        self.assertEqual(rows["admin-example"]["tier"], "ultra")
        self.assertEqual(rows["admin-example"]["plan"], "pro")
        self.assertEqual(rows["member-example"]["tier"], "team")
        self.assertEqual(rows["member-example"]["email"], "member@example.test")
        self.assertFalse(rows["admin-example"]["pooled"])

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
        self._plant("admin-example", email="admin@example.test", plan="pro")
        rc, out, err = self._cmd("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("admin-example", out)
        self.assertIn("admin@example.test", out)
        self.assertIn("ultra", out)
        self._assert_no_secrets(out + err)

    # -- pool: flat 0600 record, right fields, source untouched -------------
    def test_pool_writes_flat_0600_record(self):
        path, auth, exp = self._plant("admin-example", email="admin@example.test", plan="pro")
        with open(path, "rb") as f:
            before = f.read()
        res = codexhomes.codex_pool("admin-example")
        self.assertTrue(res.get("ok"), res)
        dest = os.path.join(codexhomes.pool_dir(), "codex-admin-example.json")
        self.assertEqual(res["path"], dest)
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o600)
        with open(dest) as f:
            rec = json.load(f)
        t = auth["tokens"]
        self.assertEqual(rec["type"], "codex")
        self.assertEqual(rec["email"], "admin@example.test")
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
        self._plant("admin-example")
        first = codexhomes.codex_pool("admin-example")
        self.assertFalse(first["updated"])
        dest = first["path"]
        stamp = os.stat(dest).st_mtime_ns
        # byte-identical re-pool is a NO-OP: the stale-401 cure is FRESH
        # bytes, and an identical rewrite only churns the proxy's hot-reload
        # watcher (+ lets a one-shot sync-orca double-run prove anti-churn)
        second = codexhomes.codex_pool("admin-example")
        self.assertTrue(second.get("ok"), second)
        self.assertTrue(second.get("unchanged"), second)
        self.assertEqual(os.stat(dest).st_mtime_ns, stamp)
        # divergent pool bytes (proxy refreshed its copy in place, or the
        # source re-logged-in) DO rewrite — the refresh cure still fires
        with open(dest) as f:
            rec = json.load(f)
        rec["access_token"] += "-drifted"
        codexhomes._write_pool_atomic(dest, json.dumps(rec, indent=2) + "\n")
        third = codexhomes.codex_pool("admin-example")
        self.assertTrue(third.get("ok"), third)
        self.assertTrue(third["updated"])
        self.assertFalse(third.get("unchanged"))
        pool = os.listdir(codexhomes.pool_dir())
        self.assertEqual(pool, ["codex-admin-example.json"])

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
        self._plant("admin-example", email="admin@example.test")
        rc, out, err = self._cmd("pool", "admin-example")
        self.assertEqual(rc, 0, err)
        self.assertIn("admin@example.test", out)
        self.assertIn("hot-reload", out)
        self._assert_no_secrets(out + err)

    # -- pooled / unpool round-trip ------------------------------------------
    def test_pooled_unpool_roundtrip(self):
        self._plant("admin-example", email="admin@example.test", plan="pro")
        self._plant("member-example", email="member@example.test", plan="team")
        codexhomes.codex_pool("admin-example")
        codexhomes.codex_pool("member-example")
        rows = {r["file"]: r for r in codexhomes.codex_pooled()}
        self.assertEqual(len(rows), 2)
        r = rows["codex-admin-example.json"]
        self.assertEqual((r["email"], r["tier"], r["disabled"]),
                         ("admin@example.test", "ultra", False))
        self.assertEqual(rows["codex-member-example.json"]["tier"], "team")
        # list now shows the pooled linkage
        by_name = {x["name"]: x for x in codexhomes.codex_list()}
        self.assertEqual(by_name["admin-example"]["pooled"], "codex-admin-example.json")
        # unpool one — the other survives
        res = codexhomes.codex_unpool("admin-example")
        self.assertEqual(res["removed"], ["codex-admin-example.json"])
        self.assertEqual([r["file"] for r in codexhomes.codex_pooled()],
                         ["codex-member-example.json"])
        # fail-open: unpooling the already-absent name is ok, not error
        again = codexhomes.codex_unpool("admin-example")
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
        self._plant("admin-example", email="admin@example.test")
        codexhomes.codex_pool("admin-example")
        os.makedirs(codexhomes.pool_dir(), exist_ok=True)
        with open(os.path.join(codexhomes.pool_dir(), "junk.json"), "w") as f:
            f.write("{not json")
        rc, out, err = self._cmd("pooled")
        self.assertEqual(rc, 0, err)
        self.assertIn("admin@example.test", out)
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
    """The `helm codex launch` cred-% gate : refuse-by-default
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
        # the canonical seat tail: plan-mode ENTRY denied, permissions bypassed
        self.assertIn("claude --disallowedTools EnterPlanMode"
                      " --dangerously-skip-permissions", out)

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


def _fake_snapshot(accounts, active, system_default=None):
    """orca's AccountsSnapshot, only the branch sync-orca reads populated."""
    codex = {"accounts": accounts, "activeAccountId": active,
             "activeAccountIdsByRuntime": {"host": active, "wsl": {}}}
    if system_default is not None:
        codex["systemDefault"] = system_default
    return {"claude": {"accounts": [], "activeAccountId": None},
            "codex": codex, "rateLimits": {}}


def _orca_acct(aid, email, provider=None):
    """One CodexManagedAccountSummary row — the fields sync-orca reads plus
    the required stamps."""
    return {"id": aid, "email": email, "providerAccountId": provider,
            "managedHomeRuntime": "host", "wslDistro": None,
            "createdAt": 0, "updatedAt": 0, "lastAuthenticatedAt": 0}


class _SyncOrcaBase(CodexHomesTest):
    """Shared rig for the sync-orca legs: ORCA_USER_DATA_PATH is pinned per
    test (without it the adapter falls back to $HOME/.config/orca and a unit
    test talks to the LIVE daemon), and HELM_ORCA_CLI is pinned OFF — the
    CLI fallback route reaches the REAL `orca` binary on PATH, the same
    live-workspace hazard the HELM_ORCA_RPC kill-switch exists for; fallback
    tests point it at a stub script (_stub_cli) instead."""

    def setUp(self):
        super().setUp()
        self.orca_dir = os.path.join(self.tmp, "orca-ud")
        os.makedirs(self.orca_dir)
        self._orca_env = {k: os.environ.get(k)
                          for k in ("ORCA_USER_DATA_PATH", "HELM_ORCA_RPC",
                                    "HELM_ORCA_CLI",
                                    "HELM_CODEX_ACCOUNTS_TIMEOUT_S",
                                    "HELM_CODEX_FORCE_MANAGED")}
        os.environ["ORCA_USER_DATA_PATH"] = self.orca_dir
        os.environ.pop("HELM_ORCA_RPC", None)
        os.environ["HELM_ORCA_CLI"] = "off"
        os.environ.pop("HELM_CODEX_ACCOUNTS_TIMEOUT_S", None)
        os.environ.pop("HELM_CODEX_FORCE_MANAGED", None)

    def tearDown(self):
        for k, v in self._orca_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def _stub_cli(self, stdout="", rc=0, stderr=""):
        """Point HELM_ORCA_CLI at a stub `orca` that prints canned bytes —
        the discovery seam is the pin, no real orca in tests. Returns the
        path of a file recording the argv each invocation appends."""
        arglog = os.path.join(self.tmp, "cli-args.log")
        path = os.path.join(self.tmp, "orca-stub")
        with open(path, "w") as f:
            f.write("#!/usr/bin/env python3\n"
                    "import sys\n"
                    "open(%r, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                    "sys.stdout.write(%r)\n"
                    "sys.stderr.write(%r)\n"
                    "sys.exit(%d)\n" % (arglog, stdout, stderr, rc))
        os.chmod(path, 0o755)
        os.environ["HELM_ORCA_CLI"] = path
        return arglog

    def _daemon(self, accounts, active, **kw):
        d = FakeDaemon(self.orca_dir,
                       reply=_fake_snapshot(accounts, active, **kw))
        self.addCleanup(d.close)
        return d

    def _plant_orca_managed(self, aid, email="live@x.test", marker=None,
                            token_tag=None, **kw):
        """orca's managed per-account home under the pinned userData —
        codex-accounts/<aid>/home/{auth.json,.orca-managed-home}, the exact
        convention orca's codex-accounts/service.ts mints (path + marker
        bytes) and host-codex-managed-home-ownership.ts proves. token_tag
        keys the refresh token so a test can prove WHICH store's bytes got
        pooled; marker overrides the ownership contents (mismatch fixtures)."""
        d = os.path.join(self.orca_dir, "codex-accounts", aid, "home")
        os.makedirs(d, exist_ok=True)
        auth, exp = _auth_json(token_tag or ("orca-" + aid), email=email, **kw)
        path = os.path.join(d, "auth.json")
        with open(path, "w") as f:
            json.dump(auth, f)
        with open(os.path.join(d, ".orca-managed-home"), "w") as f:
            f.write((marker or aid) + "\n")
        return path, auth, exp

    def _pool_files(self):
        # raw listdir, not *.json glob: a lingering .pool-*.tmp sibling must
        # show up in the equality checks below (the atomic-write proof)
        d = codexhomes.pool_dir()
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

class SyncOrcaTest(_SyncOrcaBase):
    """`helm codex sync-orca` — the one-way orca-selection -> pool adapter,
    driven over the REAL AF_UNIX transport (tests/_fakeorca.FakeDaemon)."""

    # (a) select -> pool: exactly one file, 0600, no tmp remnant
    def test_selected_account_pools_exactly_one_file(self):
        self._plant("work-home", email="work@x.test")
        self._plant("other-home", email="other@x.test")
        d = self._daemon([_orca_acct("a1", "work@x.test", "acct-work-home"),
                          _orca_acct("a2", "other@x.test",
                                     "acct-other-home")], "a1")
        rc, out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._pool_files(), ["codex-work-home.json"])
        dest = os.path.join(codexhomes.pool_dir(), "codex-work-home.json")
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o600)
        with open(dest) as f:
            self.assertEqual(json.load(f)["account_id"], "acct-work-home")
        self.assertEqual(d.seen[0]["method"], "accounts.list")
        self.assertIn("work@x.test", out)
        self._assert_no_secrets(out + err)

    # (b) no match: refuse printing BOTH rosters, NOTHING written. The
    # nothing-written proof rides a PRE-POOLED bystander cred (the positive
    # control on the pool observable): the post-refusal equality cannot pass
    # by listing a wrong/empty dir, and it proves no-clobber besides.
    def test_no_match_refuses_with_both_rosters_and_writes_nothing(self):
        self._plant("only-home", email="local@x.test")
        self.assertTrue(codexhomes.codex_pool("only-home").get("ok"))
        self._daemon([_orca_acct("a1", "stranger@x.test", "acct-stranger")],
                     "a1")
        rc, out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 1, err)
        self.assertIn("REFUSE", err)
        self.assertIn("stranger@x.test", err)   # orca's roster surfaced
        self.assertIn("only-home", err)         # helm's roster surfaced
        self.assertIn("codex login --device-auth", err)  # the shopping list
        # the shopping list names BOTH locations (amendment): the codexhomes
        # roster above AND the orca-managed store probe with its derived path
        self.assertIn("orca-managed store:", err)
        self.assertIn(os.path.join(self.orca_dir, "codex-accounts"), err)
        self.assertEqual(self._pool_files(), ["codex-only-home.json"])
        self._assert_no_secrets(out + err)

    def test_ambiguous_email_match_refuses_without_writing(self):
        """No account id from orca + two homes sharing the email = a guess
        either way; the refusal names both candidates instead. home-a rides
        pre-pooled as the positive control on the pool observable."""
        self._plant("home-a", email="dup@x.test", account_id="acct-a")
        self._plant("home-b", email="dup@x.test", account_id="acct-b")
        self.assertTrue(codexhomes.codex_pool("home-a").get("ok"))
        self._daemon([_orca_acct("a1", "dup@x.test")], "a1")
        rc, _out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 1)
        self.assertIn("home-a", err)
        self.assertIn("home-b", err)
        self.assertEqual(self._pool_files(), ["codex-home-a.json"])

    # (c) daemon absent: exit 2 naming what was tried
    def test_daemon_absent_exits_2_naming_the_metadata_tried(self):
        rc, _out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 2)
        self.assertIn("orca-runtime.json", err)

    def test_dead_socket_exits_2_naming_the_socket(self):
        """The bystander cred is the positive control: the pool observable is
        live, and the dead-socket exit adds nothing beside it."""
        self._plant("bystander", email="by@x.test")
        self.assertTrue(codexhomes.codex_pool("bystander").get("ok"))
        gone = os.path.join(self.orca_dir, "gone.sock")
        with open(os.path.join(self.orca_dir, "orca-runtime.json"), "w") as f:
            json.dump({"runtimeId": "rt1", "authToken": "t",
                       "transports": [{"kind": "unix", "endpoint": gone}]}, f)
        rc, _out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 2)
        self.assertIn("gone.sock", err)
        self.assertEqual(self._pool_files(), ["codex-bystander.json"])

    def test_system_default_selection_maps_through_its_identity(self):
        """activeAccountId null = orca's system-default slot; the identity
        rides the snapshot's systemDefault block and must still match."""
        self._plant("sys-home", email="sys@x.test")
        self._daemon([], None, system_default={
            "hasAuth": True, "authKind": "oauth", "email": "sys@x.test",
            "providerAccountId": "acct-sys-home", "workspaceLabel": None})
        rc, out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 0, err)
        self.assertIn("sys@x.test", out)
        files = self._pool_files()
        self.assertEqual(files, ["codex-sys-home.json"])

    def test_system_default_without_oauth_refuses(self):
        """sys-home rides pre-pooled as the positive control: the refusal
        must leave the pool exactly as it stood."""
        self._plant("sys-home", email="sys@x.test")
        self.assertTrue(codexhomes.codex_pool("sys-home").get("ok"))
        self._daemon([], None, system_default={
            "hasAuth": False, "authKind": "none", "email": None,
            "providerAccountId": None, "workspaceLabel": None})
        rc, _out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 1)
        self.assertIn("system-default", err)
        self.assertEqual(self._pool_files(), ["codex-sys-home.json"])

    def test_stale_token_pools_anyway_with_warn(self):
        """The module's law holds through this leg too: a past exp is a
        refresh-first WARN with the cure line, never a refusal."""
        self._plant("stale-sel", email="s@x.test", exp_offset=-60)
        self._daemon([_orca_acct("a1", "s@x.test", "acct-stale-sel")], "a1")
        rc, out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 0, err)
        self.assertIn("WARN", out)
        self.assertIn("autorefreshes", out)
        self.assertEqual(self._pool_files(), ["codex-stale-sel.json"])

    def test_unchanged_prev_key_skips_the_rewrite(self):
        """The --watch anti-churn seam: same selection + still pooled = no
        write (the proxy's hot-reload watcher must not fire per tick)."""
        self._plant("work-home", email="w@x.test")
        self._daemon([_orca_acct("a1", "w@x.test", "acct-work-home")], "a1")
        first = codexhomes.codex_sync_orca()
        self.assertTrue(first.get("ok"), first)
        stamp = os.stat(first["path"]).st_mtime_ns
        second = codexhomes.codex_sync_orca(prev_key=first["key"])
        self.assertTrue(second.get("unchanged"), second)
        self.assertEqual(os.stat(first["path"]).st_mtime_ns, stamp)

    def test_selection_change_repools_the_new_account(self):
        """A flipped selection pools the NEW home; the old pooled cred
        SURVIVES — the pool is the proxy's fall-through, never a slot."""
        self._plant("home-a", email="a@x.test")
        self._plant("home-b", email="b@x.test")
        accts = [_orca_acct("a1", "a@x.test", "acct-home-a"),
                 _orca_acct("a2", "b@x.test", "acct-home-b")]
        d = self._daemon(accts, "a1")
        first = codexhomes.codex_sync_orca()
        self.assertEqual(first.get("home"), "home-a")
        d.reply = _fake_snapshot(accts, "a2")
        second = codexhomes.codex_sync_orca(prev_key=first["key"])
        self.assertEqual(second.get("home"), "home-b")
        self.assertFalse(second.get("unchanged"))
        self.assertEqual(self._pool_files(),
                         ["codex-home-a.json", "codex-home-b.json"])

    # -- source precedence: orca-managed live bytes > codex-homes copy -------
    # (amendment, measured 2026-08-04: codex ROTATES refresh tokens; orca's
    # managed copy refreshing first BURNS our ~/.codex-homes duplicate, so
    # syncing the selection while pooling our copy ships dead bytes)

    def test_managed_source_wins_over_codexhomes_copy(self):  # noqa: VACUOUS_ASSERTION — assertEqual on the same rec["refresh_token"] key is the unconditional positive control; the assertNotEqual only pins fixture divergence
        """(a) both stores hold the account: the pooled record must carry
        orca's LIVE bytes, keep the codexhome's pool-file name (slot
        continuity), and leave the managed source byte-identical."""
        self._plant("work-home", email="work@x.test",
                    account_id="acct-shared")
        mpath, mauth, _ = self._plant_orca_managed(
            "a1", email="work@x.test", account_id="acct-shared",
            token_tag="orca-live")
        with open(mpath, "rb") as f:
            before = f.read()
        self._daemon([_orca_acct("a1", "work@x.test", "acct-shared")], "a1")
        res = codexhomes.codex_sync_orca()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["source"], "orca-managed")
        self.assertEqual(res["home"], "work-home")
        self.assertEqual(self._pool_files(), ["codex-work-home.json"])
        with open(os.path.join(codexhomes.pool_dir(),
                               "codex-work-home.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["refresh_token"],
                         mauth["tokens"]["refresh_token"])   # the LIVE bytes
        self.assertNotEqual(rec["refresh_token"],
                            "fake-refresh-token-work-home")  # not our copy
        with open(mpath, "rb") as f:
            self.assertEqual(f.read(), before)  # source READ-ONLY, forever

    def test_managed_source_pools_without_any_codexhome(self):
        """(a) the live incident shape: orca holds the account, our
        codex-homes does NOT — the old code refused; now the managed source
        pools under the homes-prepare email slug."""
        self._plant_orca_managed("a1", email="live@x.test",
                                 account_id="acct-live")
        self._daemon([_orca_acct("a1", "live@x.test", "acct-live")], "a1")
        rc, out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._pool_files(), ["codex-live-x-test.json"])
        self.assertIn("orca-managed", out)
        self._assert_no_secrets(out + err)

    def test_fallback_to_codexhomes_when_orca_has_no_managed_home(self):
        """(b) no managed dir for the selection: today's leg verbatim — the
        codexhome copy pools, and the result says which source answered."""
        _path, auth, _ = self._plant("work-home", email="w@x.test")
        self._daemon([_orca_acct("a1", "w@x.test", "acct-work-home")], "a1")
        res = codexhomes.codex_sync_orca()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["source"], "codex-homes")
        with open(os.path.join(codexhomes.pool_dir(),
                               "codex-work-home.json")) as f:
            self.assertEqual(json.load(f)["refresh_token"],
                             auth["tokens"]["refresh_token"])

    def test_managed_marker_mismatch_falls_back_to_codexhomes(self):
        """A managed dir whose ownership marker names ANOTHER account is not
        orca's dir for this selection (orca's own assert law) — fall back."""
        self._plant("work-home", email="w@x.test")
        self._plant_orca_managed("a1", email="w@x.test",
                                 account_id="acct-work-home",
                                 marker="a2", token_tag="orca-live")
        self._daemon([_orca_acct("a1", "w@x.test", "acct-work-home")], "a1")
        res = codexhomes.codex_sync_orca()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["source"], "codex-homes")
        with open(os.path.join(codexhomes.pool_dir(),
                               "codex-work-home.json")) as f:
            self.assertEqual(json.load(f)["refresh_token"],
                             "fake-refresh-token-work-home")

    def test_managed_identity_mismatch_falls_back_to_codexhomes(self):
        """A managed auth carrying a DIFFERENT account than orca selects is
        never trusted — the fallback still matches by identity, so at worst
        a stale copy of the RIGHT account pools, never a wrong one."""
        self._plant("work-home", email="w@x.test")
        self._plant_orca_managed("a1", email="w@x.test",
                                 account_id="acct-someone-else",
                                 token_tag="orca-live")
        self._daemon([_orca_acct("a1", "w@x.test", "acct-work-home")], "a1")
        res = codexhomes.codex_sync_orca()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["source"], "codex-homes")
        with open(os.path.join(codexhomes.pool_dir(),
                               "codex-work-home.json")) as f:
            self.assertEqual(json.load(f)["refresh_token"],
                             "fake-refresh-token-work-home")

    def test_managed_stale_exp_warns_never_refuses(self):
        """The stale-exp law applies to whichever source is chosen: a past
        exp in the managed copy is a WARN with the orca-side cure, never a
        refusal."""
        self._plant_orca_managed("a1", email="s@x.test",
                                 account_id="acct-s", exp_offset=-60)
        self._daemon([_orca_acct("a1", "s@x.test", "acct-s")], "a1")
        rc, out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 0, err)
        self.assertIn("WARN", out)
        self.assertIn("re-run sync-orca", out)
        self.assertEqual(self._pool_files(), ["codex-s-x-test.json"])
        self._assert_no_secrets(out + err)

    def test_unchanged_prev_key_managed_source_skips_rewrite(self):
        """The --watch anti-churn seam holds on the managed leg too — even
        with NO codexhome row to carry the pooled linkage."""
        self._plant_orca_managed("a1", email="w@x.test", account_id="acct-w")
        self._daemon([_orca_acct("a1", "w@x.test", "acct-w")], "a1")
        first = codexhomes.codex_sync_orca()
        self.assertTrue(first.get("ok"), first)
        self.assertEqual(first["source"], "orca-managed")
        stamp = os.stat(first["path"]).st_mtime_ns
        second = codexhomes.codex_sync_orca(prev_key=first["key"])
        self.assertTrue(second.get("unchanged"), second)
        self.assertEqual(second["source"], "orca-managed")
        self.assertEqual(os.stat(first["path"]).st_mtime_ns, stamp)

    # -- the FRESHNESS RUNG (incident 2026-08-04 19:10Z: a July-26 managed
    # copy pooled over an 18:17Z login grant; the codex family went
    # auth-dark 11 minutes until the fresher bytes were restored) ----------

    def _incident(self):
        """The live incident in miniature: the codexhome's login bytes are
        POOLED (fresh), orca's managed copy of the same account is a day
        OLDER with different token bytes, and orca selects the account.
        Returns (pool_file, managed_auth_path)."""
        self._plant("work-home", email="w@x.test", account_id="acct-w")
        self.assertTrue(codexhomes.codex_pool("work-home").get("ok"))
        mpath, _auth, _ = self._plant_orca_managed(
            "a1", email="w@x.test", account_id="acct-w",
            token_tag="orca-stale")
        past = time.time() - 86400
        os.utime(mpath, (past, past))
        self._daemon([_orca_acct("a1", "w@x.test", "acct-w")], "a1")
        return (os.path.join(codexhomes.pool_dir(), "codex-work-home.json"),
                mpath)

    def test_stale_managed_refuses_over_fresher_pool(self):
        dest, mpath = self._incident()
        stamp = os.stat(dest).st_mtime_ns
        rc, _out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 1)
        self.assertIn("FRESHER", err)
        # BOTH timestamps named, rendered exactly as the verb renders them
        self.assertIn(codexhomes._utc(os.stat(mpath).st_mtime), err)
        self.assertIn(codexhomes._utc(os.stat(dest).st_mtime), err)
        self.assertIn("--force-managed", err)          # the override, named
        self.assertIn("HELM_CODEX_FORCE_MANAGED", err)
        self.assertEqual(os.stat(dest).st_mtime_ns, stamp)   # untouched
        with open(dest) as f:                          # still OUR fresh bytes
            self.assertEqual(json.load(f)["refresh_token"],
                             "fake-refresh-token-work-home")
        self._assert_no_secrets(err)

    def test_fresher_managed_pools_over_older_pool(self):
        """The mirror image: the POOL file is the older artifact — the
        managed copy is the genuine live bytes and pools through."""
        self._plant("work-home", email="w@x.test", account_id="acct-w")
        self.assertTrue(codexhomes.codex_pool("work-home").get("ok"))
        dest = os.path.join(codexhomes.pool_dir(), "codex-work-home.json")
        past = time.time() - 86400
        os.utime(dest, (past, past))
        self._plant_orca_managed("a1", email="w@x.test", account_id="acct-w",
                                 token_tag="orca-live")
        self._daemon([_orca_acct("a1", "w@x.test", "acct-w")], "a1")
        res = codexhomes.codex_sync_orca()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["source"], "orca-managed")
        with open(dest) as f:
            self.assertEqual(json.load(f)["refresh_token"],
                             "fake-refresh-token-orca-live")

    def test_equal_mtime_managed_pools(self):
        """Equal is NOT older — only a strictly-older managed copy refuses."""
        self._plant("work-home", email="w@x.test", account_id="acct-w")
        self.assertTrue(codexhomes.codex_pool("work-home").get("ok"))
        dest = os.path.join(codexhomes.pool_dir(), "codex-work-home.json")
        mpath, _a, _ = self._plant_orca_managed(
            "a1", email="w@x.test", account_id="acct-w",
            token_tag="orca-live")
        ns = os.stat(dest).st_mtime_ns
        os.utime(mpath, ns=(ns, ns))       # ns-exact, no float round-trip
        self._daemon([_orca_acct("a1", "w@x.test", "acct-w")], "a1")
        res = codexhomes.codex_sync_orca()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["source"], "orca-managed")

    def test_identical_bytes_skip_precedes_freshness_rung(self):
        """A byte-identical managed copy answers unchanged even when its
        mtime reads stale — an identical copy cannot regress anything."""
        mpath, _a, _ = self._plant_orca_managed("a1", email="w@x.test",
                                                account_id="acct-w")
        self._daemon([_orca_acct("a1", "w@x.test", "acct-w")], "a1")
        first = codexhomes.codex_sync_orca()
        self.assertTrue(first.get("ok"), first)
        past = time.time() - 86400
        os.utime(mpath, (past, past))      # the source now reads STALE
        second = codexhomes.codex_sync_orca()
        self.assertTrue(second.get("unchanged"), second)   # not a refusal

    def test_force_managed_flag_overrides_the_rung(self):
        dest, _mpath = self._incident()
        rc, out, err = self._cmd("sync-orca", "--force-managed")
        self.assertEqual(rc, 0, err)
        with open(dest) as f:              # the deliberate regression landed
            self.assertEqual(json.load(f)["refresh_token"],
                             "fake-refresh-token-orca-stale")
        self._assert_no_secrets(out + err)

    def test_force_managed_env_overrides_the_rung(self):
        dest, _mpath = self._incident()
        os.environ["HELM_CODEX_FORCE_MANAGED"] = "1"
        res = codexhomes.codex_sync_orca()
        self.assertTrue(res.get("ok"), res)
        with open(dest) as f:
            self.assertEqual(json.load(f)["refresh_token"],
                             "fake-refresh-token-orca-stale")

    def test_watch_tick_after_freshness_refusal_goes_unchanged(self):
        """The refusal must not spam a --watch loop: it carries the key, so
        the next tick's unchanged seam (same selection, still pooled)
        answers silent instead of re-printing the refusal."""
        self._incident()
        first = codexhomes.codex_sync_orca()
        self.assertEqual(first.get("rc"), 1, first)
        self.assertIn("key", first)
        second = codexhomes.codex_sync_orca(prev_key=first["key"])
        self.assertTrue(second.get("unchanged"), second)

    def test_cli_junk_tail_refuses(self):
        rc, _out, err = self._cmd("sync-orca", "--bogus")
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg", err)


# The live `orca account list` shape (2026-08-04), Claude section INCLUDED —
# its own `(active)` marker is the leak the parser's section scoping exists
# to stop.
_CLI_ROSTER = ("Managed Claude accounts (2):\n"
               "  claude-a@x.test\n"
               "  claude-b@x.test (active)\n"
               "\n"
               "Managed Codex accounts (3):\n"
               "  work@x.test (active)\n"
               "  other@x.test\n"
               "  third@x.test\n")


class CliAccountsParserTest(unittest.TestCase):
    """_parse_cli_accounts — the CLI half of the dual-source snapshot."""

    def test_active_marker_and_multiple_accounts(self):
        codex, err = codexhomes._parse_cli_accounts(_CLI_ROSTER)
        self.assertIsNone(err)
        self.assertEqual([a["email"] for a in codex["accounts"]],
                         ["work@x.test", "other@x.test", "third@x.test"])
        self.assertEqual(codex["activeAccountId"], "work@x.test")
        self.assertEqual(codex["_helm_source"], "cli")

    def test_claude_sections_active_never_leaks_into_codex(self):
        codex, err = codexhomes._parse_cli_accounts(
            "Managed Claude accounts (1):\n"
            "  claude-only@x.test (active)\n"
            "Managed Codex accounts (1):\n"
            "  codex-only@x.test\n")
        self.assertIsNone(err)
        self.assertIsNone(codex["activeAccountId"])
        self.assertEqual([a["email"] for a in codex["accounts"]],
                         ["codex-only@x.test"])

    def test_empty_codex_section_is_a_valid_zero_roster(self):
        # positive control FIRST: the same header shape with a row yields
        # that row — so the zero below means EMPTY, never a scoping miss
        control, cerr = codexhomes._parse_cli_accounts(
            "Managed Codex accounts (1):\n  seeded@x.test\n")
        self.assertIsNone(cerr)
        self.assertEqual([a["email"] for a in control["accounts"]],
                         ["seeded@x.test"])
        codex, err = codexhomes._parse_cli_accounts(
            "Managed Claude accounts (1):\n  a@x.test (active)\n"
            "Managed Codex accounts (0):\n")
        self.assertIsNone(err)
        self.assertEqual(codex["_helm_source"], "cli")
        self.assertEqual(codex["accounts"], [])
        self.assertIsNone(codex["activeAccountId"])

    def test_missing_codex_section_is_a_parse_error(self):
        codex, err = codexhomes._parse_cli_accounts(
            "Managed Claude accounts (1):\n  a@x.test (active)\n")
        self.assertIsNone(codex)
        self.assertIn("Managed Codex accounts", err)


class SyncOrcaCliFallbackTest(_SyncOrcaBase):
    """The degraded snapshot route: accounts.list RPC dead -> `orca account
    list` roster (stubbed — no real orca in tests), and the honest two-line
    rc=2 when BOTH routes die. The rig inherits the HELM_ORCA_CLI=off pin;
    each fallback test points it at _stub_cli's script instead."""

    # (a) RPC timeout -> CLI fallback -> pooled by casefolded email match
    def test_rpc_timeout_falls_back_to_cli_and_pools_by_email(self):
        os.environ["HELM_CODEX_ACCOUNTS_TIMEOUT_S"] = "1"
        d = FakeDaemon(self.orca_dir, hang=True)
        self.addCleanup(d.close)
        self._plant("work-home", email="work@x.test")
        arglog = self._stub_cli(_CLI_ROSTER)
        res = codexhomes.codex_sync_orca()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["snapshot"], "cli")
        self.assertEqual(res["source"], "codex-homes")
        self.assertEqual(res["key"], "work@x.test")
        self.assertEqual(self._pool_files(), ["codex-work-home.json"])
        with open(arglog) as f:
            self.assertEqual(f.read().strip(), "account list")

    def test_rpc_timeout_error_names_method_when_cli_also_off(self):
        """The exact live-defect string, cured: a recv timeout must say
        WHICH method timed out, never the pane-resolution prose."""
        os.environ["HELM_CODEX_ACCOUNTS_TIMEOUT_S"] = "1"
        d = FakeDaemon(self.orca_dir, hang=True)
        self.addCleanup(d.close)
        rc, _out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 2)
        self.assertIn("orca runtime rpc accounts.list failed: timed out", err)
        self.assertNotIn("pane resolution", err)

    # (b) both routes dead: one rc=2 line carrying BOTH failures + the socket
    def test_both_routes_dead_rc2_names_both_failures_and_socket(self):
        gone = os.path.join(self.orca_dir, "gone.sock")
        with open(os.path.join(self.orca_dir, "orca-runtime.json"), "w") as f:
            json.dump({"runtimeId": "rt1", "authToken": "t",
                       "transports": [{"kind": "unix", "endpoint": gone}]}, f)
        self._stub_cli("", rc=1, stderr="cli-broke")
        rc, _out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 2)
        self.assertIn("accounts.list", err)      # the rpc failure, named
        self.assertIn("cli fallback:", err)      # the cli failure, named
        self.assertIn("cli-broke", err)
        self.assertIn("gone.sock", err)          # the socket, named
        self.assertNotIn("pane resolution", err)

    # (c) the silent-drop shape: connection closed with no reply at all
    def test_silent_drop_no_reply_names_the_method(self):
        d = FakeDaemon(self.orca_dir, raw=b"")
        self.addCleanup(d.close)
        rc, _out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 2)
        self.assertIn("orca runtime rpc accounts.list returned no reply", err)
        self.assertNotIn("pane resolution", err)

    # (c/d) an unknown-method ERROR reply surfaces the daemon's own message
    def test_unknown_method_error_reply_names_the_method(self):
        d = FakeDaemon(self.orca_dir,
                       error={"message": "Unknown method: accounts.list",
                              "code": "method_not_found"})
        self.addCleanup(d.close)
        rc, _out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 2)
        self.assertIn("orca runtime rpc accounts.list:", err)
        self.assertIn("Unknown method", err)
        self.assertNotIn("pane resolution", err)

    def test_flap_to_cli_answers_unchanged_without_rewrite(self):
        """The --watch survival law: an RPC-sourced sync followed by a
        CLI-fallback poll of the SAME selection answers unchanged — the
        email-first key survives the source flap, the pool file's mtime
        never moves, and a fresh one-shot over the CLI route agrees."""
        self._plant("work-home", email="work@x.test")
        d = FakeDaemon(self.orca_dir,
                       reply=_fake_snapshot(
                           [_orca_acct("a1", "work@x.test",
                                       "acct-work-home")], "a1"))
        self.addCleanup(d.close)
        first = codexhomes.codex_sync_orca()
        self.assertTrue(first.get("ok"), first)
        self.assertEqual(first["snapshot"], "rpc")
        self.assertEqual(first["key"], "work@x.test")
        stamp = os.stat(first["path"]).st_mtime_ns
        d.close()                                  # the daemon flaps out
        self._stub_cli(_CLI_ROSTER)
        second = codexhomes.codex_sync_orca(prev_key=first["key"])
        self.assertTrue(second.get("unchanged"), second)
        self.assertEqual(second["snapshot"], "cli")
        self.assertEqual(second["key"], first["key"])
        self.assertEqual(os.stat(first["path"]).st_mtime_ns, stamp)
        third = codexhomes.codex_sync_orca()       # one-shot, no prev_key
        self.assertTrue(third.get("unchanged"), third)
        self.assertEqual(os.stat(first["path"]).st_mtime_ns, stamp)

    def test_cli_roster_without_active_refuses_honestly(self):
        gone = os.path.join(self.orca_dir, "gone.sock")
        with open(os.path.join(self.orca_dir, "orca-runtime.json"), "w") as f:
            json.dump({"runtimeId": "rt1", "authToken": "t",
                       "transports": [{"kind": "unix", "endpoint": gone}]}, f)
        self._stub_cli("Managed Codex accounts (1):\n  work@x.test\n")
        rc, _out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 1)
        self.assertIn("no (active) codex account", err)
        self.assertNotIn("system-default", err)   # the RPC-only prose

    def test_one_shot_double_run_unchanged_managed(self):
        """The live acceptance in unit form: two bare sync-orca runs over a
        healthy RPC — the second answers unchanged (byte-identical pool) and
        the pool file's mtime proves no rewrite."""
        self._plant_orca_managed("a1", email="w@x.test", account_id="acct-w")
        self._daemon([_orca_acct("a1", "w@x.test", "acct-w")], "a1")
        rc, out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 0, err)
        # positive control: run 1 REALLY pooled — the file exists carrying
        # the selected identity, and the verdict line names the email
        self.assertIn("w@x.test", out)
        path = os.path.join(codexhomes.pool_dir(), "codex-w-x-test.json")
        with open(path) as f:
            self.assertEqual(json.load(f)["account_id"], "acct-w")
        stamp = os.stat(path).st_mtime_ns
        rc, out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 0, err)
        self.assertIn("unchanged", out)
        self.assertIn("codex-w-x-test.json", out)   # names the untouched file
        self.assertEqual(os.stat(path).st_mtime_ns, stamp)
        self._assert_no_secrets(out + err)


if __name__ == "__main__":
    unittest.main()
