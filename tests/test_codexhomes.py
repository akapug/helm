"""Hermetic tests for helm.codexhomes — HELM_HOME and HELM_CODEX_HOMES_DIR
both point at tmp dirs; fake JWTs are minted in-test. The real ~/.codex-homes
and the real seat pool are never read, no codex CLI is ever run."""
import base64
import contextlib
import fcntl
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests._tmphome import pin_suite_guard
# `seat` IS IMPORTED EXPLICITLY at module scope: the nameless-mint arm
# imports helm.seat_credentials, and the co-occurrence guard in
# tests/test_seat_facade_injection.py requires an accepted facade import
# in the same or an enclosing scope. It asserts nothing about import order.
from helm import codexhomes, seat, seat_credentials  # noqa: F401
from tests import socket_dir
from tests._fakeorca import FakeDaemon


def _b64seg(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims):
    """header.payload.sig — enough structure for unverified payload decode."""
    return _b64seg({"alg": "RS256", "typ": "JWT"}) + "." + _b64seg(claims) + ".fake-sig"


def _auth_json(tag, email="fake@example.com", plan="pro", plan_in="access",
               exp_offset=3600, account_id=None, acct_in="tokens",
               user_id=None):
    """One CLI-native auth.json dict + its exp. plan_in selects which token
    carries the plan claim (the premise reads access first; the live id_token
    carries it too — both paths must classify). acct_in plants the account id
    in tokens.account_id (today's CLI) or ONLY in the id_token
    chatgpt_account_id claim (a shape the CLI has emitted — the kimi-review
    FIX-1 fixture); acct_in="absent" plants it NOWHERE, a credential whose
    identity is UNKNOWN to `translate_codex_auth` (task/2514). tag keys the
    refresh token so byte-provenance is assertable across two stores holding
    the same account."""
    exp = int(time.time()) + exp_offset
    acct = account_id or ("acct-" + tag)
    id_auth, acc_auth = {}, {}
    (acc_auth if plan_in == "access" else id_auth)["chatgpt_plan_type"] = plan
    if acct_in == "id":
        id_auth["chatgpt_account_id"] = acct
    if user_id:
        # the QUOTA BEARER on a Team workspace: three members carry one
        # chatgpt_account_id and three chatgpt_user_ids
        id_auth["chatgpt_user_id"] = user_id
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


class CodexHomesBase(unittest.TestCase):
    """The codexhomes fixture: a scratch HELM_HOME and codex-homes root, the
    operator's env restored at tearDown, and the helpers `_plant` (a fake
    home with a CLI-native auth.json), `_cmd` (the `helm codex` door, output
    captured) and `_assert_no_secrets`.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

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
        # a seat mint refuses a contract it cannot write in full
        pin_suite_guard(self, self.tmp)

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


class CodexHomesTest(CodexHomesBase):
    """The codexhomes arms, on CodexHomesBase's fixture.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses CodexHomesBase."""

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
        # dedup: a second home of the SAME account does not pool blind — it
        # RETIRES the older spelling, one pool file per credential
        self._plant("claim-twin", email="claim@x.test",
                    account_id="acct-claim-only", acct_in="id")
        res2 = codexhomes.codex_pool("claim-twin")
        self.assertEqual(res2["also_pooled_as"], ["codex-claim-only.json"])
        self.assertEqual(res2["retired"], ["codex-claim-only.json"])
        # and the pooled roster shows the account ONCE, under the survivor
        by_file = {r["file"]: r for r in codexhomes.codex_pooled()}
        self.assertEqual(sorted(by_file), ["codex-claim-twin.json"])
        self.assertEqual(by_file["codex-claim-twin.json"]["account_id"],
                         "acct-claim-only")
        rows = {r["name"]: r for r in codexhomes.codex_list()}
        self.assertEqual(rows["claim-only"]["pooled"], "codex-claim-twin.json")

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

    def test_a_refresh_keeps_a_disabled_flag_the_operator_set(self):  # noqa: VACUOUS_ASSERTION — every assertFalse here is paired on the SAME result dict with an unconditional positive: assertFalse(unchanged) beside assertTrue(updated) plus the byte-equal access_token re-read, and the control's assertFalse(kept_disabled) beside assertIs(disabled, False) re-read from the file
        """gate-r2 P1, second door: `_pool_auth` wrote `disabled = False` on
        EVERY pass, so refreshing a credential an operator had deliberately
        parked armed it again behind their back — and the automatic follow
        rung turned that into an unattended re-arm.

        Drives the SHIPPED `helm codex pool` producer twice over one home,
        with the kill-switch set between the passes exactly the way an
        operator sets it (edit the field in the pooled file — nothing in helm
        writes it), and with a materially different second credential so the
        write really happens and the assertion is not about a skipped no-op.
        """
        self._plant("parked-home", email="parked@x.test")
        dest = codexhomes.codex_pool("parked-home")["path"]
        with open(dest) as f:
            rec = json.load(f)
        rec["disabled"] = True
        codexhomes._write_pool_atomic(dest, json.dumps(rec, indent=2) + "\n")
        # a genuinely newer credential: a different exp mints different tokens
        _p, auth, _e = self._plant("parked-home", email="parked@x.test",
                                   exp_offset=7200)

        res = codexhomes.codex_pool("parked-home")

        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res["updated"])
        self.assertFalse(res.get("unchanged"), res)   # control: it really wrote
        with open(dest) as f:
            after = json.load(f)
        # the refresh landed ...
        self.assertEqual(after["access_token"], auth["tokens"]["access_token"])
        # ... and the kill switch survived it
        self.assertIs(after["disabled"], True)
        self.assertTrue(res["kept_disabled"], res)
        self.assertIn("kill-switch", res["warn"])

        # CONTROL — blast radius is THIS METHOD ONLY (same temp HELM_HOME, one
        # extra home + two extra calls): the identical sequence WITHOUT the
        # operator's edit comes back disabled=False, so the preservation is
        # reading the destination and not hard-coding True. Without it the
        # assertions above would also pass over a `disabled = True` constant.
        self._plant("live-home", email="live@x.test")
        live = codexhomes.codex_pool("live-home")["path"]
        self._plant("live-home", email="live@x.test", exp_offset=7200)
        ctl = codexhomes.codex_pool("live-home")
        self.assertTrue(ctl["updated"])
        self.assertFalse(ctl["kept_disabled"], ctl)
        with open(live) as f:
            self.assertIs(json.load(f)["disabled"], False)

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
        # A GREEN GATE DELEGATES TO THE SEAT-LAUNCH MINT, AND THE MINT ARMS THE
        # AUTOCOMPACT TIMER: unstubbed, every admitted launch here wrote unit
        # files under ~/.config/systemd/user and ran the host's real
        # `systemctl --user daemon-reload` and `enable --now
        # helm-autocompact.timer` (measured on fab: ~4s each, 8.1-8.4s per
        # arm). The seam test_seat_spawn stubs; the arms below assert it is
        # reached exactly when the gate admits.
        self.timer = mock.patch.object(seat, "_ensure_autocompact_timer")
        self.ensure_timer = self.timer.start()
        self.addCleanup(self.timer.stop)

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
    def test_launch_refuses_when_all_pooled_not_ok(self):  # noqa: VACUOUS_ASSERTION — the absences (no claude tail, no timer call) sit beside the unconditional positives rc 1, REFUSE and the named fix; the timer seam's positive control is its sibling arms, which assert the SAME stub called once on every admitted launch
        self._plant("a", email="a@x.test")
        _rollout("a", 95.0)                      # near
        self._cmd("pool", "a")
        self._plant("b", email="b@x.test")       # unknown, unpooled
        rc, out, err = self._cmd("launch")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSE", err)
        self.assertIn("helm codex pool b", err)  # the concrete fix
        self.assertNotIn("claude --dangerously", out)  # no mint happened
        self.ensure_timer.assert_not_called()    # nor its lifecycle wiring

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
        self.ensure_timer.assert_called_once_with()   # the mint's own wiring

    def test_launch_force_overrides_refusal(self):
        self._plant("a", email="a@x.test")       # unknown (no rollout)
        self._cmd("pool", "a")
        rc, out, err = self._cmd("launch", "--force")
        self.assertEqual(rc, 0, err)
        self.assertIn("--force", err)
        self.ensure_timer.assert_called_once_with()
        # the canonical seat tail: plan-mode ENTRY denied, permissions bypassed
        self.assertIn("claude --disallowedTools EnterPlanMode Artifact Skill"
                      " 'Agent(fork)' --dangerously-skip-permissions", out)   # task/1941, task/2287, task/2559
        # task/2328: feedback never leaves helm — both env switches are the
        # last words before `claude`; the settings switch is a file, not a word
        self.assertIn(" DISABLE_FEEDBACK_COMMAND=1 DISABLE_BUG_COMMAND=1 claude"
                      " --disallowedTools ", out)
        self.assertNotIn("feedbackDrafts", out)

    def test_launch_bad_instance_flag_rc2(self):  # noqa: VACUOUS_ASSERTION — rc 2 and 'integer' in err are the unconditional positives; the timer seam's positive control is its sibling arms, which assert the SAME stub called once on every admitted launch
        rc, _, err = self._cmd("launch", "-i", "two")
        self.assertEqual(rc, 2)
        self.assertIn("integer", err)
        self.ensure_timer.assert_not_called()

    def test_gate_output_no_secrets(self):
        self._plant("a", email="a@x.test")
        _rollout("a", 10.0)
        self._cmd("pool", "a")
        rc, out, err = self._cmd("launch")
        self.assertEqual(rc, 0, err)
        self.ensure_timer.assert_called_once_with()
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
        gone = os.path.join(socket_dir(self), "gone.sock")
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

    def _short_rpc_bound(self):
        """The hung daemon never answers, so a bound's LENGTH proves nothing
        here: any bound that expires does. The HELM_CODEX_ACCOUNTS_TIMEOUT_S
        knob floors at a whole second, so these arms set the bound below the
        parser, and the knob's own arm tests how it parses. The arm that
        names the method asserts "after 0.2s" in the error text, so the
        shortened bound is shown to reach the rpc, not assumed."""
        p = mock.patch.object(codexhomes, "_accounts_timeout_s",
                              return_value=0.2)
        p.start()
        self.addCleanup(p.stop)

    def test_the_accounts_timeout_knob_reads_whole_seconds_with_a_floor(self):
        """The operator override the two timeout arms below no longer pass
        through: whole seconds, never under one, default on garbage."""
        for raw, want in (("3", 3), ("0", 1),
                          ("soon", codexhomes.ACCOUNTS_TIMEOUT_S)):
            os.environ["HELM_CODEX_ACCOUNTS_TIMEOUT_S"] = raw
            self.assertEqual(codexhomes._accounts_timeout_s(), want, raw)
        os.environ.pop("HELM_CODEX_ACCOUNTS_TIMEOUT_S")
        self.assertEqual(codexhomes._accounts_timeout_s(),
                         codexhomes.ACCOUNTS_TIMEOUT_S)

    # (a) RPC timeout -> CLI fallback -> pooled by casefolded email match
    def test_rpc_timeout_falls_back_to_cli_and_pools_by_email(self):
        self._short_rpc_bound()
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
        self._short_rpc_bound()
        d = FakeDaemon(self.orca_dir, hang=True)
        self.addCleanup(d.close)
        rc, _out, err = self._cmd("sync-orca")
        self.assertEqual(rc, 2)
        # PINNED TO THE STABLE HALVES, not the whole sentence. This asserted
        # the exact string "orca runtime rpc accounts.list failed: timed out",
        # which the wall-clock deadline deliberately replaced with a NAMED
        # expiry — so the arm reddened while the property it defends (the
        # error says WHICH method) was still perfectly satisfied. A
        # transcribed sentence reddens on every future rewording; the method
        # name and the failure class are what this test is actually about.
        self.assertIn("accounts.list", err)
        self.assertIn("deadline exceeded", err)
        self.assertIn("after 0.2s", err)   # the bound set above is the one spent
        self.assertNotIn("pane resolution", err)

    # (b) both routes dead: one rc=2 line carrying BOTH failures + the socket
    def test_both_routes_dead_rc2_names_both_failures_and_socket(self):
        gone = os.path.join(socket_dir(self), "gone.sock")
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
        gone = os.path.join(socket_dir(self), "gone.sock")
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


