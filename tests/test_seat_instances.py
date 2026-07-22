#!/usr/bin/env python3
"""slice 6 — N-codex-per-credhome. Hermetic: tmpdir HELM_HOME + pool dir,
hand-built unsigned JWTs for plan/exp claims, no real creds / proxy /
network. launch_line identity-shape + capacity policy + asset mint + the
launch guards, per the design doc's test plan."""
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

from helm import codexhomes, seat


def _read(path):
    with open(path) as f:
        return f.read()


def _b64seg(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims):
    return _b64seg({"alg": "RS256"}) + "." + _b64seg(claims) + ".sig"


class Slice6Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-slice6-")
        self._env = {k: os.environ.get(k) for k in
                     ("HELM_HOME", "HELM_CODEX_ULTRA_SEATS", "MELD_HOME")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ.pop("MELD_HOME", None)
        os.environ.pop("HELM_CODEX_ULTRA_SEATS", None)
        os.makedirs(seat.seat_dir("codex"), exist_ok=True)
        os.makedirs(codexhomes.pool_dir(), exist_ok=True)
        # _require_seat demands a minted seat: family-level config.yaml + token
        seat._write_private(os.path.join(seat.seat_dir("codex"), "config.yaml"),
                            "port: 8317\n", mode=0o600)
        seat._write_private(os.path.join(seat.seat_dir("codex"), "token"),
                            "test-token\n", mode=0o600)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pool_cred(self, name, plan="pro", disabled=False):
        rec = {"type": "codex", "email": "%s@x.test" % name,
               "account_id": "acct-%s" % name,
               "access_token": _jwt({"exp": int(time.time()) + 3600,
                                     "https://api.openai.com/auth":
                                     {"chatgpt_plan_type": plan}}),
               "id_token": _jwt({"email": "%s@x.test" % name}),
               "refresh_token": "r-%s" % name, "disabled": disabled}
        with open(os.path.join(codexhomes.pool_dir(),
                               "codex-%s.json" % name), "w") as f:
            json.dump(rec, f)


class LaunchLineInstanceTest(Slice6Base):
    def test_instance_line_carries_seat_identity_and_instance_dir(self):
        line = seat.launch_line("codex", seat="codex-2")
        self.assertIn("HELM_CHAT_NAME=codex-2", line)
        self.assertIn("HELM_CELL_PROFILE=codex-2", line)
        self.assertIn("DREGG_PROFILE=codex-2", line)
        self.assertIn("instances/codex-2/claude", line)
        # per-instance proxies: an instance gets its OWN port (family base + N),
        # NOT the shared family port — one instance's restart/429-stall never
        # takes a sibling down. The OAuth pool stays family-level.
        base = seat.launch_line("codex")
        fam_port = seat.FAMILIES["codex"]["port"]
        self.assertIn("127.0.0.1:%d" % (fam_port + 2), line)      # codex-2
        self.assertIn("127.0.0.1:%d" % fam_port, base)            # instance 1
        self.assertNotIn("127.0.0.1:%d " % fam_port, line)

    def test_default_line_byte_identical_to_pre_slice_shape(self):
        # instance 1 (and no -i) = today's exact behavior, back-compat
        self.assertEqual(seat.launch_line("codex"),
                         seat.launch_line("codex", seat="codex"))
        line = seat.launch_line("codex")
        self.assertIn("HELM_CHAT_NAME=codex ", line)
        self.assertNotIn("instances/", line)

    def test_instance_dir_resolution(self):
        self.assertEqual(seat._instance_dir("codex", "codex"),
                         seat.seat_dir("codex"))
        self.assertTrue(seat._instance_dir("codex", "codex-3")
                        .endswith(os.path.join("codex", "instances", "codex-3")))


class CapacityTest(Slice6Base):
    def test_pool_of_pro_and_team_folds_tiers(self):
        self._pool_cred("cto", plan="pro")          # ultra -> 3
        self._pool_cred("simbi", plan="team")       # team  -> 1
        self._pool_cred("ops", plan="team")         # team  -> 1
        cap = codexhomes.capacity()
        self.assertEqual(cap["total"], 5)
        by_tier = {c["tier"] for c in cap["creds"]}
        self.assertEqual(by_tier, {"ultra", "team"})

    def test_env_override_moves_ultra_count(self):
        self._pool_cred("cto", plan="pro")
        os.environ["HELM_CODEX_ULTRA_SEATS"] = "5"
        self.assertEqual(codexhomes.capacity()["total"], 5)

    def test_disabled_and_unparseable_contribute_zero(self):
        self._pool_cred("cto", plan="pro", disabled=True)
        with open(os.path.join(codexhomes.pool_dir(), "junk.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(codexhomes.capacity()["total"], 0)

    def test_unknown_tier_is_one(self):
        self._pool_cred("mystery", plan="enterprise-weird")
        cap = codexhomes.capacity()
        self.assertEqual(cap["total"], 1)
        self.assertEqual(cap["creds"][0]["seats"], 1)


class AssetMintTest(Slice6Base):
    def test_instance_assets_minted_no_second_token(self):
        d = seat._instance_dir("codex", "codex-2")
        seat._write_launch_assets("codex", d, seat="codex-2")
        self.assertTrue(os.path.isdir(os.path.join(d, "claude")))
        sh = os.path.join(d, "launch.sh")
        self.assertTrue(os.path.isfile(sh))
        self.assertEqual(stat.S_IMODE(os.stat(sh).st_mode), 0o700)
        with open(sh) as f:
            self.assertIn("HELM_CHAT_NAME=codex-2", f.read())
        # the shared family token is NOT re-minted into the instance dir
        self.assertFalse(os.path.exists(os.path.join(d, "token")))
        self.assertFalse(os.path.exists(os.path.join(d, "config.yaml")))

    def test_instance_claude_dir_is_a_recognized_config_home(self):
        # slice 6: an instance's claude dir must pass the configs write gate
        # (born-wired — hooks.install_home lands delivery hooks at mint).
        from helm import configs
        inst = os.path.join(seat.seat_dir("codex"), "instances", "codex-2",
                            "claude")
        self.assertTrue(configs._is_seat_home(inst))
        # and the family dir still is (back-compat)
        self.assertTrue(configs._is_seat_home(
            os.path.join(seat.seat_dir("codex"), "claude")))
        # a non-instance nested dir is NOT (no trust-surface widening)
        self.assertFalse(configs._is_seat_home(
            os.path.join(seat.seat_dir("codex"), "instances", "codex-2",
                         "other")))


class GuardTest(Slice6Base):
    def _launch(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_over_capacity_warns_never_refuses(self):
        # no pooled creds -> capacity 0; -i 9 must WARN on stderr, rc 0,
        # stdout still exactly the pasteable line
        rc, out, err = self._launch("launch", "codex", "-i", "9")
        self.assertEqual(rc, 0)
        self.assertIn("capacity", err)
        self.assertIn("HELM_CHAT_NAME=codex-9", out)

    def test_instance_one_prints_no_capacity_guard(self):
        rc, out, err = self._launch("launch", "codex")
        self.assertEqual(rc, 0)
        self.assertNotIn("capacity", err)
        self.assertIn("HELM_CHAT_NAME=codex ", out)

    def test_instance_flag_wants_an_integer(self):
        rc, out, err = self._launch("launch", "codex", "-i", "two")
        self.assertEqual(rc, 2)
        self.assertIn("integer", err)


class AtomicPoolWriteTest(Slice6Base):
    def test_pool_write_is_atomic_0600(self):
        dest = os.path.join(codexhomes.pool_dir(), "codex-cto.json")
        codexhomes._write_pool_atomic(dest, json.dumps({"type": "codex"}) + "\n")
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o600)
        # no tmp litter: the replace consumed the sibling
        leftovers = [n for n in os.listdir(codexhomes.pool_dir())
                     if n.startswith(".pool-")]
        self.assertEqual(leftovers, [])


class PerInstanceProxyTest(Slice6Base):
    """The proxy-FATE layer: each instance gets its own port/config/token/log
    (restart blast-radius + shared-fate 429 + log attribution), while the OAuth
    pool stays family-level (no quota multiplication)."""

    def test_instance_port_derivation(self):
        base = seat.FAMILIES["codex"]["port"]
        self.assertEqual(seat._instance_port("codex"), base)            # inst 1
        self.assertEqual(seat._instance_port("codex", "codex"), base)
        self.assertEqual(seat._instance_port("codex", "codex-2"), base + 2)
        self.assertEqual(seat._instance_port("codex", "codex-3"), base + 3)
        self.assertEqual(seat._instance_port("codex", "codex-10"), base + 10)

    def test_proxy_home_resolution(self):
        self.assertEqual(seat._proxy_home("codex"), seat.seat_dir("codex"))
        self.assertEqual(seat._proxy_home("codex", "codex"),
                         seat.seat_dir("codex"))
        self.assertTrue(seat._proxy_home("codex", "codex-2")
                        .endswith(os.path.join("codex", "instances", "codex-2")))

    def test_mint_instance_proxy_writes_own_config_and_token(self):
        seat._mint_instance_proxy("codex", "codex-2")
        home = seat._proxy_home("codex", "codex-2")
        cfg = _read(os.path.join(home, "config.yaml"))
        self.assertIn("port: %d" % seat._instance_port("codex", "codex-2"), cfg)
        # auth-dir points at the FAMILY pool — same OAuth account, no quota xN
        self.assertIn(os.path.join(seat.seat_dir("codex"), "auth"), cfg)
        tok = _read(os.path.join(home, "token")).strip()
        self.assertTrue(tok)
        self.assertNotEqual(tok, "test-token")     # instance token, not family
        # idempotent: re-mint keeps the same token (a live line stays valid)
        seat._mint_instance_proxy("codex", "codex-2")
        self.assertEqual(_read(os.path.join(home, "token")).strip(), tok)

    def test_instance_token_isolated_from_family(self):
        seat._mint_instance_proxy("codex", "codex-2")
        itok = seat._read_token("codex", "codex-2")
        ftok = seat._read_token("codex")           # family
        self.assertEqual(ftok, "test-token")
        self.assertNotEqual(itok, ftok)
        # an UNMINTED instance falls back to the family token (back-compat)
        self.assertEqual(seat._read_token("codex", "codex-9"), "test-token")

    def test_minted_instances_enumeration(self):
        self.assertEqual(seat._minted_instances("codex"), [])
        seat._mint_instance_proxy("codex", "codex-3")
        seat._mint_instance_proxy("codex", "codex-2")
        self.assertEqual(seat._minted_instances("codex"),
                         ["codex-2", "codex-3"])    # numeric, not lexical

    def test_up_refuses_an_unminted_instance(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["up", "codex-2"])
        self.assertEqual(rc, 1)
        self.assertIn("no per-instance proxy", err.getvalue())

    def test_split_seat(self):
        self.assertEqual(seat._split_seat("codex"), ("codex", "codex"))
        self.assertEqual(seat._split_seat("codex-3"), ("codex", "codex-3"))


class ProxyFixRoundTest(Slice6Base):
    """The codex xrev FIX round on 7bb422a: token never in argv/text, mint-order
    immunity, per-instance spawn fate, birth-identity pid guard, and the
    unsupported-family gate. Each test names the finding it closes."""

    def test_launch_line_never_carries_the_literal_token(self):
        # Finding 2 (no keys in argv/logs): the bearer is a file reference
        # resolved at exec, never interpolated into the line.
        seat._mint_instance_proxy("codex", "codex-2")
        itok = _read(os.path.join(seat._proxy_home("codex", "codex-2"),
                             "token")).strip()
        line = seat.launch_line("codex", seat="codex-2")
        self.assertNotIn(itok, line)                # instance token absent
        self.assertNotIn("test-token", line)        # family token absent
        self.assertIn("$(cat ", line)               # a read-at-exec reference
        self.assertIn("instances/codex-2/token", line)

    def test_launch_line_is_mint_order_immune(self):
        # Finding 1 (first-mint stale token): a line rendered BEFORE the mint
        # points at the instance path, so it resolves the token the mint writes.
        pre = seat.launch_line("codex", seat="codex-2")     # pre-mint
        seat._mint_instance_proxy("codex", "codex-2")
        itok = _read(os.path.join(seat._proxy_home("codex", "codex-2"),
                             "token")).strip()
        path = pre.split("ANTHROPIC_AUTH_TOKEN=$(cat ")[1].split(" ")[0]
        self.assertEqual(_read(path).strip(), itok)   # resolves instance
        self.assertNotEqual(_read(path).strip(), "test-token")

    def test_token_file_prefers_the_instance_path_for_instances(self):
        self.assertTrue(seat._token_file("codex", "codex-2")
                        .endswith(os.path.join("instances", "codex-2", "token")))
        self.assertEqual(seat._token_file("codex"),
                         os.path.join(seat.seat_dir("codex"), "token"))

    def test_down_refuses_to_signal_a_reused_pid(self):
        # Finding 4 (bare reusable PID): a live pid with a MISMATCHED birth
        # identity is not our proxy — never signalled, record reaped.
        home = seat._proxy_home("codex", "codex-2")
        os.makedirs(home, exist_ok=True)
        live = os.getpid()      # guaranteed alive, guaranteed NOT a proxy
        seat._write_private(os.path.join(home, "proxy.pid"),
                            "%d proc:forged-birth\n" % live, mode=0o600)
        self.assertIsNone(seat._running_pid("codex", "codex-2"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat._down("codex", seat="codex-2")
        self.assertEqual(rc, 0)                 # we survived => not signalled
        self.assertIn("reused", out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(home, "proxy.pid")))

    def test_running_pid_trusts_a_matching_birth_identity(self):
        home = seat._proxy_home("codex", "codex-2")
        os.makedirs(home, exist_ok=True)
        live = os.getpid()
        seat._write_private(os.path.join(home, "proxy.pid"),
                            "%d %s\n" % (live, seat._pid_identity(live)),
                            mode=0o600)
        self.assertEqual(seat._running_pid("codex", "codex-2"), live)

    def test_launch_refuses_instances_for_proxy_key_families(self):
        # Finding 5 (unsupported family): kimi is proxy-key — `launch kimi -i 2`
        # can never mint a working instance proxy, so refuse up front (rc 2).
        os.makedirs(seat.seat_dir("kimi"), exist_ok=True)
        seat._write_private(os.path.join(seat.seat_dir("kimi"), "config.yaml"),
                            "port: 8318\n", mode=0o600)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["launch", "kimi", "-i", "2"])
        self.assertEqual(rc, 2)
        self.assertIn("mode=proxy", err.getvalue())

    def test_proxy_family_bases_are_unique(self):
        self.assertTrue(seat._family_port_bases_are_unique())


if __name__ == "__main__":
    unittest.main()