class CredFollowTest(_SyncOrcaBase):
    """`helm seat cred-follow` — the DAEMON-FREE rung that makes the proxy
    pool carry whichever codex account orca has active (task/2478, the owner's
    "why do I have to switch it by hand after I already switched in orca
    too?").

    The rig is _SyncOrcaBase for the pinned ORCA_USER_DATA_PATH + HELM_HOME and
    the managed-home planter; no daemon is started in this class, because not
    needing one is the whole point of the rung.
    """

    #: exactly the keys the proxy's pool record carries — nothing more (a
    #: stray key is a record the proxy may reject) and nothing less
    POOL_KEYS = {"access_token", "account_id", "disabled", "email", "expired",
                 "id_token", "last_refresh", "refresh_token", "type"}

    def _provenance(self, account, owner="managed"):
        """orca's own active-account record, in orca's shape and at orca's
        path. The rung reads this file and nothing else to learn the
        selection."""
        d = os.path.join(self.orca_dir, "codex-runtime-home")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "shared-runtime-auth-provenance.json")
        with open(path, "w") as f:
            json.dump({"owner": owner, "accountId": account}, f)
        return path

    def _seat_cmd(self, *args):
        """The SHIPPED operator door, dispatched exactly as `helm` dispatches
        it — not the producer function."""
        from helm import seat
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["cred-follow"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def _states(self, out):
        """The STATE of each rendered row.

        NEVER `assertIn("PRESENT", out)`: every row also prints a `detail`
        sentence, and a mutation run proved the point — with the PRESENT
        branch deleted the idempotence arm still passed, because the
        IMPORTED row's own prose contained the word PRESENT. The state is the
        first token of the indented row line and nothing else is."""
        return [line.split()[0] for line in out.splitlines()
                if line.startswith("  ") and not line.startswith("    ")]

    def _snapshot(self):
        """{name: (bytes, mtime_ns)} for the whole pool dir — the control
        every arm here states: a pool member this rung must not touch is
        byte-identical AND un-restatted after the run."""
        d = codexhomes.pool_dir()
        if not os.path.isdir(d):
            return {}
        out = {}
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            with open(path, "rb") as f:
                out[name] = (f.read(), os.stat(path).st_mtime_ns)
        return out

    def _bystander(self):
        """A pool member for an UNRELATED account, pre-pooled. It is the
        positive control on the pool observable: every "nothing was written"
        assertion below compares a dir that is provably non-empty and
        provably readable, so it cannot pass by listing an absent dir."""
        self._plant("bystander", email="bystander@x.test")
        res = codexhomes.codex_pool("bystander")
        self.assertTrue(res.get("ok"), res)
        return res["pooled"]

    # (1) positive: the shipped door, on orca's real file shapes -------------
    def test_apply_imports_orcas_active_account_into_the_pool(self):
        keep = self._bystander()
        src, auth, _exp = self._plant_orca_managed(
            "01ef0b6f-08e1-460a-bd35-8ae2d5687fb2", email="switched@x.test",
            plan="team", plan_in="id", account_id="acct-switched")
        self._provenance("01ef0b6f-08e1-460a-bd35-8ae2d5687fb2")
        before = self._snapshot()
        self.assertEqual(sorted(before), [keep])       # control: pool answers

        rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["IMPORTED"])
        dest = os.path.join(codexhomes.pool_dir(),
                            "codex-switched@x.test-team.json")
        self.assertTrue(os.path.exists(dest), sorted(self._snapshot()))
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o600)
        with open(dest) as f:
            rec = json.load(f)
        self.assertEqual(set(rec), self.POOL_KEYS)
        self.assertEqual(rec["type"], "codex")
        self.assertIs(rec["disabled"], False)
        self.assertEqual(rec["email"], "switched@x.test")
        self.assertEqual(rec["account_id"], "acct-switched")
        self.assertEqual(rec["last_refresh"], auth["last_refresh"])
        # the CREDENTIAL itself, byte-equal to orca's copy — compared here,
        # never printed, and never asserted through a repr
        t = auth["tokens"]
        self.assertTrue(rec["access_token"] == t["access_token"]
                        and rec["id_token"] == t["id_token"]
                        and rec["refresh_token"] == t["refresh_token"],
                        "the imported record does not carry orca's tokens")
        # orca's own file is READ-ONLY forever
        with open(src) as f:
            self.assertEqual(json.load(f), auth)
        # the unrelated pool member is untouched
        self.assertEqual(self._snapshot()[keep], before[keep])
        self._assert_no_secrets(out + err)

    # (2) idempotence: a second pass writes nothing -------------------------
    def test_second_pass_reports_PRESENT_and_writes_nothing(self):
        self._plant_orca_managed("a-live", email="switched@x.test",
                                 plan="team", plan_in="id",
                                 account_id="acct-switched")
        self._provenance("a-live")
        rc, out, _err = self._seat_cmd("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._states(out), ["IMPORTED"])   # control: it wrote
        after_first = self._snapshot()
        self.assertIn("codex-switched@x.test-team.json", after_first)

        rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["PRESENT"])
        self.assertEqual(self._snapshot(), after_first)
        self._assert_no_secrets(out + err)

    def test_the_same_account_under_another_name_is_still_PRESENT(self):
        """Identity is the ACCOUNT ID, never the file name or the email. On
        this host orca's active account was pooled as
        `codex-hen@example.com-team.json` while its auth.json translates to
        `codex-d@example.com-team.json` — one account, two addresses. A rung
        keyed on the name would import a second copy of a live credential and
        then let the proxy refresh one of the two into a 401."""
        self._plant("older-name", email="older@x.test", account_id="acct-one")
        pooled = codexhomes.codex_pool("older-name")["pooled"]
        self._plant_orca_managed("a-live", email="newer@x.test",
                                 account_id="acct-one")
        self._provenance("a-live")
        before = self._snapshot()
        # positive control on the observable this arm calls unchanged: the
        # pool holds exactly that one member and the dir is readable, so the
        # equality below cannot pass by comparing two absent dirs
        self.assertEqual(sorted(before), [pooled])

        rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["PRESENT"])
        self.assertIn(pooled, out)
        self.assertEqual(self._snapshot(), before)
        self._assert_no_secrets(out + err)

    # (3) negative on OTHERWISE-VALID input ---------------------------------
    def test_provenance_naming_an_account_with_no_auth_is_MISSING_AUTH(self):
        """Everything else is exactly the positive arm's input — a managed
        home is planted and the provenance is well-formed and owner=managed —
        so this can only fail at the auth-file gate. The planted home belongs
        to a DIFFERENT account than the one provenance names — and, the pool
        following orca's ROSTER, that other account still imports beside the
        MISSING-AUTH row (which is the one flagged active)."""
        keep = self._bystander()
        self._plant_orca_managed("a-live", email="live@x.test")
        self._provenance("a-switched-away")
        before = self._snapshot()

        rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self._states(out), ["MISSING-AUTH", "IMPORTED"])
        self.assertTrue([l for l in out.splitlines()
                         if l.startswith("  MISSING-AUTH")][0].endswith("(active)"))
        self.assertEqual(sorted(self._snapshot()),
                         sorted(list(before) + ["codex-live@x.test-pro.json"]))
        self.assertEqual(sorted(before), [keep])       # control: pool answers
        self._assert_no_secrets(out + err)

    # (4) a disabled member is REPORTED, never flipped -----------------------
    def test_a_disabled_pool_member_is_reported_and_never_flipped(self):  # noqa: VACUOUS_ASSERTION — the pool's exact one-member content is pinned before the run and the disabled flag is re-read from that same named file
        self._plant("off-home", email="off@x.test", account_id="acct-off")
        pooled = codexhomes.codex_pool("off-home")["pooled"]
        path = os.path.join(codexhomes.pool_dir(), pooled)
        with open(path) as f:
            rec = json.load(f)
        rec["disabled"] = True
        with open(path, "w") as f:
            json.dump(rec, f, indent=2)
        self._plant_orca_managed("a-live", email="off@x.test",
                                 account_id="acct-off")
        self._provenance("a-live")
        before = self._snapshot()
        # positive control: the pool holds exactly the disabled member, so
        # neither the equality nor the disabled=true re-read can pass on air
        self.assertEqual(sorted(before), [pooled])

        rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self._states(out), ["DISABLED"])
        self.assertIn(pooled, out)
        with open(path) as f:
            self.assertIs(json.load(f)["disabled"], True)   # NOT flipped
        self.assertEqual(self._snapshot(), before)
        self._assert_no_secrets(out + err)

    # (4b) gate-r2 P1: A FILE NAME IS NOT AN IDENTITY -----------------------
    #: the one email and one plan that two DIFFERENT accounts can share —
    #: measured on this host (one account pooled as codex-hen@example.com-team
    #: while its own auth.json translates to codex-d@example.com-team)
    SHARED_EMAIL = "switched@x.test"
    SHARED_PLAN = "team"

    def _stranger_on_the_name(self, account_id="acct-old", parked=True):
        """Account A, pooled under EXACTLY the file name orca's active account
        would mint, and parked with the proxy's kill-switch.

        Nothing here is hand-written: the pooled file is minted by the SHIPPED
        `helm codex pool` door over a codexhome whose NAME is the
        email-and-plan pair (that door names a file after the home, which is
        how two accounts on one address legitimately get two pool files), and
        the kill-switch is then edited in place — the only way it is ever set,
        since no helm verb writes that field.

        Returns (home-name, pooled-file-name, pooled-path)."""
        home = "%s-%s" % (self.SHARED_EMAIL, self.SHARED_PLAN)
        self._plant(home, email=self.SHARED_EMAIL, plan=self.SHARED_PLAN,
                    plan_in="id", account_id=account_id)
        res = codexhomes.codex_pool(home)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["pooled"], "codex-%s.json" % home)
        if parked:
            with open(res["path"]) as f:
                rec = json.load(f)
            rec["disabled"] = True
            codexhomes._write_pool_atomic(res["path"],
                                          json.dumps(rec, indent=2) + "\n")
        return home, res["pooled"], res["path"]

    def _orca_holds(self, account_id="acct-new", aid="a-live", **kw):
        """orca's ACTIVE account, on the shared email and plan."""
        kw.setdefault("email", self.SHARED_EMAIL)
        kw.setdefault("plan", self.SHARED_PLAN)
        kw.setdefault("plan_in", "id")
        self._plant_orca_managed(aid, account_id=account_id, **kw)
        self._provenance(aid)

    def test_the_name_another_account_holds_is_refused_never_overwritten(self):
        """THE P1 WITNESS (gate-r2). The destination was named from
        email-plus-plan alone, and presence was checked by the NEW account id,
        so an unattended pass over an account sharing one owner's address and
        plan REPLACED a different pooled account — and `_pool_auth`'s
        `disabled = False` cleared that account's kill-switch on the way.

        NEGATIVE ON AN OTHERWISE-VALID INPUT: the world below is armed to
        import in every other respect (managed home, owner=managed
        provenance, readable auth, an account id), so the only thing that can
        turn this into rc 1 with an untouched pool is the identity gate."""
        home, pooled, path = self._stranger_on_the_name()
        self._orca_holds()
        before = self._snapshot()
        self.assertEqual(sorted(before), [pooled])     # control: pool answers

        rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self._states(out), ["COLLISION"])
        self.assertIn("acct-old", out)      # the account that HOLDS the name
        self.assertIn("acct-new", out)      # the account orca has active
        self.assertIn(pooled, out)          # the standing pool file
        self.assertIn("auth.json", out)     # orca's source file, named too
        # A's bytes AND mtime are untouched, and its kill-switch still holds
        self.assertEqual(self._snapshot(), before)
        with open(path) as f:
            self.assertIs(json.load(f)["disabled"], True)
        self._assert_no_secrets(out + err)

        # CONTROL — blast radius is THIS METHOD ONLY (same temp HELM_HOME and
        # orca root, one unpool + one extra run): free the NAME and the very
        # same world imports. Without it the refusal above would also pass
        # against a fixture that had nothing to import, which is how a
        # "nothing happened" arm goes green over a gate that never ran.
        self.assertEqual(codexhomes.codex_unpool(home)["removed"], [pooled])
        rc, out, err = self._seat_cmd("--apply")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["IMPORTED"])
        self.assertIn(pooled, self._snapshot())

    def test_the_account_pooled_under_its_own_name_is_PRESENT(self):  # noqa: VACUOUS_ASSERTION — assertNotIn(stranger, out) sits on the same `out` as the unconditional assertIn(own, out) and the exact states equality, and the two-member pool is pinned before the run
        """The other half of "resolve by account id": orca's active account IS
        pooled — under a file of its own — while a DIFFERENT account sits on
        the email-and-plan name. Identity is the account id, so the rung
        resolves to B's own file and reports PRESENT.

        PRESENT, not a rewrite: this rung imports an account once and then
        lets the proxy own the copy — the proxy rotates both tokens away from
        orca's within a minute, so a re-import replaces a live credential with
        a stale one. "The destination is B's file" therefore means B's file is
        the one it looks at and leaves alone. A's file is never a candidate."""
        _home, stranger, stranger_path = self._stranger_on_the_name()
        self._plant("own-file", email=self.SHARED_EMAIL, plan=self.SHARED_PLAN,
                    plan_in="id", account_id="acct-new")
        own = codexhomes.codex_pool("own-file")["pooled"]
        self._orca_holds()
        before = self._snapshot()
        # control: the pool answers, and holds exactly these two members
        self.assertEqual(sorted(before), sorted([stranger, own]))

        rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["PRESENT"])
        self.assertIn(own, out)             # resolved to ITS OWN file, by id
        self.assertNotIn(stranger, out)     # A is not the destination
        self.assertEqual(self._snapshot(), before)
        with open(stranger_path) as f:
            self.assertIs(json.load(f)["disabled"], True)
        self._assert_no_secrets(out + err)

    def test_a_credential_missing_email_or_plan_refuses_and_names_the_field(self):
        """NO UNKNOWN FIELDS IN A NAME. `translate_codex_auth` spells an absent
        email or plan `unknown`, so two unrelated half-read credentials both
        name `codex-unknown-unknown.json` — a name that is a collision by
        construction. Each arm is otherwise a fully armed import (managed
        home, owner=managed provenance, a readable auth.json carrying an
        account id); only the one missing claim differs."""
        keep = self._bystander()
        before = self._snapshot()
        self.assertEqual(sorted(before), [keep])       # control: pool answers

        for n, (field, kw) in enumerate((("email", {"email": None}),
                                         ("plan", {"plan": None})), 1):
            with self.subTest(missing=field):
                self._orca_holds(account_id="acct-" + field,
                                 aid="a-" + field, **kw)

                rc, out, err = self._seat_cmd("--apply")

                self.assertEqual(rc, 1, out + err)
                # the roster accumulates: every incomplete account orca holds
                # is its own INCOMPLETE row, the active one first
                self.assertEqual(self._states(out), ["INCOMPLETE"] * n)
                self.assertIn(field, out)
                self.assertEqual(self._snapshot(), before)
                self.assertEqual([n for n in self._snapshot() if "unknown" in n],
                                 [])
                self._assert_no_secrets(out + err)

        # CONTROL — blast radius is THIS METHOD ONLY (same temp HELM_HOME and
        # orca root, one extra managed home + one run): with BOTH claims
        # present the identical pass imports, so the two refusals above came
        # from the missing-field gate and not from a world that could not
        # import at all.
        self._orca_holds(account_id="acct-whole", aid="a-whole")
        rc, out, err = self._seat_cmd("--apply")
        # rc 1: the two incomplete accounts orca still holds are faults that
        # stand beside the import (the roster is the world, not the selection)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self._states(out),
                         ["IMPORTED", "INCOMPLETE", "INCOMPLETE"])
        self.assertIn("codex-%s-%s.json" % (self.SHARED_EMAIL, self.SHARED_PLAN),
                      self._snapshot())

    def test_both_passes_surface_the_collision_refusal(self):
        """The refusal is ROOM-VISIBLE, not a quiet skip: both unattended
        doors this rung rides print the COLLISION row and neither writes.
        `cred_follow_noteworthy` is what carries that, and it reads
        `cred_follow_rc` — so a refusal state left out of FOLLOW_FAULTS would
        make the pass silent about a credential it declined to import."""
        from helm import proxywatch, seat
        _home, pooled, _path = self._stranger_on_the_name()
        self._orca_holds()
        before = self._snapshot()
        self.assertEqual(sorted(before), [pooled])     # control: pool answers

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(proxywatch, "health",
                               return_value={"seats": [], "ts": 0}), \
                mock.patch.object(proxywatch, "report_lines",
                                  return_value=["(health elided)"]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = proxywatch.cmd_proxywatch([])
        self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
        self.assertEqual(self._states(out.getvalue()), ["COLLISION"])
        self.assertIn("acct-old", out.getvalue())
        self.assertEqual(self._snapshot(), before)
        self._assert_no_secrets(out.getvalue() + err.getvalue())

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_minted_seats", return_value=[]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            seat._ensure(["--ensure"])
        self.assertEqual(self._states(out.getvalue()), ["COLLISION"])
        self.assertIn("acct-old", out.getvalue())
        self.assertEqual(self._snapshot(), before)
        self._assert_no_secrets(out.getvalue() + err.getvalue())

    # (5) an owner orca does not manage is skipped --------------------------
    def test_a_provenance_owner_other_than_managed_is_skipped_only_with_no_roster(self):
        """orca not driving the ACTIVE slot says nothing about the accounts
        orca HOLDS: the roster is pooled, no row is flagged active, and the
        pass says why. SKIPPED is a row only when there is no roster."""
        keep = self._bystander()
        self._provenance("a-live", owner="system-default")
        before = self._snapshot()

        rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["SKIPPED"])
        self.assertIn("system-default", out)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(sorted(before), [keep])       # control: pool answers
        self._assert_no_secrets(out + err)
        # now orca holds an account: the roster pools, nothing flagged active
        self._plant_orca_managed("a-live", email="live@x.test")
        rc, out, err = self._seat_cmd("--apply")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["IMPORTED"])
        self.assertNotIn("(active)", out)
        self.assertIn("no account flagged active", out)
        self.assertIn("system-default", out)
        self.assertEqual(sorted(self._snapshot()),
                         sorted([keep, "codex-live@x.test-pro.json"]))
        self._assert_no_secrets(out + err)

    def test_every_orca_held_account_is_pooled_and_the_active_one_is_flagged(self):  # noqa: VACUOUS_ASSERTION — the `0` imported and the two False flags sit beside three IMPORTED states, a four-file pool snapshot and an unconditional (active) suffix on the first row
        """The pool follows orca's ROSTER, not its selection: every managed
        account reaches the proxy, the selected one first and flagged."""
        keep = self._bystander()
        self._plant_orca_managed("a-one", email="one@x.test")
        self._plant_orca_managed("a-two", email="two@x.test")
        self._plant_orca_managed("a-three", email="three@x.test")
        self._provenance("a-two")

        rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["IMPORTED"] * 3)
        rows = [line for line in out.splitlines()
                if line.startswith("  ") and not line.startswith("    ")]
        self.assertIn("two@x.test", rows[0])
        self.assertTrue(rows[0].endswith("(active)"), rows[0])
        self.assertEqual([r.endswith("(active)") for r in rows],
                         [True, False, False])
        self.assertEqual(sorted(self._snapshot()),
                         sorted([keep, "codex-one@x.test-pro.json",
                                 "codex-two@x.test-pro.json",
                                 "codex-three@x.test-pro.json"]))
        res = codexhomes.cred_follow(apply=False)
        self.assertEqual((res["active"], res["imported"]), ("a-two", 0))
        self.assertEqual([r["state"] for r in res["rows"]], ["PRESENT"] * 3)
        self.assertEqual([r["active"] for r in res["rows"]], [True, False, False])
        # the second pass leaves every member alone
        before = self._snapshot()
        rc, out, err = self._seat_cmd("--apply")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["PRESENT"] * 3)
        self.assertEqual(self._snapshot(), before)
        self._assert_no_secrets(out + err)

    def test_team_siblings_sharing_a_workspace_id_each_reach_the_pool(self):
        """admin@ never reached the proxy: the rung keyed PRESENT on the
        workspace id d@ already carried. Identity is the CREDENTIAL —
        account id and user id — so the sibling is MISSING, then IMPORTED,
        while the member already pooled stays PRESENT under its own file."""
        self._plant("d-home", email="d@x.test", plan="team",
                    account_id="ws-1", user_id="user-d")
        pooled = codexhomes.codex_pool("d-home")["pooled"]
        self._plant_orca_managed("a-d", email="d@x.test", plan="team",
                                 account_id="ws-1", user_id="user-d")
        self._plant_orca_managed("a-admin", email="admin@x.test", plan="team",
                                 account_id="ws-1", user_id="user-admin")
        self._provenance("a-d")

        res = codexhomes.cred_follow(apply=False)
        self.assertEqual([(r["orca_account"], r["state"], r["pooled"])
                          for r in res["rows"]],
                         [("a-d", "PRESENT", pooled), ("a-admin", "MISSING", None)])

        rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["PRESENT", "IMPORTED"])
        self.assertEqual(sorted(self._snapshot()),
                         sorted([pooled, "codex-admin@x.test-team.json"]))
        # CONTROL: with no user claim on either side the same workspace id
        # IS the same credential, exactly as before this key existed
        self._plant_orca_managed("a-hen", email="hen@x.test", plan="team",
                                 account_id="ws-2")
        self._plant("hen-home", email="hen@x.test", plan="team",
                    account_id="ws-2")
        hen = codexhomes.codex_pool("hen-home")["pooled"]
        res = codexhomes.cred_follow(apply=True)
        by = {r["orca_account"]: r for r in res["rows"]}
        self.assertEqual((by["a-hen"]["state"], by["a-hen"]["pooled"]),
                         ("PRESENT", hen))
        self._assert_no_secrets(out + err)

    def test_an_absent_provenance_file_is_UNKNOWN_never_a_refusal(self):
        """A host with no orca must not turn the two passes this rung rides
        red, and "helm cannot say which account is active" is never the same
        claim as "none is"."""
        keep = self._bystander()
        before = self._snapshot()

        rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["UNKNOWN"])
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(sorted(before), [keep])       # control: pool answers

    # (7) the TAIL GUARD on the function that READS --apply -----------------
    def test_the_function_itself_refuses_a_junk_tail_before_it_writes(self):
        """gate-r1 finding: cmd_cred_follow read `--apply` by MEMBERSHIP and
        then WROTE the pool, while the only tail check lived up in the
        `helm seat` dispatcher — the shape
        tests.test_dispatch_honest.ApplyReadersAreGuarded refuses, because a
        dispatcher's guard covers only the door that dispatcher owns. The
        guard now lives ON the function, so the PRODUCER refuses junk whoever
        routes to it.

        NEGATIVE ON AN OTHERWISE-VALID INPUT: `--apply` is the real flag and
        the world below is fully armed to import, so the only thing that can
        turn this into rc 2 with an untouched pool is the guard under test.
        """
        keep = self._bystander()
        self._plant_orca_managed("a-live", email="switched@x.test",
                                 plan="team", plan_in="id",
                                 account_id="acct-switched")
        self._provenance("a-live")
        before = self._snapshot()
        self.assertEqual(sorted(before), [keep])       # control: pool answers

        for argv in (["--apply", "--bogus"], ["--bogus", "--apply"],
                     ["--bogus", "--help"]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = codexhomes.cmd_cred_follow(list(argv))
            self.assertEqual(rc, 2, (argv, out.getvalue(), err.getvalue()))
            self.assertIn("--bogus", err.getvalue(), argv)
            self.assertEqual(self._states(out.getvalue()), [], argv)
            self.assertEqual(self._snapshot(), before, argv)

        # CONTROL — blast radius is THIS METHOD ONLY (same temp HELM_HOME and
        # orca root, one extra call): the identical world with the junk token
        # removed DOES import. Without it the three refusals above would also
        # pass against a fixture that had nothing to write, which is how a
        # "nothing happened" arm goes green over a dead guard.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = codexhomes.cmd_cred_follow(["--apply"])
        self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
        self.assertEqual(self._states(out.getvalue()), ["IMPORTED"])
        self.assertIn("codex-switched@x.test-team.json", self._snapshot())
        self._assert_no_secrets(out.getvalue() + err.getvalue())

    def test_the_guard_does_not_eat_the_verbs_own_help(self):
        """The other half of guard_tail's contract: a CLEAN tail carrying
        --help prints this verb's usage, exits 0 and writes nothing — so the
        cure cannot be a guard that simply refuses everything."""
        keep = self._bystander()
        self._plant_orca_managed("a-live", email="switched@x.test",
                                 plan="team", plan_in="id",
                                 account_id="acct-switched")
        self._provenance("a-live")
        before = self._snapshot()
        self.assertEqual(sorted(before), [keep])       # control: pool answers

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = codexhomes.cmd_cred_follow(["--help"])

        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("cred-follow", out.getvalue())
        self.assertEqual(self._states(out.getvalue()), [])
        self.assertEqual(self._snapshot(), before)


    # (5b) THE LOCKED RE-PROOF ---------------------------------------------
    #
    # THE PROPERTY: round 2 cured the STATIC sequential collision (prove the
    # destination's identity before naming it, plain name or nothing, keep a
    # disabled flag). It did not serialize that PROOF with the WRITE. A follow
    # read a free destination, a competing pool import wrote B into it, and
    # this follow then replaced B with A; likewise a park taken after the
    # `disabled` read could be cleared by a writer already in flight. Two
    # unattended follow paths exist (the proxywatch pass and
    # `seat doctor --ensure`), so two writers really can interleave.
    #
    # THE ARMS BELOW DRIVE THE SHIPPED DOOR with a real second writer wedged
    # into the real window. Nothing about the proof or the refusal is faked:
    # only the TIMING is injected, at `pool_lock`, which is the one seam that
    # sits between the unlocked plan and the locked write.

    def _second_writer_at_the_lock(self, writer):
        """Patch `codexhomes.pool_lock` so ONE competing write lands in the
        exact window the finding describes: after this pass proved the
        destination, before it holds the boundary.

        The real lock is still taken — `wedged` delegates to it — so what the
        arm measures is the shipped re-read INSIDE the shipped boundary, not a
        substitute for either. ONE-SHOT on purpose: `_pool_auth` takes the
        same lock (that is the cure's other half), so a writer that fired on
        every acquire would recurse through its own pool write."""
        real = codexhomes.pool_lock
        fired = []

        @contextlib.contextmanager
        def wedged():
            if not fired:
                fired.append(True)
                writer()
            with real():
                yield

        return mock.patch.object(codexhomes, "pool_lock", wedged), fired

    def _competing_import(self, account_id, park=False):
        """The competing writer: the SHIPPED `helm codex pool` door mints a
        pool file under exactly the name the follow is about to use. It is a
        real import of a real codexhome, not a hand-written file, because the
        thing this arm has to be true about is what another helm writer can
        actually leave in that dir."""
        def go():
            home = "rival-%s" % account_id
            self._plant(home, email=self.SHARED_EMAIL, plan=self.SHARED_PLAN,
                        plan_in="id", account_id=account_id)
            # `codex_pool` names the file after the HOME, so it is re-pointed
            # at the follow's destination name through the same one write.
            canonical = "%s-%s" % (self.SHARED_EMAIL, self.SHARED_PLAN)
            src = os.path.join(codexhomes.homes_root(), home, "auth.json")
            res = codexhomes._pool_auth(src, canonical, "n/a")
            self.assertTrue(res.get("ok"), res)
            if park:
                with open(res["path"]) as f:
                    rec = json.load(f)
                rec["disabled"] = True
                codexhomes._write_pool_atomic(
                    res["path"], json.dumps(rec, indent=2) + "\n")
            self.rival = self._snapshot()
        return go

    def test_a_destination_written_between_the_proof_and_the_lock_refuses(self):  # noqa: VACUOUS_ASSERTION — no assertion here is about an absence: the pool is compared against the rival's pinned NON-EMPTY snapshot, the states list and the fired flag are unconditional positives, and the tail control re-runs the same world to IMPORTED
        """THE INTERLEAVING CONTROL: a second writer in the real window.

        NEGATIVE ON AN OTHERWISE-VALID INPUT: the world is armed to import in
        every respect the round-2 gates test — managed home, owner=managed
        provenance, readable auth, an account id, an email and a plan, and a
        destination name this pass PROVED free. The only thing that can turn
        it into rc 1 with B intact is the re-read inside the lock."""
        self._orca_holds(account_id="acct-new")
        before = self._snapshot()
        self.assertEqual(before, {})            # control: destination is free

        patch, fired = self._second_writer_at_the_lock(
            self._competing_import("acct-old"))
        with patch:
            rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(fired, [True], "the competing write never ran")
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self._states(out), ["CHANGED"])
        self.assertIn("acct-old", out)        # what it FOUND, named
        self.assertIn("acct-new", out)          # orca's active account
        # B SURVIVES, byte-for-byte and un-restatted
        self.assertEqual(self._snapshot(), self.rival)
        self._assert_no_secrets(out + err)

        # PRE-CURE: the same fixture lets A replace B. With the locked
        # re-read reverted, the plan's stale "the name is free" proof reaches
        # `_pool_auth`, which writes orca's bytes over the rival's credential
        # — the original unattended-overwrite class. Proven by mutation on the
        # fab (revert, run this module, restore), never left in the tree.
        #
        # CONTROL — blast radius is THIS METHOD ONLY (same temp HELM_HOME and
        # orca root, one unpool plus one extra run): free the destination and
        # the very same world imports. Without it the refusal above would also
        # pass over a fixture that had nothing to import.
        self.assertEqual(
            codexhomes.codex_unpool("codex-%s-%s.json"
                                    % (self.SHARED_EMAIL, self.SHARED_PLAN)
                                    )["removed"],
            ["codex-%s-%s.json" % (self.SHARED_EMAIL, self.SHARED_PLAN)])
        rc, out, err = self._seat_cmd("--apply")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["IMPORTED"])

    def test_a_park_taken_after_the_proof_is_honoured_not_cleared(self):  # noqa: VACUOUS_ASSERTION — the observable is the rival's pinned NON-EMPTY snapshot plus disabled re-read True from that same named file, beside the unconditional states equality and fired flag
        """THE PARK-AFTER-DISABLED TWIN. The `disabled` read is half of the
        same stale proof: this pass read a pool with no member for the account
        (so no kill-switch to report), and an operator parked it before the
        write landed.

        `_pool_auth` preserves the FLAG on a destination it replaces, so the
        uncured failure is quieter than a cleared switch and worse than it
        looks: the parked record's own credential is replaced with orca's, and
        the pass reports IMPORTED for a cred the proxy still cannot draw on.
        This arm pins the BYTES, which is what discriminates."""
        self._orca_holds(account_id="acct-new")
        self.assertEqual(self._snapshot(), {})   # control: nothing pooled yet

        patch, fired = self._second_writer_at_the_lock(
            self._competing_import("acct-new", park=True))
        with patch:
            rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(fired, [True], "the competing park never ran")
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self._states(out), ["CHANGED"])
        self.assertIn("disabled=true", out)
        # the park is honoured: the record is untouched and still parked
        self.assertEqual(self._snapshot(), self.rival)
        dest = os.path.join(codexhomes.pool_dir(), "codex-%s-%s.json"
                            % (self.SHARED_EMAIL, self.SHARED_PLAN))
        with open(dest) as f:
            self.assertIs(json.load(f)["disabled"], True)
        self._assert_no_secrets(out + err)

    def test_an_uncontended_follow_still_imports_through_the_boundary(self):
        """THE POSITIVE, through the same door: with nobody competing, the
        pool still moves to orca's active account exactly as round 2 pinned —
        and the boundary was really taken on the way.

        Counting the acquire is what makes this arm discriminating rather than
        a duplicate of the round-2 import arm: delete the `with pool_lock()`
        and the import still succeeds, so only the count goes red."""
        self._orca_holds(account_id="acct-new")
        real, taken = codexhomes.pool_lock, []

        @contextlib.contextmanager
        def counted():
            taken.append(True)
            with real():
                yield

        with mock.patch.object(codexhomes, "pool_lock", counted):
            rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["IMPORTED"])
        self.assertTrue(taken, "the follow wrote without taking the pool lock")
        dest = os.path.join(codexhomes.pool_dir(), "codex-%s-%s.json"
                            % (self.SHARED_EMAIL, self.SHARED_PLAN))
        with open(dest) as f:
            rec = json.load(f)
        self.assertEqual(rec["account_id"], "acct-new")
        self.assertIs(rec["disabled"], False)
        self._assert_no_secrets(out + err)

    def test_the_lock_file_is_not_in_the_dir_the_proxy_reloads(self):
        """The boundary must not become noise the proxy re-reads. CLIProxyAPI
        hot-reloads the auth-dir on every change; a lock file living in there
        would churn that watcher on every timer pass forever."""
        self._orca_holds(account_id="acct-new")
        rc, out, err = self._seat_cmd("--apply")
        self.assertEqual(rc, 0, out + err)            # control: it ran
        self.assertTrue(os.path.exists(codexhomes.pool_lock_path()))
        self.assertEqual(
            os.path.dirname(codexhomes.pool_lock_path()),
            os.path.dirname(codexhomes.pool_dir().rstrip(os.sep)))
        self.assertEqual(sorted(self._snapshot()),
                         ["codex-%s-%s.json" % (self.SHARED_EMAIL,
                                                self.SHARED_PLAN)])

    # (5b) THE WRITER THAT NEVER TOOK THE LOCK. Serializing the follow's
    # re-proof with its write covers the competitors that take the lock, and
    # the arms above drive one that COOPERATES: `_competing_import` writes
    # through `_pool_auth`, which takes the same lock. The production writer
    # that matters is `helm seat add codex`: a mint that scanned, removed and
    # wrote the auth-dir from `seat_provision` with its own glob and its own
    # `_write_private` would be a writer the lock cannot serialize, and a
    # seat add landing between the follow's LOCKED re-proof and its write
    # would still be replaced by orca's account. The mint therefore writes
    # through `codexhomes.pool_provision` under `pool_lock`; the arms below
    # drive the SHIPPED `helm seat add codex` as the competitor, in both
    # orders, and probe the lock itself.

    #: the shipped seat mint in a CHILD PROCESS: the production race is two
    #: helm processes (proxywatch's timer pass against an operator's shell),
    #: and flock is what stands between them — an in-process thread would be
    #: serialized by the threading half of the lock and prove nothing about
    #: the cross-process half. The marker file is touched the instant the
    #: child is about to enter the shipped door, so a window is timed from
    #: there and never from interpreter start-up.
    _SEAT_ADD_CHILD = (
        "import sys\n"
        "from helm import seat\n"
        "open(sys.argv[2], 'w').close()\n"
        "sys.exit(seat.cmd_seat(['add', 'codex', '--auth-from', sys.argv[1]]))\n")

    def _shared_name(self):
        return "codex-%s-%s.json" % (self.SHARED_EMAIL, self.SHARED_PLAN)

    def _rival_home(self, account_id, tag=None):
        """A codexhome for a RIVAL account on the shared email and plan — the
        exact input `helm seat add codex --auth-from` takes. plan_in="id"
        because `translate_codex_auth` reads the plan from the id_token, and
        the destination it names must be the follow's own. Returns the
        auth.json path; its refresh token is keyed by the home name, so an
        arm can tell whose bytes the pool holds without printing them."""
        src, _auth, _exp = self._plant(tag or "rival-%s" % account_id,
                                       email=self.SHARED_EMAIL,
                                       plan=self.SHARED_PLAN, plan_in="id",
                                       account_id=account_id)
        return src

    def _seat_add(self, src):
        """The SHIPPED operator door for the mint, dispatched exactly as
        `helm` dispatches it, in THIS process."""
        from helm import seat
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["add", "codex", "--auth-from", src])
        return rc, out.getvalue(), err.getvalue()

    def _seat_add_child(self, src):
        """The same door in a child process (see _SEAT_ADD_CHILD). Returns
        (proc, marker); the child inherits this fixture's HELM_HOME, pinned
        suite guard and orca root, so it writes into the SAME temp pool."""
        marker = os.path.join(self.tmp, "child-at-the-door")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.Popen(
            [sys.executable, "-c", self._SEAT_ADD_CHILD, src, marker],
            cwd=root, env=dict(os.environ), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc, marker

    def _holds_the_pool_lock(self):
        """Is `pool_lock` held by SOMEBODY right now — measured, from a fresh
        descriptor. flock is per open file description, so a non-blocking
        take here conflicts with the descriptor the door holds and answers
        EWOULDBLOCK exactly while the door is inside its boundary; the same
        probe answers False outside one (the tail control of the arm that
        uses it)."""
        fd = os.open(codexhomes.pool_lock_path(),
                     os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def test_a_seat_add_that_lands_before_the_lock_is_kept_and_the_follow_says_changed(self):  # noqa: VACUOUS_ASSERTION — the `{}` before the run is the free-destination control; every claim after it compares the pool against the mint's pinned NON-EMPTY snapshot, the states list, the two rcs and the fired flag are unconditional positives, and the tail control re-runs the same world to IMPORTED
        """SEAT ADD FIRST, with the SHIPPED mint as the competitor. The mint
        lands in the round-3 window (after this pass's unlocked plan, before
        it holds the boundary) and pools the rival under the follow's own
        destination name through its own locked door. The follow's locked
        re-read finds it and REFUSES; the mint's bytes survive byte-for-byte.

        Unlike `_competing_import` above, nothing here cooperates by
        construction: the competitor is `seat.cmd_seat(["add", ...])`, the
        production mint itself, and its bytes are what the re-read compares
        against."""
        self._orca_holds(account_id="acct-new")
        self.assertEqual(self._snapshot(), {})           # control: name free
        src = self._rival_home("acct-old")
        mint = {}

        def go():
            mint["rc"], mint["out"], mint["err"] = self._seat_add(src)
            mint["pool"] = self._snapshot()

        patch, fired = self._second_writer_at_the_lock(go)
        with patch:
            rc, out, err = self._seat_cmd("--apply")

        self.assertEqual(fired, [True], "the seat add never ran")
        self.assertEqual(mint["rc"], 0, mint["out"] + mint["err"])
        self.assertEqual(sorted(mint["pool"]), [self._shared_name()])
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self._states(out), ["CHANGED"])
        self.assertIn("acct-old", out)                   # what it FOUND
        self.assertIn("acct-new", out)                   # orca's active
        # the mint's credential survives, byte-for-byte and un-restatted
        self.assertEqual(self._snapshot(), mint["pool"])
        dest = os.path.join(codexhomes.pool_dir(), self._shared_name())
        with open(dest) as f:
            self.assertEqual(json.load(f)["account_id"], "acct-old")
        self._assert_no_secrets(out + err + mint["out"] + mint["err"])

        # CONTROL — blast radius is THIS METHOD ONLY: retire the mint's file
        # and the very same world imports.
        self.assertEqual(codexhomes.codex_unpool(self._shared_name())["removed"],
                         [self._shared_name()])
        rc, out, err = self._seat_cmd("--apply")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["IMPORTED"])

    def test_a_seat_add_inside_the_follows_locked_window_waits_then_refuses(self):  # noqa: VACUOUS_ASSERTION — `window["pool"] == {}` is the claim that nothing landed INSIDE the window and it sits beside the unconditional positives that the child was still running at the window's end, that the follow then IMPORTED (rc 0), that the child then exited 1 with the refusal naming both accounts, and that the pool holds orca's bytes under the one name
        """THE FINDING'S EXACT WINDOW, with the production competitor in a
        second process. The follow has taken the lock and re-proved its
        destination free; before its write, another helm process runs the
        shipped `helm seat add codex` for a rival account on the same name.

        CURED: the child BLOCKS on the pool's flock for as long as the follow
        holds it — it is still running when the window closes and the pool is
        still empty; the follow writes orca's account and IMPORTS; the child
        then acquires, finds the name held by ANOTHER account, and REFUSES
        without writing. Nothing of the rival's ever reaches the dir.

        WITHOUT THE ROUTING (a mint that never takes the lock): the child's
        mint lands INSIDE the window — the pool holds acct-old before the
        follow writes — and the follow's `_pool_auth` then replaces it with
        orca's account while the child reports rc 0 for a credential that no
        longer exists. Proven by mutation on the fab (revert
        `seat_provision`'s routing through `pool_provision`, run this module,
        restore), never left in the tree."""
        self._orca_holds(account_id="acct-new")
        self.assertEqual(self._snapshot(), {})           # control: name free
        src = self._rival_home("acct-old")
        real_write = codexhomes._write_pool_atomic
        window = {}

        def wedged(dest, text):
            # ONE-SHOT, at the follow's own write: inside its lock, after its
            # locked re-proof, before the bytes land.
            if not window:
                proc, marker = self._seat_add_child(src)
                window["proc"] = proc
                deadline = time.time() + 60
                while (not os.path.exists(marker) and proc.poll() is None
                       and time.time() < deadline):
                    time.sleep(0.02)
                window["at_the_door"] = os.path.exists(marker)
                try:
                    proc.wait(timeout=2.0)
                    window["still_running"] = False
                except subprocess.TimeoutExpired:
                    window["still_running"] = True
                window["pool"] = self._snapshot()
            return real_write(dest, text)

        with mock.patch.object(codexhomes, "_write_pool_atomic", wedged):
            rc, out, err = self._seat_cmd("--apply")
        self.assertIn("proc", window, "the follow never reached its write")
        cout, cerr = window["proc"].communicate(timeout=120)

        self.assertTrue(window["at_the_door"],
                        "the child never reached the shipped door: rc %r\n%s"
                        % (window["proc"].returncode, cerr))
        self.assertTrue(window["still_running"],
                        "the seat add FINISHED inside the follow's locked "
                        "window — it never waited for the lock")
        self.assertEqual(window["pool"], {},
                         "the seat add wrote inside the follow's window")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._states(out), ["IMPORTED"])
        self.assertEqual(window["proc"].returncode, 1, cout + cerr)
        self.assertIn("already holds account", cerr)
        self.assertIn("acct-new", cerr)
        self.assertIn("acct-old", cerr)
        self.assertEqual(sorted(self._snapshot()), [self._shared_name()])
        dest = os.path.join(codexhomes.pool_dir(), self._shared_name())
        with open(dest) as f:
            rec = json.load(f)
        self.assertEqual(rec["account_id"], "acct-new")
        # orca's bytes, not the rival's — compared, never printed
        self.assertTrue(rec["refresh_token"] == "fake-refresh-token-orca-a-live",
                        "the pool holds the wrong store's credential")
        self._assert_no_secrets(out + err + cout + cerr)

    def test_seat_add_holds_the_pool_lock_across_its_scan_removal_and_write(self):  # noqa: VACUOUS_ASSERTION — the one absence here is the probe's own control (`_holds_the_pool_lock()` False outside any boundary) and it sits beside three unconditional positives on the same probe: `[True]` at the removal, `[True]` at the write and True in the census reads
        """THE FALSIFIER, measured: show that the `seat add codex`
        production path acquires the identical `.pool.lock` across its scan,
        remove and write sequence. Each of the three is probed from a fresh
        descriptor (`_holds_the_pool_lock`) at the moment it happens, on an
        UNCONTENDED mint whose pool holds a stale spelling of the same
        account (to be retired) and a bystander (to be kept) — so the mint's
        own effects are pinned in the same breath, exactly as they were
        before the routing (tests.test_seat pins the printed lines too).

        WITHOUT THE ROUTING (a mint scanning with glob, removing with a bare
        `os.remove` and writing with `_write_private`) the census read and
        the atomic write never fire, and the one probe that does — at the
        removal — answers False."""
        d = codexhomes.pool_dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        codexhomes._write_pool_atomic(
            os.path.join(d, "codex-stale-b.json"),
            json.dumps({"type": "codex", "account_id": "acct-b",
                        "email": self.SHARED_EMAIL, "disabled": False}) + "\n")
        keep = self._bystander()
        src = self._rival_home("acct-b")
        held = {"read_pool": [], "remove": [], "write": []}
        real_read = codexhomes.read_pool
        real_remove = os.remove
        real_write = codexhomes._write_pool_atomic

        def read():
            held["read_pool"].append(self._holds_the_pool_lock())
            return real_read()

        def remove(path, *a, **kw):
            if os.path.dirname(path) == d:
                held["remove"].append(self._holds_the_pool_lock())
            return real_remove(path, *a, **kw)

        def write(dest, text):
            if os.path.dirname(dest) == d:
                held["write"].append(self._holds_the_pool_lock())
            return real_write(dest, text)

        with mock.patch.object(codexhomes, "read_pool", read), \
                mock.patch.object(os, "remove", remove), \
                mock.patch.object(codexhomes, "_write_pool_atomic", write):
            rc, out, err = self._seat_add(src)

        self.assertEqual(rc, 0, out + err)
        self.assertEqual(held["remove"], [True],
                         "the same-account retirement ran outside the lock "
                         "(or never through the door): %r" % held)
        self.assertEqual(held["write"], [True],
                         "the pool write ran outside the lock (or never "
                         "through the door): %r" % held)
        self.assertIn(True, held["read_pool"],
                      "no pool census was taken under the lock: %r" % held)
        self.assertFalse(self._holds_the_pool_lock())   # probe control
        # the mint's own effects, unchanged from before the routing
        self.assertEqual(sorted(os.listdir(d)),
                         sorted([keep, self._shared_name()]))
        self.assertIn("replaced same-account pooled cred: codex-stale-b.json",
                      out)
        self.assertIn("1 other pooled cred preserved", out)
        with open(os.path.join(d, self._shared_name())) as f:
            rec = json.load(f)
        self.assertEqual(set(rec), self.POOL_KEYS)
        self.assertEqual(rec["account_id"], "acct-b")
        self.assertIs(rec["disabled"], False)
        self.assertEqual(stat.S_IMODE(os.stat(
            os.path.join(d, self._shared_name())).st_mode), 0o600)
        self._assert_no_secrets(out + err)

    def test_a_seat_add_whose_name_another_account_holds_refuses_without_writing(self):
        """The door's IDENTITY RE-PROOF, uncontended. The follow has pooled
        orca's account under the shared name through the shipped door; a
        seat add for a rival account that translates to the SAME name must
        refuse, name both accounts and the file, and leave the standing
        credential byte-identical and un-restatted. A mint that rewrites its
        own name in place would overwrite it."""
        self._orca_holds(account_id="acct-new")
        rc, out, err = self._seat_cmd("--apply")
        self.assertEqual(rc, 0, out + err)
        before = self._snapshot()
        self.assertEqual(sorted(before), [self._shared_name()])  # control
        src = self._rival_home("acct-old")

        rc, out, err = self._seat_add(src)

        self.assertEqual(rc, 1, out + err)
        self.assertIn("already holds account", err)
        self.assertIn("acct-new", err)
        self.assertIn("acct-old", err)
        self.assertIn(self._shared_name(), err)
        self.assertEqual(self._snapshot(), before)
        self._assert_no_secrets(out + err)
        # CONTROL — blast radius is THIS METHOD ONLY: retire the standing
        # file and the very same mint succeeds, holding the rival.
        codexhomes.codex_unpool(self._shared_name())
        rc, out, err = self._seat_add(src)
        self.assertEqual(rc, 0, out + err)
        with open(os.path.join(codexhomes.pool_dir(),
                               self._shared_name())) as f:
            self.assertEqual(json.load(f)["account_id"], "acct-old")

    def test_a_re_add_of_a_parked_account_keeps_the_park(self):
        """The kill-switch survives the mint's refresh, as it survives every
        other refresh through this module: a standing SAME-account record
        carrying disabled=true keeps it while its bytes are refreshed from
        the newer source. A mint writing the translated record as is would
        carry no `disabled` field at all."""
        src = self._rival_home("acct-b")
        rc, out, err = self._seat_add(src)
        self.assertEqual(rc, 0, out + err)
        dest = os.path.join(codexhomes.pool_dir(), self._shared_name())
        with open(dest) as f:
            rec = json.load(f)
        rec["disabled"] = True
        codexhomes._write_pool_atomic(dest, json.dumps(rec, indent=2) + "\n")
        newer = self._rival_home("acct-b", tag="rival-acct-b-again")

        rc, out, err = self._seat_add(newer)

        self.assertEqual(rc, 0, out + err)
        with open(dest) as f:
            after = json.load(f)
        self.assertIs(after["disabled"], True)
        self.assertTrue(after["refresh_token"]
                        == "fake-refresh-token-rival-acct-b-again",
                        "the re-add did not refresh the record's bytes")
        self.assertEqual(sorted(os.listdir(codexhomes.pool_dir())),
                         [self._shared_name()])
        self._assert_no_secrets(out + err)

    def _nameless_home(self, tag="nameless"):
        """A codexhome on the shared email and plan whose tokens carry NO
        account id anywhere — `translate_codex_auth` admits it with
        account_id None. Its refresh token is keyed by `tag`, so an arm can
        tell whose bytes the pool holds without printing them."""
        src, _auth, _exp = self._plant(tag, email=self.SHARED_EMAIL,
                                       plan=self.SHARED_PLAN, plan_in="id",
                                       acct_in="absent")
        return src

    def test_a_seat_add_naming_no_account_never_replaces_a_known_pooled_account(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone calls are the NAMELESS control on the incoming record (and on the tail mint that admits it into an empty slot); the unconditional positives on the pool observable are the non-empty snapshot before the mint, the rc-1 refusal naming acct-b and the shared file, and the tail rc 0 whose written record carries the nameless home refresh-token tag
        """The door's third admission (task/2514). The shipped mint has
        pooled KNOWN account acct-b under the shared name; a seat add for a
        credential whose tokens name NO account id translates to the SAME
        name. Before this rung the collision test required BOTH ids, and
        the nameless credential fell through it and replaced acct-b with a
        record nobody can attribute. It must refuse, name the holder, the
        file and the reason `unknown-identity-cannot-replace-known`, and
        leave the standing credential byte-identical and un-restatted.

        CONTROLS: the incoming really is nameless (its translated record
        carries account_id None — if the fixture planted an id this arm
        would be the known-different arm in disguise); the pool is provably
        non-empty before the mint; and the TAIL control retires the standing
        file and re-runs the very same mint, which then succeeds into the
        empty slot — the refusal is about the KNOWN holder, not about the
        nameless credential, and an UNKNOWN identity into an EMPTY slot is
        admitted as before this rung."""
        rc, out, err = self._seat_add(self._rival_home("acct-b"))
        self.assertEqual(rc, 0, out + err)
        before = self._snapshot()
        self.assertEqual(sorted(before), [self._shared_name()])  # control
        self.assertEqual(json.loads(before[self._shared_name()][0])
                         ["account_id"], "acct-b")
        src = self._nameless_home()
        rec, fname, terr = seat_credentials.translate_codex_auth(src)
        self.assertIsNone(terr)
        self.assertIsNone(rec["account_id"])            # control: nameless
        self.assertEqual(fname, self._shared_name())    # control: same name

        rc, out, err = self._seat_add(src)

        self.assertEqual(rc, 1, out + err)
        self.assertIn("unknown-identity-cannot-replace-known", err)
        self.assertIn("already holds account", err)
        self.assertIn("acct-b", err)
        self.assertIn(self._shared_name(), err)
        self.assertEqual(self._snapshot(), before)
        self._assert_no_secrets(out + err)
        # TAIL CONTROL: an empty slot admits the same nameless mint
        codexhomes.codex_unpool(self._shared_name())
        rc, out, err = self._seat_add(src)
        self.assertEqual(rc, 0, out + err)
        with open(os.path.join(codexhomes.pool_dir(),
                               self._shared_name())) as f:
            rec = json.load(f)
        self.assertIsNone(rec["account_id"])
        self.assertEqual(rec["refresh_token"], "fake-refresh-token-nameless")

    def test_a_seat_add_of_the_same_known_account_still_refreshes(self):
        """KNOWN-SAME, the positive control of the arm above: the mint
        has pooled acct-b under the shared name, and a re-add of acct-b from
        a NEWER home carrying a different refresh token is a refresh —
        rc 0, the file now holds the newer bytes, the account id is still
        acct-b, and no other member appears. A refusal that keyed off the
        standing holder alone (rather than holder-versus-incoming) would
        refuse this too.

        CONTROL: the pool snapshot taken before the re-add DIFFERS after
        it — the same observable the refusing arm asserts unchanged — so a
        rung that silently skipped the write would fail here."""
        rc, out, err = self._seat_add(self._rival_home("acct-b"))
        self.assertEqual(rc, 0, out + err)
        before = self._snapshot()
        self.assertEqual(sorted(before), [self._shared_name()])  # control
        newer = self._rival_home("acct-b", tag="rival-acct-b-newer")

        rc, out, err = self._seat_add(newer)

        self.assertEqual(rc, 0, out + err)
        self.assertNotEqual(self._snapshot(), before)
        self.assertEqual(sorted(os.listdir(codexhomes.pool_dir())),
                         [self._shared_name()])
        with open(os.path.join(codexhomes.pool_dir(),
                               self._shared_name())) as f:
            rec = json.load(f)
        self.assertEqual(rec["account_id"], "acct-b")
        self.assertEqual(rec["refresh_token"],
                         "fake-refresh-token-rival-acct-b-newer")
        self._assert_no_secrets(out + err)

    # (6) the two passes it rides — driven, never read ----------------------
    def _armed(self):
        self._plant_orca_managed("a-live", email="switched@x.test",
                                 plan="team", plan_in="id",
                                 account_id="acct-switched")
        self._provenance("a-live")
        self.assertEqual(self._snapshot(), {})         # control: pool empty

    def test_the_proxywatch_pass_runs_the_rung(self):
        from helm import proxywatch
        self._armed()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(proxywatch, "health",
                               return_value={"seats": [], "ts": 0}), \
                mock.patch.object(proxywatch, "report_lines",
                                  return_value=["(health elided)"]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = proxywatch.cmd_proxywatch([])
        self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
        self.assertIn("cred-follow", out.getvalue())
        self.assertEqual(self._states(out.getvalue()), ["IMPORTED"])
        self.assertIn("codex-switched@x.test-team.json", self._snapshot())
        self._assert_no_secrets(out.getvalue() + err.getvalue())

    def test_seat_doctor_ensure_runs_the_rung(self):
        from helm import seat
        self._armed()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_minted_seats", return_value=[]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            seat._ensure(["--ensure"])
        self.assertIn("cred-follow", out.getvalue())
        self.assertEqual(self._states(out.getvalue()), ["IMPORTED"])
        self.assertIn("codex-switched@x.test-team.json", self._snapshot())
        self._assert_no_secrets(out.getvalue() + err.getvalue())


class FollowHasOneDoorTest(unittest.TestCase):
    """THE ONE DOOR, by census over the shipped source.

    proxywatch and `seat doctor --ensure` each drive an unattended follow.
    They are two CALLERS, and the cure is that they stay callers:
    the read-the-active-account, prove-the-destination, take-the-lock, write
    sequence lives in ONE function and neither supervisory module carries a
    pool write of its own. A second copy would be outside the boundary by
    construction, and no lock can help a writer that does not take it.

    Read with `ast` off the shipped files, never from memory."""

    #: every name in `codexhomes` that PUTS BYTES IN THE POOL
    POOL_WRITERS = ("_pool_auth", "_pool_auth_locked", "_write_pool_atomic",
                    "codex_pool", "codex_unpool", "pool_provision",
                    "_pool_provision_locked")
    #: the one door
    DOOR = "cred_follow"
    CALLERS = ("helm/proxywatch.py", "helm/seat_health.py")

    def _attrs(self, relpath):
        import ast
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            relpath)
        with open(path) as f:
            src = f.read()
        tree = ast.parse(src, filename=path)

        def spelling(node):
            """The name a call or reference resolves to, under EITHER
            spelling. `mod.f()` is an Attribute; `f()` after a
            `from .codexhomes import f` is a bare Name — and a scanner that
            saw only the first would call a from-imported pool write clean.
            The must-hit below is what caught exactly that: codexhomes calls
            its own writers bare, so an attribute-only census came back empty
            over the module that DEFINES them."""
            if isinstance(node, ast.Attribute):
                return node.attr
            return node.id if isinstance(node, ast.Name) else None

        called = {spelling(n.func) for n in ast.walk(tree)
                  if isinstance(n, ast.Call)} - {None}
        named = {spelling(n) for n in ast.walk(tree)
                 if isinstance(n, (ast.Attribute, ast.Name))} - {None}
        return called, named

    def test_both_callers_reach_the_follow_through_the_one_door(self):  # noqa: VACUOUS_ASSERTION — the expected side is the NON-EMPTY caller list itself, so this assertion cannot be satisfied by a scanner that resolved nothing
        reached = sorted(rel for rel in self.CALLERS
                         if self.DOOR in self._attrs(rel)[0])
        self.assertEqual(
            sorted(self.CALLERS), reached,
            "a supervisory pass no longer reaches the follow through %s — a "
            "second copy of the read-prove-write would be outside the lock "
            "by construction" % self.DOOR)

    def test_neither_caller_carries_a_pool_write_of_its_own(self):  # noqa: VACUOUS_ASSERTION — the emptiness claim is paired, in this method and on the same scanner, with an unconditional non-empty positive over helm/codexhomes.py
        carried = sorted((rel, w) for rel in self.CALLERS
                         for w in sorted(set(self.POOL_WRITERS)
                                         & self._attrs(rel)[1]))
        # POSITIVE CONTROL on the same observable, unconditional: the module
        # that DEFINES those writers must come back non-empty under the very
        # same scanner, or the emptiness above is the scanner's silence.
        self.assertTrue(
            sorted(set(self.POOL_WRITERS) & self._attrs("helm/codexhomes.py")[1]),
            "the attribute census found no pool writer in the module that "
            "defines them — it is blind, so the claim below proves nothing")
        self.assertEqual(
            [], carried,
            "a supervisory pass names a pool writer directly — the follow's "
            "read-prove-write must stay behind %s, which is where the lock "
            "is: %r" % (self.DOOR, carried))

    def test_the_census_sees_a_pool_writer_where_one_exists(self):
        """MUST-HIT. The two assertions above are emptiness claims, and an
        `ast` walk that resolved nothing would satisfy both. `codexhomes`
        itself calls its own pool writers, so the same scanner run over it has
        to come back non-empty — otherwise the green above is the scanner's
        silence, not the tree's shape."""
        called, named = self._attrs("helm/codexhomes.py")
        self.assertTrue(set(self.POOL_WRITERS) & named,
                        "the attribute census found no pool writer in the "
                        "module that defines them — the scanner is blind")
        self.assertIn("pool_lock", called,
                      "the census found no pool_lock acquire in codexhomes")

    # THE SEAT MINT'S HALF. The census above covers the two SUPERVISORY
    # callers and the follow's door. A seat mint reaching the pool from
    # `seat_provision._add` with a glob, a bare `os.remove` and
    # `_write_private` carries names outside POOL_WRITERS' vocabulary, so
    # that census cannot see it. This half reads `_add` itself: it must call
    # the mint's door and carry no raw touch of the auth-dir.

    #: the seat mint's door into the pool
    SEAT_ADD_DOOR = "pool_provision"
    #: a call by one of these names whose PATH argument names `auth_dir` is a
    #: raw pool touch — the shape `_add` carried before round 4. The path is
    #: positional 0, plus positional 1 for the two-path verbs; judging the
    #: whole argument list instead flags the config.yaml write, whose TEXT
    #: argument carries the auth-dir as a config value (measured on the fab:
    #: `_write_private(<config.yaml>, _config_yaml(.., auth_dir, ..))` is a
    #: write of the seat's config, not of the pool).
    RAW_TOUCHES = ("glob", "remove", "unlink", "replace", "rename",
                   "_write_private", "open", "listdir")
    PATH_ARGS = {"replace": (0, 1), "rename": (0, 1)}
    #: the raw shape of a seat mint's pool block — a glob scan, a bare
    #: removal and a `_write_private` into the auth-dir — as the census's
    #: must-hit. The name census sees the glob and
    #: the `_write_private` (both take `auth_dir`); it CANNOT see the
    #: `os.remove(pooled)`, whose argument is the loop variable — that
    #: removal's boundary is measured by the flock probe in
    #: CredFollowTest.test_seat_add_holds_the_pool_lock_across_its_scan_removal_and_write,
    #: not by this census, and the expected list below says so.
    _PRE_CURE_ADD = '''\
def _add(family, args, room=None, room_source=None):
    d = seat_dir(family)
    auth_dir = os.path.join(d, "auth")
    removed, kept = [], 0
    for pooled in glob.glob(os.path.join(auth_dir, "codex-*.json")):
        base = os.path.basename(pooled)
        if base == fname:
            continue  # the mint rewrites this spelling in place below
        try:
            with open(pooled) as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = None
        acct = old.get("account_id") if isinstance(old, dict) else None
        if acct and acct == rec.get("account_id"):
            os.remove(pooled)  # same account, stale spelling
            removed.append(base)
            continue
        kept += 1
    _write_private(os.path.join(auth_dir, fname),
                   json.dumps(rec, indent=2, sort_keys=False) + "\\n")
'''

    def _add_census(self, src):
        """(door_calls, raw_touches) over the ONE `_add` FunctionDef in src,
        under either call spelling (`mod.f()` or `f()`), as `_attrs` reads."""
        import ast
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_add")

        def spelling(node):
            if isinstance(node, ast.Attribute):
                return node.attr
            return node.id if isinstance(node, ast.Name) else None

        door, raw = [], []
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            name = spelling(n.func)
            if name == self.SEAT_ADD_DOOR:
                door.append(n.lineno)
            paths = [n.args[i] for i in self.PATH_ARGS.get(name, (0,))
                     if i < len(n.args)]
            if name in self.RAW_TOUCHES and any(
                    isinstance(a, ast.Name) and a.id == "auth_dir"
                    for arg in paths for a in ast.walk(arg)):
                raw.append((name, n.lineno))
        return door, raw

    def test_seat_add_reaches_the_pool_only_through_the_locked_door(self):  # noqa: VACUOUS_ASSERTION — the emptiness claim over the shipped `_add` is paired with the unconditional NON-EMPTY door call in the same function, and the next arm runs the same scanner over the pre-cure body and requires it to come back non-empty
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "helm/seat_provision.py")
        with open(path) as f:
            door, raw = self._add_census(f.read())
        self.assertTrue(
            door, "seat_provision._add no longer calls codexhomes.%s — the "
                  "seat mint's pool write is outside the lock by construction"
                  % self.SEAT_ADD_DOOR)
        self.assertEqual(
            [], raw, "seat_provision._add touches the pool auth-dir directly, "
                     "outside pool_lock: %r" % raw)

    def test_the_census_sees_the_raw_touches_the_pre_cure_mint_carried(self):
        """MUST-HIT. The clean bill above is an emptiness claim; the same
        scanner over the raw mint shape has to name the glob and the
        `_write_private`, or that clean bill is the scanner's silence."""
        door, raw = self._add_census(self._PRE_CURE_ADD)
        self.assertEqual([], door)                 # the old mint knew no door
        self.assertEqual(sorted(n for n, _ in raw), ["_write_private", "glob"],
                         "the census is blind to the raw touches the pre-cure "
                         "mint carried: %r" % raw)


if __name__ == "__main__":
    unittest.main()


class AnUnreadPoolIsUNKNOWNAtEveryCodexDoorTest(CodexHomesTest):
    """task/2480 R5 F1 — every `helm codex` door that touches the pool.

    Round 3's reader RAISED on any enumeration OSError but FileNotFoundError.
    The raise crossed a module whose header says every public function returns
    a JSON-able dict and "errors are {"error": "..."} — loud, attributed,
    never an exception across the API edge", so `helm codex pooled`,
    `capacity`, `list` and `launch` all answered a directory fault with a
    traceback. Swallowing it is the opposite failure and just as wrong: a pool
    nobody could read would print as an EMPTY pool, an UNPOOLED fleet, a
    capacity of ZERO, and a launch refusal pointing at `helm codex pool` for a
    problem pooling cannot fix.

    THE FAULT IS REAL AND UID-INDEPENDENT: the pool directory is replaced by a
    regular FILE, so `os.listdir` raises NotADirectoryError whether or not the
    suite runs as root. Nothing in the reader is patched."""

    def setUp(self):
        super().setUp()
        from helm import seat
        # the launch delegate refuses a seat contract it cannot write in full
        os.makedirs(seat.seat_dir("codex"), exist_ok=True)
        seat._write_private(os.path.join(seat.seat_dir("codex"), "config.yaml"),
                            "port: 8317\n", mode=0o600)
        seat._write_private(os.path.join(seat.seat_dir("codex"), "token"),
                            "gate-test-token\n", mode=0o600)
        self.pool = codexhomes.pool_dir()

    def _plant_and_pool(self):
        """The fixture lives HERE, not in setUp: this class inherits
        `CodexHomesTest`, so every arm THAT class defines also runs under this
        setUp — and a setUp that plants a home and pools it rewrites what four
        of those arms are measuring (a roundtrip that counts two pooled files
        found three). A helper each arm calls leaves the inherited suite
        exactly as it was."""
        self._plant("cxA", email="a@x.test", plan="pro")
        self.assertTrue(codexhomes.codex_pool("cxA").get("ok"))

    def _break_the_pool(self):
        shutil.rmtree(self.pool)
        with open(self.pool, "w") as f:
            f.write("not a directory")
        with self.assertRaises(OSError):   # the fixture's own premise
            os.listdir(self.pool)

    def test_the_readouts_say_UNKNOWN_instead_of_empty_unpooled_or_zero(self):
        self._plant_and_pool()
        # THE CONTROLS COME FIRST, through the same four doors over the same
        # pool, readable. Blast radius: four extra calls over one real pooled
        # file — they go red only if this fixture can never pool anything,
        # which is exactly what would make the UNKNOWNs below worthless.
        row = codexhomes.codex_list()[0]
        self.assertEqual(row["pooled"], "codex-cxA.json")
        self.assertFalse(row["pool_unread"])
        self.assertEqual(codexhomes.capacity()["unknown"], False)
        rc, out, _err = self._cmd("pooled")
        self.assertEqual(rc, 0)
        self.assertIn("codex-cxA.json", out)

        self._break_the_pool()
        # `pooled: None` would be a MEASUREMENT — "this account is not in the
        # pool". Nothing was measured, and the row says so.
        row = codexhomes.codex_list()[0]
        self.assertIsNone(row["pooled"])
        self.assertTrue(row["pool_unread"])
        self.assertIn(self.pool, row["pool_error"])
        gate_row = codexhomes.usage_gate()[0]
        self.assertTrue(gate_row["pool_unread"])
        cap = codexhomes.capacity()
        self.assertTrue(cap["unknown"])
        self.assertIn(self.pool, cap["error"])
        rc, out, err = self._cmd("pooled")
        self.assertEqual(rc, 1)
        self.assertIn("UNKNOWN", err)
        self.assertNotIn("pool empty", out)
        rc, out, err = self._cmd("capacity")
        self.assertEqual(rc, 1)
        self.assertIn("UNKNOWN", err)
        self.assertNotIn("capacity 0", out)
        rc, out, _err = self._cmd("list")
        self.assertEqual(rc, 0)
        self.assertIn("pooled:?", out)     # never a bare "-"
        self._assert_no_secrets(out + err)

    def test_the_launch_gate_REFUSES_and_names_the_DIRECTORY_not_pooling(self):  # noqa: VACUOUS_ASSERTION — the two absences (no `no pooled cred reads ok` sentence, no minted claude line) sit beside four unconditional positives on the SAME stderr: rc 1, the word REFUSE, the pool PATH, and the phrase `DIRECTORY fault`; the closing control re-runs the same gate over a restored, pooled, ok-rollout pool and asserts rc 0  # noqa: ORPHANED_MOCK — the `_run_launch` double IS on the path: `cmd_codex('launch')` calls `launch_gate` and then `_run_launch` on rc 0, which is exactly the leg the --force and control legs drive; the walker entered from `codex_pool`, reached through a test helper, and so never modelled the verb dispatch table
        self._plant_and_pool()
        self._break_the_pool()
        rc, out, err = self._cmd("launch")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSE", err)
        self.assertIn(self.pool, err)
        self.assertIn("DIRECTORY fault", err)
        # the pre-cure refusal sent the operator to pool another account; that
        # sentence must NOT appear for a directory that would not enumerate.
        self.assertNotIn("no pooled cred reads ok", err)
        self.assertNotIn("claude --dangerously", out)   # no mint happened
        # --force still overrides, like every other verdict this gate reaches
        with mock.patch.object(codexhomes, "_run_launch", lambda rest: 0):
            rc2, _out2, err2 = self._cmd("launch", "--force")
        self.assertEqual(rc2, 0)
        self.assertIn("WARN", err2)
        # THE CONTROL: the same gate over a pool that reads, with an ok pooled
        # cred, admits. Blast radius: one rollout file plus one gate call — it
        # proves the refusal above is the directory's doing and not a gate
        # that can no longer admit anything.
        os.remove(self.pool)
        os.makedirs(self.pool)
        self.assertTrue(codexhomes.codex_pool("cxA").get("ok"))
        _rollout("cxA", 3.0)
        with mock.patch.object(codexhomes, "_run_launch", lambda rest: 0):
            rc3, _out3, err3 = self._cmd("launch")
        self.assertEqual(rc3, 0, err3)
        self.assertNotIn("REFUSE", err3)

    def test_pooling_REFUSES_before_it_writes_anything(self):
        """`_pool_auth`'s dupe scan is how a re-pool learns the same account
        already sits under another filename. An unread census cannot be told
        from an empty one, so "no dupes" would be a claim made out of an
        error — and the write would go ahead blind."""
        self._plant_and_pool()
        self._break_the_pool()
        res = codexhomes.codex_pool("cxA")
        self.assertIn("error", res)
        self.assertIn(self.pool, res["error"])
        self.assertIn("refusing to pool", res["error"])
        # NOTHING WAS WRITTEN: the path is still the regular file the fixture
        # put there, byte for byte.
        self.assertTrue(os.path.isfile(self.pool))
        with open(self.pool) as f:
            self.assertEqual(f.read(), "not a directory")
        rc, _out, err = self._cmd("pool", "cxA")
        self.assertEqual(rc, 1)
        self.assertIn(self.pool, err)
        # THE MUST-HIT: restored, the very same call writes again. Blast
        # radius: one pool write over one real home — it goes red only if this
        # path can never pool, which is what would make the refusal worthless.
        os.remove(self.pool)
        os.makedirs(self.pool)
        again = codexhomes.codex_pool("cxA")
        self.assertTrue(again.get("ok"), again)
        self.assertEqual(sorted(os.listdir(self.pool)), ["codex-cxA.json"])


class TheSyncWatchSurvivesAnUnreadableCycleTest(_SyncOrcaBase):
    """task/2480 R5 F1 at the watch loop. `_run_sync_orca(watch=True)` polls
    forever precisely so that transient faults are ridden out — a raise out of
    the pool reader on ONE tick killed the watcher outright on a transient
    EIO, and the pool then stopped tracking orca's selection until a human
    noticed a process that was not there."""

    def test_a_faulted_tick_is_reported_and_the_watch_polls_again(self):
        self._plant("work-home", email="w@x.test")
        self._daemon([_orca_acct("a1", "w@x.test", "acct-work-home")], "a1")
        pool = codexhomes.pool_dir()
        os.makedirs(pool, exist_ok=True)
        shutil.rmtree(pool)
        with open(pool, "w") as f:           # tick 1 faults, uid-independently
            f.write("not a directory")

        class _StopWatching(Exception):
            pass

        ticks = []

        def fake_sleep(_s):
            ticks.append(len(ticks))
            if len(ticks) == 1:
                os.remove(pool)              # the fault clears between polls
                os.makedirs(pool)
                return
            raise _StopWatching              # end the arm after tick 2

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(codexhomes.time, "sleep", fake_sleep), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(_StopWatching):
                codexhomes._run_sync_orca(watch=True)
        text = out.getvalue() + err.getvalue()
        # the watcher REACHED a second poll — that is the whole finding
        self.assertEqual(len(ticks), 2, text)
        # and the faulted cycle was REPORTED, not swallowed
        self.assertIn(pool, text)
        self.assertIn("not syncing", text)
        # THE MUST-HIT, and it is the second tick's own work: the recovered
        # cycle actually pooled the selection. Blast radius: one pool file —
        # it goes red only if this rig can never sync at all, which would make
        # the survival above a reading of a loop that does nothing.
        self.assertEqual(self._pool_files(), ["codex-work-home.json"])


class OneAdmissionRuleForEveryPoolWriterTest(_SyncOrcaBase):
    """task/2517, the sibling of task/2514. LAND 108 cured identity admission
    at `_pool_provision_locked` (seat add) only; `_pool_auth_locked` — the
    source-file door behind `helm codex pool`, `sync-orca` and
    `cred-follow` — admitted on its own and still let the first RPC
    `sync-orca` replace a KNOWN pooled account with an orca-managed
    credential naming NO account id. The cure is SHAPE: one function,
    `_admit_identity`, both writers ask it. These arms drive the SHIPPED
    sync-orca verb over the real AF_UNIX FakeDaemon; nothing about the
    pool, the managed home or the verdict is faked.

    The rig: KNOWN acct-b is pooled by the shipped `helm codex pool` door
    from a codexhome named by the email slug — the one codexhome carrying
    the shared email, so sync-orca names its destination after it and the
    two writers meet on ONE file. The orca-managed source is stamped
    strictly FRESHER than the pool file, so the freshness rung can never be
    what refuses."""

    EMAIL = "switched@x.test"
    PLAN = "team"

    def _known_holder(self, account_id="acct-b"):
        """(path, bytes, mtime_ns) of KNOWN `account_id` pooled under the
        name sync-orca will mint for the shared email."""
        home = codexhomes.pk.slug(self.EMAIL)
        self._plant(home, email=self.EMAIL, plan=self.PLAN, plan_in="id",
                    account_id=account_id)
        res = codexhomes.codex_pool(home)
        self.assertTrue(res.get("ok"), res)
        with open(res["path"], "rb") as f:
            data = f.read()
        self.assertEqual(json.loads(data)["account_id"], account_id)
        return res["path"], data, os.stat(res["path"]).st_mtime_ns

    def _orca_selects(self, account_id, acct_in="tokens", token_tag=None):
        """orca's daemon selects managed slot a-live on the shared email; its
        managed home (marker + auth.json) carries `account_id` where
        `acct_in` says (\"absent\" = NOWHERE, the nameless credential), keyed
        by `token_tag`, stamped fresher than anything in the pool. Returns
        the managed auth.json path."""
        msrc, _auth, _exp = self._plant_orca_managed(
            "a-live", email=self.EMAIL, plan=self.PLAN, plan_in="id",
            account_id=account_id, acct_in=acct_in, token_tag=token_tag)
        later = time.time_ns() + 2 * 10 ** 9
        os.utime(msrc, ns=(later, later))
        self._daemon([_orca_acct("a-live", self.EMAIL,
                                 None if acct_in == "absent" else account_id)],
                     "a-live")
        return msrc

    def _holder_intact(self, path, before, stamp):
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.stat(path).st_mtime_ns, stamp)
        self.assertEqual(self._pool_files(), [os.path.basename(path)])

    def test_a_nameless_sync_orca_never_replaces_a_known_pooled_account(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone calls are the NAMELESS control on the incoming record; the unconditional positives are the pooled holder read back as acct-b, the rc-1 refusal naming acct-b, the file and the reason, the holder bytes compared to a non-empty snapshot, the two watch ticks, and the known-same control below that REFRESHES the same file
        """THE WITNESS (LAND 108). acct-b is pooled under the
        name sync-orca mints; orca selects a managed credential on the same
        email whose tokens name NO account id, fresher than the pool file.
        On main the sync REPLACED acct-b with a record nobody can attribute
        (probe recorded in the commit). It must refuse rc 1, name the
        holder, the file and `unknown-identity-cannot-replace-known`, and
        leave the standing file byte-identical and un-restatted. Then
        `--watch` must SURVIVE the refusal: the watcher reaches a second
        poll, which answers unchanged-silent, and the pool is still intact.

        CONTROLS: the incoming really translates with account_id None (an
        id anywhere would make this the known-different arm); the managed
        source is provably fresher than the pool file (so the freshness rung
        is not the refuser); and `test_a_known_same_sync_orca_still_refreshes`
        drives the identical rig with a KNOWN-SAME identity and the same
        file CHANGES — a rule keyed off the holder alone would refuse both."""
        path, before, stamp = self._known_holder("acct-b")
        msrc = self._orca_selects(None, acct_in="absent", token_tag="nameless")
        rec, _fname, terr = seat_credentials.translate_codex_auth(msrc)
        self.assertIsNone(terr)
        self.assertIsNone(rec["account_id"])                # control: nameless
        self.assertGreater(os.stat(msrc).st_mtime_ns, stamp)  # control: fresher

        rc, out, err = self._cmd("sync-orca")

        self.assertEqual(rc, 1, out + err)
        self.assertIn("unknown-identity-cannot-replace-known", err)
        self.assertIn("already holds account", err)
        self.assertIn("acct-b", err)
        self.assertIn(os.path.basename(path), err)
        self._holder_intact(path, before, stamp)
        self._assert_no_secrets(out + err)

        # --watch SURVIVES the refusal: tick 0 prints it, the loop reaches a
        # second poll, and that poll answers unchanged-silent on the still-
        # pooled email (the anti-churn seam) with the holder still intact.
        class _StopWatching(Exception):
            pass

        ticks = []

        def fake_sleep(_s):
            ticks.append(len(ticks))
            if len(ticks) == 2:
                raise _StopWatching

        wout, werr = io.StringIO(), io.StringIO()
        with mock.patch.object(codexhomes.time, "sleep", fake_sleep), \
                contextlib.redirect_stdout(wout), \
                contextlib.redirect_stderr(werr):
            with self.assertRaises(_StopWatching):
                codexhomes._run_sync_orca(watch=True)
        text = wout.getvalue() + werr.getvalue()
        self.assertEqual(len(ticks), 2, text)     # the watcher polled again
        self.assertIn("unknown-identity-cannot-replace-known", text)
        self._holder_intact(path, before, stamp)
        self._assert_no_secrets(text)

    def test_a_known_same_sync_orca_still_refreshes(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual pair pins that the holder's bytes and mtime MOVED; the unconditional positives on the same file are rc 0, the record read back with account_id acct-b and the orca-live-b refresh-token tag, and the one-member pool listing
        """KNOWN-SAME, the positive control of the arm above: the same rig
        with orca's managed credential naming acct-b — the holder — under a
        different refresh token. rc 0, the SAME file now carries orca's
        bytes (the token tag proves whose), the account id is still acct-b,
        no other member appears.

        CONTROL: the observable the refusing arms hold unchanged — the
        holder's bytes and mtime — DIFFERS here, so a rule that silently
        skipped every write over a known holder would fail this arm."""
        path, before, stamp = self._known_holder("acct-b")
        self._orca_selects("acct-b", token_tag="orca-live-b")

        rc, out, err = self._cmd("sync-orca")

        self.assertEqual(rc, 0, out + err)
        with open(path, "rb") as f:
            after = f.read()
        self.assertNotEqual(after, before)
        self.assertNotEqual(os.stat(path).st_mtime_ns, stamp)
        rec = json.loads(after)
        self.assertEqual(rec["account_id"], "acct-b")
        self.assertEqual(rec["refresh_token"],
                         "fake-refresh-token-orca-live-b")
        self.assertEqual(self._pool_files(), [os.path.basename(path)])
        self._assert_no_secrets(out + err)

    def test_a_known_different_sync_orca_refuses_and_names_both_accounts(self):
        """KNOWN-DIFFERENT through the sync path — NEWLY PINNED here: on main
        no arm held it, and the same rig REPLACED acct-b with acct-new (two
        accounts on one address, one file name). Now the rule refuses rc 1,
        naming the holder, the incoming account and the file, and the
        standing credential is byte-identical and un-restatted.

        CONTROL: `test_a_known_same_sync_orca_still_refreshes` is the same
        rig with the ids agreeing and it writes; the only difference here is
        the incoming account id."""
        path, before, stamp = self._known_holder("acct-b")
        self._orca_selects("acct-new", token_tag="orca-live-new")

        rc, out, err = self._cmd("sync-orca")

        self.assertEqual(rc, 1, out + err)
        self.assertIn("already holds account", err)
        self.assertIn("acct-b", err)
        self.assertIn("acct-new", err)
        self.assertIn(os.path.basename(path), err)
        self._holder_intact(path, before, stamp)
        self._assert_no_secrets(out + err)

    def test_both_pool_writers_admit_identity_through_the_one_rule(self):
        """THE CENSUS, two ways.

        (1) BY DRIVING: `_admit_identity` is wrapped with a recorder that
        delegates to the real rule, and the two SHIPPED doors are run —
        `helm seat add codex --auth-from` (-> `_pool_provision_locked`, into
        an empty slot) and `helm codex sync-orca` (-> `_pool_auth_locked`,
        the known-same refresh of the file the seat add minted). Each door
        must reach the rule with the (holder, incoming) pair its world
        implies, and the pool must show both writes landed (the recorder is
        transparent).

        (2) BY SOURCE: read with `ast` off the shipped module, every function
        that calls `_write_pool_atomic` also calls `_admit_identity`, and
        that set is EXACTLY the two locked writers — so a future third
        writer that puts bytes in the pool without asking the rule is red
        here, whether or not any driving arm happens to reach it."""
        real = codexhomes._admit_identity
        seen = []

        def recording(holder, incoming):
            seen.append((holder, incoming))
            return real(holder, incoming)

        home = "%s-%s" % (self.EMAIL, self.PLAN)   # names seat add's own file
        src, _auth, _exp = self._plant(home, email=self.EMAIL, plan=self.PLAN,
                                       plan_in="id", account_id="acct-b")
        self._orca_selects("acct-b", token_tag="orca-live-b")
        from helm import seat
        with mock.patch.object(codexhomes, "_admit_identity", recording):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = seat.cmd_seat(["add", "codex", "--auth-from", src])
            self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
            self.assertEqual(seen, [(None, "acct-b")])      # provision door
            rc, sout, serr = self._cmd("sync-orca")
            self.assertEqual(rc, 0, sout + serr)
        self.assertEqual(seen, [(None, "acct-b"), ("acct-b", "acct-b")])
        pooled = "codex-%s.json" % home
        self.assertEqual(self._pool_files(), [pooled])
        with open(os.path.join(codexhomes.pool_dir(), pooled)) as f:
            self.assertEqual(json.load(f)["refresh_token"],
                             "fake-refresh-token-orca-live-b")

        import ast
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "helm", "codexhomes.py")
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)

        def calls(fn):
            return {n.func.id for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

        writers = {fn.name: calls(fn) for fn in tree.body
                   if isinstance(fn, ast.FunctionDef)
                   and "_write_pool_atomic" in calls(fn)}
        self.assertEqual(sorted(writers),
                         ["_pool_auth_locked", "_pool_provision_locked"])
        for name, called in sorted(writers.items()):
            self.assertIn("_admit_identity", called,
                          "%s puts bytes in the pool without asking the one "
                          "admission rule" % name)



class OneFileEachCredentialTest(CodexHomesBase):
    """The pool carries EXACTLY ONE file per credential, and a credential is
    account id AND user id (`codexhomes._member_key`), never the account id
    alone.

    Measured: one owner login sat in the pool under two spellings (one
    named after the home by `helm codex pool`, one after email-and-plan by
    the follow) and the refusal counted "6 cooling down" over five
    credentials; d@, hen@ and admin@ share one Team workspace
    id and the census collapsed them to one row, so admin@ never reached the
    proxy and the budget printed d@'s reading as admin@'s."""

    def _pool_disabled(self, fname):
        with open(os.path.join(codexhomes.pool_dir(), fname)) as f:
            return json.load(f)["disabled"]

    def test_team_members_sharing_a_workspace_id_are_distinct_members(self):
        self._plant("team-d", email="d@x.test", plan="team",
                    account_id="ws-1", user_id="user-d")
        self._plant("team-admin", email="admin@x.test", plan="team",
                    account_id="ws-1", user_id="user-admin")
        r1 = codexhomes.codex_pool("team-d")
        r2 = codexhomes.codex_pool("team-admin")
        self.assertTrue(r1.get("ok") and r2.get("ok"), (r1, r2))
        # the sibling is NOT a spelling of the same credential: nothing retired
        self.assertEqual((r2["retired"], r2["also_pooled_as"]), ([], None))
        self.assertEqual(sorted(os.listdir(codexhomes.pool_dir())),
                         ["codex-team-admin.json", "codex-team-d.json"])
        rows = {r["name"]: r for r in codexhomes.codex_list()}
        self.assertEqual(rows["team-d"]["pooled"], "codex-team-d.json")
        self.assertEqual(rows["team-admin"]["pooled"], "codex-team-admin.json")
        self.assertEqual((rows["team-d"]["user_id"], rows["team-admin"]["user_id"]),
                         ("user-d", "user-admin"))
        # POSITIVE CONTROL on the same workspace id: the SAME user under a
        # third home IS the same credential and retires the older spelling
        self._plant("team-d-again", email="d@x.test", plan="team",
                    account_id="ws-1", user_id="user-d")
        r3 = codexhomes.codex_pool("team-d-again")
        self.assertEqual(r3["retired"], ["codex-team-d.json"])
        self.assertEqual(sorted(os.listdir(codexhomes.pool_dir())),
                         ["codex-team-admin.json", "codex-team-d-again.json"])
        rows = {r["name"]: r for r in codexhomes.codex_list()}
        self.assertNotIn("team-d-again", rows)          # folded onto team-d
        self.assertEqual(rows["team-d"]["aliases"], ["team-d-again"])
        self.assertEqual(rows["team-d"]["pooled"], "codex-team-d-again.json")
        self.assertEqual(rows["team-admin"]["pooled"], "codex-team-admin.json")

    def test_retiring_a_parked_spelling_carries_the_park_to_the_survivor(self):
        self._plant("dup-a", email="dup@x.test", account_id="acct-dup",
                    user_id="user-dup")
        self.assertTrue(codexhomes.codex_pool("dup-a").get("ok"))
        self.assertFalse(self._pool_disabled("codex-dup-a.json"))  # born live
        path = os.path.join(codexhomes.pool_dir(), "codex-dup-a.json")
        with open(path) as f:
            rec = json.load(f)
        rec["disabled"] = True                      # the operator parks it
        with open(path, "w") as f:
            json.dump(rec, f)
        self._plant("dup-b", email="dup@x.test", account_id="acct-dup",
                    user_id="user-dup")
        res = codexhomes.codex_pool("dup-b")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["retired"], ["codex-dup-a.json"])
        self.assertTrue(res["kept_disabled"])
        self.assertEqual(os.listdir(codexhomes.pool_dir()), ["codex-dup-b.json"])
        self.assertTrue(self._pool_disabled("codex-dup-b.json"))
        self.assertIn("disabled=true", res["warn"])

    def test_a_RAISED_write_retires_nothing(self):
        """A measurement, armed. `_write_pool_atomic` can raise —
        a full disk, a pool dir that lost its write bit — and on a RE-KEY the
        destination is a new name: it is excluded from the dupes and it does
        not exist yet. Retiring first therefore left the credential with ZERO
        spellings in the pool, and the seat holding it cannot authenticate at
        all. The write is injected to raise; every prior spelling must stand.
        """
        self._plant("raise-a", email="raise@x.test", account_id="acct-raise",
                    user_id="user-raise")
        self.assertTrue(codexhomes.codex_pool("raise-a").get("ok"))
        src, _auth, _exp = self._plant("raise-b", email="raise@x.test",
                                       account_id="acct-raise",
                                       user_id="user-raise")
        with mock.patch.object(
                codexhomes, "_write_pool_atomic",
                side_effect=OSError(28, "No space left on device")) as spy:
            res = codexhomes._pool_auth(src, "raise-b", "fix")
        self.assertEqual(spy.call_count, 1,
                         "the injected write never ran, so this arm is about "
                         "a door that was never reached")
        self.assertIn("error", res)
        self.assertIn("NOTHING was retired", res["error"])
        # THE MEASUREMENT: the credential still has a pooled copy to serve.
        self.assertEqual(os.listdir(codexhomes.pool_dir()),
                         ["codex-raise-a.json"])
        # CONTROL, same pair, real write: the new spelling lands and the old
        # one IS retired, so the arm above is about the raise and not about a
        # door that stopped retiring.
        res = codexhomes._pool_auth(src, "raise-b", "fix")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["retired"], ["codex-raise-a.json"])
        self.assertEqual(os.listdir(codexhomes.pool_dir()),
                         ["codex-raise-b.json"])

    def test_a_write_that_does_not_read_back_retires_nothing(self):
        """A retirement is irreversible, so what licenses it is the survivor
        being ON DISK — not a write call that returned. Here the write
        returns and leaves other bytes; nothing may be retired."""
        self._plant("back-a", email="back@x.test", account_id="acct-back",
                    user_id="user-back")
        self.assertTrue(codexhomes.codex_pool("back-a").get("ok"))
        src, _auth, _exp = self._plant("back-b", email="back@x.test",
                                       account_id="acct-back",
                                       user_id="user-back")
        real = codexhomes._write_pool_atomic

        def other_bytes(dest, _text):
            real(dest, "{}\n")          # readable JSON, not what was asked for

        with mock.patch.object(codexhomes, "_write_pool_atomic",
                               side_effect=other_bytes) as spy:
            res = codexhomes._pool_auth(src, "back-b", "fix")
        self.assertEqual(spy.call_count, 1,
                         "the injected write never ran, so this arm is about "
                         "a door that was never reached")
        self.assertIn("error", res)
        self.assertIn("did not read back", res["error"])
        self.assertEqual(sorted(os.listdir(codexhomes.pool_dir())),
                         ["codex-back-a.json", "codex-back-b.json"])
        # CONTROL: the real write reads back, so the old spelling retires.
        res = codexhomes._pool_auth(src, "back-b", "fix")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["retired"], ["codex-back-a.json"])
        self.assertEqual(os.listdir(codexhomes.pool_dir()),
                         ["codex-back-b.json"])

    def test_the_seat_add_door_also_writes_before_it_retires(self):
        """THE SIBLING, one function over. `_pool_provision_locked` retires the
        account's other spellings too and excludes its own file name, so a
        seat add under a NEW name whose write raised lost the account's last
        pooled copy for exactly the same reason."""
        self._plant("prov-a", email="prov@x.test", account_id="acct-prov",
                    user_id="user-prov")
        self.assertTrue(codexhomes.codex_pool("prov-a").get("ok"))
        rec, _fname, err = _seat_translate(
            os.path.join(codexhomes.homes_root(), "prov-a", "auth.json"))
        # the mint this door refuses anything else from, proven present
        self.assertEqual((rec["type"], err), ("codex", None))
        with mock.patch.object(
                codexhomes, "_write_pool_atomic",
                side_effect=OSError(28, "No space left on device")) as spy:
            res = codexhomes.pool_provision(rec, "codex-prov-b.json")
        self.assertEqual(spy.call_count, 1,
                         "the injected write never ran, so this arm is about "
                         "a door that was never reached")
        self.assertIn("error", res)
        self.assertIn("NOTHING was retired", res["error"])
        self.assertEqual(os.listdir(codexhomes.pool_dir()),
                         ["codex-prov-a.json"])
        # CONTROL: the real write lands the new name AND retires the old one.
        res = codexhomes.pool_provision(rec, "codex-prov-b.json")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["removed"], ["codex-prov-a.json"])
        self.assertEqual(os.listdir(codexhomes.pool_dir()),
                         ["codex-prov-b.json"])

    def test_a_refused_write_retires_nothing(self):
        """The retirement rides the write: a freshness-rung refusal leaves
        every spelling standing (the pool must not lose a live copy to a
        pass that then wrote nothing)."""
        self._plant("keep-a", email="keep@x.test", account_id="acct-keep",
                    user_id="user-keep")
        self.assertTrue(codexhomes.codex_pool("keep-a").get("ok"))
        src, _auth, _exp = self._plant("keep-b", email="keep@x.test",
                                       account_id="acct-keep", user_id="user-keep")
        # a second spelling standing in the pool with FRESHER bytes than the
        # source (written past the door, as a hand copy would be), so the
        # freshness rung refuses the re-pool
        with open(src) as f:
            rec, _fname, _err = _seat_translate(src)
        rec["disabled"] = False
        codexhomes._write_pool_atomic(
            os.path.join(codexhomes.pool_dir(), "codex-keep-b.json"),
            json.dumps(rec, indent=2) + "\n")
        old = time.time() - 7200
        os.utime(src, (old, old))
        res = codexhomes._pool_auth(src, "keep-b", "fix", refuse_stale_src=True)
        self.assertIn("error", res)
        self.assertIn("FRESHER", res["error"])
        self.assertEqual(sorted(os.listdir(codexhomes.pool_dir())),
                         ["codex-keep-a.json", "codex-keep-b.json"])
        # CONTROL: the same re-pool with the rung off writes AND retires
        res = codexhomes._pool_auth(src, "keep-b", "fix", refuse_stale_src=False)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["retired"], ["codex-keep-a.json"])
        self.assertEqual(os.listdir(codexhomes.pool_dir()), ["codex-keep-b.json"])


def _seat_translate(src):
    from helm import seat
    return seat.translate_codex_auth(src)


class SyncOrcaNamesTheSelectedMemberTest(CodexHomesBase):
    """task/2981, the siblings of the creds join in the sync-orca path.

    Orca's selection names ONE member: an address plus the workspace id. On
    a Team plan every member shares that workspace id. `_sync_match` returned
    every home on the workspace (three homes, so sync refused to guess), or
    the ONE sibling home present (so sync pooled that sibling's credential as
    the selection). `_pooled_for` fell back to the first file on the
    workspace, so a sibling's file answered "already pooled". Every arm runs
    over REAL homes, and the shipped `helm codex pool` door mints the pool
    files."""

    WS = "ws-fake-0002"
    MEMBERS = (("admin", "admin@team.example", "user-fake-admin"),
               ("d", "d@team.example", "user-fake-d"),
               ("hen", "hen@team.example", "user-fake-hen"))

    def _member(self, name, email, uid, pool=True):
        self._plant(name, email=email, plan="team", plan_in="id",
                    account_id=self.WS, user_id=uid)
        if pool:
            rc, out, err = self._cmd("pool", name)
            self.assertEqual(rc, 0, out + err)

    def _sel(self, email, account_id=None):
        return {"id": "orca-fake-" + email, "email": email,
                "account_id": account_id or self.WS}

    def _matched(self, sel):
        rows = codexhomes.codex_list(codexhomes.read_pool())
        return [r["name"] for r in codexhomes._sync_match(sel, rows)]

    def test_sync_match_names_the_selected_member_not_the_workspace(self):
        for name, email, uid in self.MEMBERS:
            self._member(name, email, uid, pool=False)
        # the control: three members are three rows in the census the
        # match reads, so one answer below is the match's own doing
        self.assertEqual(len(codexhomes.codex_list(codexhomes.read_pool())), 3)
        for name, email, _uid in self.MEMBERS:
            with self.subTest(member=name):
                self.assertEqual(self._matched(self._sel(email)), [name])

    def test_sync_match_never_lends_a_siblings_home(self):
        self._member("admin", "admin@team.example", "user-fake-admin",
                     pool=False)
        self.assertEqual(self._matched(self._sel("hen@team.example")), [])
        # the control: the member that IS here is still found
        self.assertEqual(self._matched(self._sel("admin@team.example")),
                         ["admin"])

    def test_pooled_for_never_answers_with_a_siblings_file(self):
        for name, email, uid in self.MEMBERS[:2]:
            self._member(name, email, uid)
        self.assertEqual(sorted(os.listdir(codexhomes.pool_dir())),
                         ["codex-admin.json", "codex-d.json"])
        hen = self._sel("hen@team.example")
        self.assertIsNone(codexhomes._pooled_for(hen, codexhomes.read_pool()))
        # the control: pool hen's own file, and the same question finds it
        self._member(*self.MEMBERS[2])
        self.assertEqual(codexhomes._pooled_for(hen, codexhomes.read_pool()),
                         "codex-hen.json")

    def test_a_personal_account_still_matches_on_its_account_id(self):
        """A personal plan's account id names one person. So both lookups
        still match on the id alone, as before, even when orca spells the
        address differently from the home."""
        self._plant("solo", email="solo@personal.example", plan="pro",
                    plan_in="id", account_id="acct-fake-solo",
                    user_id="user-fake-solo")
        rc, out, err = self._cmd("pool", "solo")
        self.assertEqual(rc, 0, out + err)
        sel = self._sel("solo.renamed@personal.example", "acct-fake-solo")
        self.assertEqual(self._matched(sel), ["solo"])
        self.assertEqual(codexhomes._pooled_for(sel, codexhomes.read_pool()),
                         "codex-solo.json")


class OnlyAKnownPersonalPlanNamesOnePersonTest(CodexHomesBase):
    """task/2981: `_serves_member` and `codex_list` name a member only on a
    proof, and the id alone only on a plan KNOWN to be personal.

    The member rule recognised a WORKSPACE only by the exact plan "team". Any
    other plan (business, enterprise, edu, a plan named tomorrow) took the
    PERSONAL arm, so a sibling was admitted on the workspace id alone. An
    unreadable plan beside a missing address was admitted the same way. And
    `codex_list` folded a member with no user-id claim into a sibling's row.
    The personal arm now belongs only to a plan helm's own vocabulary
    (`PLAN_TIER`) knows to be personal."""

    WS = "ws-fake-0003"

    def _row(self, plan, email=None, user_id=None):
        return {"plan": plan, "email": email, "user_id": user_id}

    def test_the_personal_arm_is_only_for_a_known_personal_plan(self):
        for plan in ("business", "enterprise", "edu", "plan-fake-future"):
            for shared in (False, True):
                with self.subTest(plan=plan, shared=shared):
                    self.assertFalse(codexhomes._serves_member(
                        self._row(plan, "admin@ws.example"), None,
                        "hen@ws.example", shared))
                    self.assertFalse(codexhomes._serves_member(
                        self._row(plan), None, None, shared))
        # an UNREADABLE plan with no address is no proof either way
        self.assertFalse(codexhomes._serves_member(
            self._row(None, "admin@ws.example"), None, None, False))
        self.assertFalse(codexhomes._serves_member(
            self._row(None), None, None, False))
        # THE CONTROLS: a member proof still serves on every one of those
        # plans, and a known personal plan still serves on the id alone
        for plan in ("business", None, "team"):
            with self.subTest(proof=plan):
                self.assertTrue(codexhomes._serves_member(
                    self._row(plan, "hen@ws.example"), None,
                    "hen@ws.example", True))
                self.assertTrue(codexhomes._serves_member(
                    self._row(plan, None, "user-fake-hen"), "user-fake-hen",
                    None, True))
        self.assertTrue(codexhomes._serves_member(
            self._row("pro", "solo.old@personal.example"), None,
            "solo@personal.example", False))
        self.assertTrue(codexhomes._serves_member(
            self._row("pro"), None, None, False))

    def test_the_address_proof_ignores_case_and_never_matches_another(self):
        row = self._row("team", "Hen@WS.Example")
        self.assertTrue(codexhomes._serves_member(
            row, None, "hen@ws.example", True))
        self.assertFalse(codexhomes._serves_member(
            row, None, "hay@ws.example", True))

    def test_sync_never_lends_an_unrecognised_plans_sibling(self):
        self._plant("admin", email="admin@ws.example", plan="business",
                    plan_in="id", account_id=self.WS,
                    user_id="user-fake-admin")
        rc, out, err = self._cmd("pool", "admin")
        self.assertEqual(rc, 0, out + err)
        census = codexhomes.read_pool()
        rows = codexhomes.codex_list(census)
        hen = {"id": "orca-fake-hen", "email": "hen@ws.example",
               "account_id": self.WS}
        self.assertEqual(codexhomes._sync_match(hen, rows), [])
        self.assertIsNone(codexhomes._pooled_for(hen, census))
        # the control: the member that IS here is still found, both ways
        admin = dict(hen, email="Admin@WS.example")
        self.assertEqual([r["name"] for r in
                          codexhomes._sync_match(admin, rows)], ["admin"])
        self.assertEqual(codexhomes._pooled_for(admin, census),
                         "codex-admin.json")

    def test_codex_list_keeps_a_member_with_no_user_id_as_its_own_row(self):  # noqa: VACUOUS_ASSERTION — hen's absent pooled file and absent flag sit beside unconditional positives on the same observables: admin's row carries its pooled file and no flag, and the same printed list carries "member?" on hen's line
        self._plant("team-admin", email="admin@ws.example", plan="team",
                    plan_in="id", account_id=self.WS,
                    user_id="user-fake-admin")
        self._plant("team-hen", email="hen@ws.example", plan="team",
                    plan_in="id", account_id=self.WS)
        rc, out, err = self._cmd("pool", "team-admin")
        self.assertEqual(rc, 0, out + err)
        rows = {r["name"]: r for r in codexhomes.codex_list()}
        self.assertEqual(sorted(rows), ["team-admin", "team-hen"])
        self.assertEqual(rows["team-admin"]["aliases"], [])
        # hen's own file is not pooled, and admin's file is not hen's
        self.assertIsNone(rows["team-hen"]["pooled"])
        self.assertEqual(rows["team-admin"]["pooled"], "codex-team-admin.json")
        self.assertIs(rows["team-hen"]["member_unproven"], True)
        self.assertIs(rows["team-admin"]["member_unproven"], False)
        rc, out, err = self._cmd("list")
        self.assertEqual(rc, 0, err)
        line = next(x for x in out.splitlines() if "team-hen" in x)
        self.assertIn("member?", line)
        self.assertNotIn("member?",
                         next(x for x in out.splitlines() if "team-admin" in x))
        # THE CONTROLS. A second dir of hen's own login (same address) IS
        # hen, and folds. A personal account's two dirs fold on the id alone.
        self._plant("team-hen-again", email="HEN@ws.example", plan="team",
                    plan_in="id", account_id=self.WS)
        self._plant("solo-a", email="solo@personal.example", plan="pro",
                    account_id="acct-fake-solo")
        self._plant("solo-b", email="solo.old@personal.example", plan="pro",
                    account_id="acct-fake-solo")
        rows = {r["name"]: r for r in codexhomes.codex_list()}
        self.assertEqual(sorted(rows), ["solo-a", "team-admin", "team-hen"])
        self.assertEqual(rows["team-hen"]["aliases"], ["team-hen-again"])
        self.assertEqual(rows["solo-a"]["aliases"], ["solo-b"])
        self.assertIs(rows["solo-a"]["member_unproven"], False)


class AMemberIsProvenBeforeTheActTest(CodexHomesBase):
    """task/2981, the doors that ACT on a member match: the pool door retires
    a credential's other spellings, and cred-follow calls a member PRESENT or
    imports it. Both matched a missing user id on the workspace id alone, so
    the pool door retired a sibling's file and cred-follow read a member
    PRESENT on a sibling's file. A retirement and an import act on the
    match, so each now needs a member proof, and a file nothing can prove
    either way is UNKNOWN: never retired, never PRESENT, never imported
    beside."""

    WS = "ws-fake-0005"

    def setUp(self):
        super().setUp()
        self.orca_dir = os.path.join(self.tmp, "orca-ud")
        os.makedirs(self.orca_dir)
        env = mock.patch.dict(os.environ, {"ORCA_USER_DATA_PATH": self.orca_dir,
                                           "HELM_ORCA_CLI": "off"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HELM_ORCA_RPC", None)

    def _orca(self, aid, **kw):
        return _SyncOrcaBase._plant_orca_managed(self, aid, **kw)

    def _legacy(self, fname, plan, account_id=None):
        """A pool file written before the user-id claim was read: no user id,
        and the pool writer's placeholder where the address goes."""
        auth, _exp = _auth_json("legacy-" + fname, email="x@unused.example",
                                plan=plan, plan_in="id",
                                account_id=account_id or self.WS)
        rec = dict(auth["tokens"], type="codex", email="unknown",
                   disabled=False)
        os.makedirs(codexhomes.pool_dir(), exist_ok=True)
        with open(os.path.join(codexhomes.pool_dir(), fname), "w") as f:
            json.dump(rec, f)
        return fname

    def _states(self):
        return {r["orca_account"]: (r["state"], r["pooled"])
                for r in codexhomes.cred_follow(apply=False)["rows"]}

    def test_cred_follow_never_reads_a_member_PRESENT_on_a_siblings_file(self):
        self._plant("team-admin", email="admin@ws.example", plan="team",
                    plan_in="id", account_id=self.WS,
                    user_id="user-fake-admin")
        self.assertTrue(codexhomes.codex_pool("team-admin").get("ok"))
        # hen's orca credential carries NO user-id claim
        self._orca("a-hen", email="hen@ws.example", plan="team",
                   plan_in="id", account_id=self.WS)
        self._orca("a-admin", email="admin@ws.example", plan="team",
                   plan_in="id", account_id=self.WS)
        self.assertEqual(self._states(),
                         {"a-hen": ("MISSING", None),
                          "a-admin": ("PRESENT", "codex-team-admin.json")})

    def test_cred_follow_reads_UNKNOWN_beside_a_file_it_cannot_prove(self):
        legacy = self._legacy("codex-legacy.json", "team")
        self._orca("a-hen", email="hen@ws.example", plan="team",
                   plan_in="id", account_id=self.WS, user_id="user-fake-hen")
        self.assertEqual(self._states(), {"a-hen": ("UNKNOWN", None)})
        res = codexhomes.cred_follow(apply=True)
        self.assertEqual([r["state"] for r in res["rows"]], ["UNKNOWN"])
        self.assertIn(legacy, res["rows"][0]["detail"])
        self.assertEqual(sorted(os.listdir(codexhomes.pool_dir())), [legacy])
        # THE CONTROL: on a KNOWN personal plan the account id names one
        # person, so the same legacy shape IS this credential
        solo = self._legacy("codex-legacy-solo.json", "pro", "acct-fake-solo")
        self._orca("a-solo", email="solo@personal.example", plan="pro",
                   account_id="acct-fake-solo", user_id="user-fake-solo")
        self.assertEqual(self._states()["a-solo"], ("PRESENT", solo))

    def test_the_pool_door_never_retires_a_file_it_cannot_prove(self):
        legacy = self._legacy("codex-legacy.json", "team")
        self._plant("team-hen", email="hen@ws.example", plan="team",
                    plan_in="id", account_id=self.WS,
                    user_id="user-fake-hen")
        res = codexhomes.codex_pool("team-hen")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual((res["retired"], res["also_pooled_as"]), ([], None))
        self.assertEqual(res["unproven"], [legacy])
        self.assertIn(legacy, res["warn"])
        self.assertEqual(sorted(os.listdir(codexhomes.pool_dir())),
                         ["codex-legacy.json", "codex-team-hen.json"])
        # THE CONTROL: a personal account's legacy spelling IS retired
        solo = self._legacy("codex-legacy-solo.json", "pro", "acct-fake-solo")
        self._plant("solo", email="solo@personal.example", plan="pro",
                    account_id="acct-fake-solo", user_id="user-fake-solo")
        res = codexhomes.codex_pool("solo")
        self.assertEqual(res["retired"], [solo])
        self.assertIsNone(res["unproven"])
