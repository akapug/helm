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
from unittest import mock

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
        self.timer = mock.patch.object(seat, "_ensure_autocompact_timer")
        self.timer.start()

    def tearDown(self):
        self.timer.stop()
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
        self._pool_cred("beta", plan="team")        # team  -> 1
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

    def test_instance_gate_refuses_zero_padded_alias(self):
        # fable adversarial LOW: codex-02 parses int()==2 but names a DISTINCT
        # proxy home aliasing codex-2's port. The gate refuses non-canonical
        # names up front, while the canonical N>=2 stays admissible.
        self.assertIsNone(seat._instance_gate("codex", "codex-2"))
        for padded in ("codex-02", "codex-002", "codex-01", "codex-00"):
            g = seat._instance_gate("codex", padded)
            self.assertIsNotNone(g, "%s must be refused" % padded)
            self.assertIn("canonical", g)


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
    """The codex xrev FIX round: token never in argv/text, mint-order
    immunity, per-instance spawn fate, birth-identity pid guard, and the
    unsupported-family gate. Each test names the finding it closes."""

    def test_launch_line_never_carries_the_literal_token(self):
        # no-keys-in-argv (the round-2 re-review): launch_line carries NO token
        # at all — neither the literal NOR a NAME=value for the external env
        # binary. The bearer is exported separately by `_token_export`.
        seat._mint_instance_proxy("codex", "codex-2")
        itok = _read(os.path.join(seat._proxy_home("codex", "codex-2"),
                             "token")).strip()
        line = seat.launch_line("codex", seat="codex-2")
        self.assertNotIn(itok, line)                # instance token absent
        self.assertNotIn("test-token", line)        # family token absent
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", line)   # no env NAME=value

    def test_token_export_is_a_builtin_no_argv(self):
        # the bearer reaches claude via `export` (a shell builtin — no external
        # process, no argv), read from the 0600 file at exec. The paste-line is
        # export-prefix + launch_line, and the env part holds no secret.
        seat._mint_instance_proxy("codex", "codex-2")
        itok = _read(os.path.join(seat._proxy_home("codex", "codex-2"),
                             "token")).strip()
        exp = seat._token_export("codex", "codex-2")
        self.assertTrue(exp.startswith("ANTHROPIC_AUTH_TOKEN=$(cat "))
        self.assertIn("export ANTHROPIC_AUTH_TOKEN", exp)
        self.assertIn("instances/codex-2/token", exp)
        self.assertNotIn(itok, exp)                 # a path, never the value
        full = exp + seat.launch_line("codex", seat="codex-2")
        self.assertNotIn(itok, full)                # nowhere in the whole line
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN=",
                         full.split("env ", 1)[1])  # env argv carries no secret

    def test_token_export_is_mint_order_immune(self):
        # first-mint (round-1 finding): the export rendered BEFORE the mint
        # points at the instance path, so it resolves the token the mint writes.
        pre = seat._token_export("codex", "codex-2")        # pre-mint
        seat._mint_instance_proxy("codex", "codex-2")
        itok = _read(os.path.join(seat._proxy_home("codex", "codex-2"),
                             "token")).strip()
        path = pre.split("$(cat ")[1].split(" ")[0]
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

    def test_unauthenticated_pid_record_fails_closed(self):
        # round-2 finding: a BARE pid (legacy, no identity) or a '?' (failed
        # capture) must NOT be trusted on the alive check alone — that is the
        # reused-pid SIGTERM hazard. `_running_pid` returns None (stale/refused)
        # and `_down` never signals the number. Probe with our OWN live pid.
        for body in ("%d\n", "%d ?\n"):                    # bare, then '?'
            home = seat._proxy_home("codex", "codex-2")
            os.makedirs(home, exist_ok=True)
            live = os.getpid()      # alive, NOT a proxy
            seat._write_private(os.path.join(home, "proxy.pid"),
                                body % live, mode=0o600)
            self.assertIsNone(seat._running_pid("codex", "codex-2"),
                              "unauthenticated record must fail closed: %r" % body)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = seat._down("codex", seat="codex-2")
            self.assertEqual(rc, 0)             # we survived => not signalled
            self.assertFalse(os.path.exists(os.path.join(home, "proxy.pid")))

    def test_config_emits_top_level_nonstream_keepalive(self):
        # owner-witnessed /compact empty-HTTP-200 (2026-07-22): a long
        # non-streaming summarize sits silent, the proxy reaps the idle socket.
        # The fix is a TOP-LEVEL key — NonStreamKeepAliveInterval lives on
        # SDKConfig (yaml:",inline" into root), NOT under streaming: (that
        # struct only holds keepalive-seconds/bootstrap-retries). The live
        # family configs carry it top-level by hand; the generator must too.
        cfg = seat._config_yaml(8319, "/auth", "tok")
        self.assertIn("\nnonstream-keepalive-interval: 15\n", cfg)
        kcfg = seat._config_yaml_key(8318, "tok", "moonshot",
                                     "https://api.moonshot.ai/v1", "kimi-k3",
                                     "sk-x")
        self.assertIn("\nnonstream-keepalive-interval: 15\n", kcfg)

    def test_config_emits_streaming_survival_block(self):
        # the owner's restart-loop class (2026-07-22): with the nonstream
        # keepalive LOADED, EVERY request at ~90% context still died empty-200 —
        # the STREAMING leg stalls before/during bytes at extreme payload sizes.
        # StreamingConfig has ONLY two knobs (no timeout field exists in the
        # schema): keepalive-seconds (SSE heartbeats keep a long stream alive)
        # and bootstrap-retries (retry a stream stalled before its first byte).
        for cfg in (seat._config_yaml(8319, "/auth", "tok"),
                    seat._config_yaml_key(8318, "tok", "moonshot",
                                          "https://api.moonshot.ai/v1",
                                          "kimi-k3", "sk-x")):
            self.assertIn("streaming:\n", cfg)
            self.assertIn("  keepalive-seconds: 15\n", cfg)
            self.assertIn("  bootstrap-retries: 2\n", cfg)

    def test_config_emits_transient_error_cooldown(self):
        # the codex 503-storm root cause (as-prevented): cli-proxy-api benches
        # a credential on ANY transient upstream error (408/500/502/503/504)
        # for transientErrorCooldown — the fork's default is 60s, and config
        # value 0 MEANS that default (conductor.go:89,149-152), it does NOT
        # disable. A codex seat's 2-cred pool: one blip benches a cred a full
        # minute, both bench in a window -> len(available)==0 -> 503 "no
        # available client" at auth-selection. Real quota is a clean 429 with
        # its own cooldown — this knob is transient-only. The generator must
        # emit 5 or every re-mint silently restores the 60s footgun.
        for cfg in (seat._config_yaml(8319, "/auth", "tok"),
                    seat._config_yaml_key(8318, "tok", "moonshot",
                                          "https://api.moonshot.ai/v1",
                                          "kimi-k3", "sk-x")):
            self.assertIn("\ntransient-error-cooldown-seconds: 5\n", cfg)

    def test_down_preserves_a_concurrent_replacements_pidfile(self):
        # atomic-ownership finding: _down kills the old proxy, then must unlink
        # ONLY the exact pid+birth record it owned — a replacement's fresh
        # pidfile (different pid) written before the unlink must SURVIVE.
        home = seat._proxy_home("codex", "codex-2")
        os.makedirs(home, exist_ok=True)
        live = os.getpid()      # our own pid: alive, NOT a proxy (never signalled)
        # the record _down will "kill": forged to match `_running_pid`'s check,
        # then we swap in a REPLACEMENT record before the unlink by patching
        # _proxy_pid_record to return the replacement on the ownership re-read.
        old_ident = seat._pid_identity(live)
        seat._write_private(os.path.join(home, "proxy.pid"),
                            "%d %s\n" % (live, old_ident), mode=0o600)
        real = seat._proxy_pid_record
        calls = {"n": 0}

        def swapping(family, seat=None):
            # 1st call = the ONE authentication read (real, so pid verifies);
            # 2nd = the unlink guard's re-check -> the REPLACEMENT record.
            # (Post-atomicity refactor there is no third ownership read — the
            # authenticated snapshot is threaded through signal+unlink.)
            calls["n"] += 1
            if calls["n"] >= 2:
                return {"pid": live + 99999, "identity": "proc:replacement"}
            return real(family, seat)

        # the record matches our own live pid, so _down "owns" it and WOULD
        # signal — stub os.kill to a no-op so the flow completes without
        # killing the test runner (the signal path is covered elsewhere; this
        # test pins the UNLINK-ownership behavior only).
        with mock.patch.object(seat, "_proxy_pid_record", swapping), \
                mock.patch.object(seat.os, "kill", lambda *a, **k: None):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = seat._down("codex", seat="codex-2")
        self.assertEqual(rc, 0)
        # the replacement record was NOT unlinked (the pidfile still exists)
        self.assertTrue(os.path.exists(os.path.join(home, "proxy.pid")))

    def test_down_survives_a_vanishing_unlink_guard_reread(self):
        # codex-2 advisory, SUPERSEDED shape: pre-refactor _down
        # re-read the pidfile for ownership AFTER verifying it, and a None/
        # wrong-pid re-read had to refuse (rc 1). The atomicity fix threads ONE
        # authenticated snapshot through signal+unlink, so the ONLY remaining
        # re-read is the unlink guard's re-check — a hostile None/wrong-pid
        # there now just declines the unlink (fail-closed), the down itself
        # completes rc 0, and the signal decisions were already made from the
        # OWNED record (never the hostile value). Pin: no crash, no refuse, no
        # signal from a hostile value, pidfile left in place.
        home = seat._proxy_home("codex", "codex-2")
        os.makedirs(home, exist_ok=True)
        live = os.getpid()      # alive, NOT a proxy
        old_ident = seat._pid_identity(live)
        pidfile = os.path.join(home, "proxy.pid")
        for bad in (None, {"pid": live + 99999, "identity": "proc:other"}):
            seat._write_private(pidfile, "%d %s\n" % (live, old_ident),
                                mode=0o600)
            real = seat._proxy_pid_record
            calls = {"n": 0}

            def vanishing(family, seat=None, _bad=bad):
                calls["n"] += 1
                # 1st call = the ONE authentication read (real, so pid
                # verifies); 2nd = the unlink guard's re-check -> BAD value.
                return real(family, seat) if calls["n"] == 1 else _bad

            real_kill = os.kill
            signals = []

            def recording_kill(pid, sig=0, *a, **k):
                signals.append(sig)
                if sig == 0:
                    return real_kill(pid, sig)   # real liveness probe
                # swallow SIGTERM/SIGKILL (would kill the test runner)

            with mock.patch.object(seat, "_proxy_pid_record", vanishing), \
                    mock.patch.object(seat.os, "kill", recording_kill):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = seat._down("codex", seat="codex-2")
            self.assertEqual(rc, 0, "hostile re-read must not crash: %r" % (bad,))
            # signals (if any) were decided from the OWNED record, before the
            # hostile read — and our own live pid was never really signalled.
            self.assertNotIn(None, signals)
            # the unlink guard declined: the pidfile survives.
            self.assertTrue(os.path.exists(pidfile))

    def test_up_serializes_concurrent_starts(self):
        # atomic-ownership finding: the check→spawn critical section is under
        # the per-home flock, so a second _up sees the first's record and
        # refuses. Prove the lock file is taken during _up (re-entrant attempt
        # to grab it non-blocking fails while _up holds it).
        import fcntl
        home = seat._proxy_home("codex", "codex-2")
        os.makedirs(home, exist_ok=True)
        seat._write_private(os.path.join(home, "config.yaml"), "port: 8319\n",
                            mode=0o600)
        lockpath = os.path.join(home, ".proxy.lock")
        held = {"fd": None}
        real_bin = seat._proxy_bin

        def probe_lock():
            # called inside _up's critical section (under the flock): a
            # non-blocking grab of the SAME lock must FAIL, proving it's held.
            fd = os.open(lockpath, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    raise AssertionError("proxy lock NOT held during _up")
                except BlockingIOError:
                    pass          # correct: the lock is held by _up
            finally:
                os.close(fd)
            return None            # no binary -> _up exits before spawning

        with mock.patch.object(seat, "_proxy_bin", probe_lock):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = seat._up("codex", seat="codex-2")
        self.assertEqual(rc, 1)     # no binary (our probe returned None)

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


class FableRoundTest(Slice6Base):
    """The fable-pair (land-gate) findings on 224e6b5: spawn/resume mint the
    instance proxy (the silent-dead-seat HIGH), the spawn-seam family/name
    gate, and the hostile-pidfile corpus. Each test names its finding."""

    def test_spawn_mints_the_instance_proxy(self):
        # HIGH: spawn of a never-launched instance produced a DEAD seat —
        # launch.sh pointed at the instance port with an EMPTY token, no proxy
        # auto-started, no warning (the seat_cfg-exists gate silently no-opped).
        # Now _spawn mints the instance proxy (idempotent) BEFORE the gate, so
        # the config exists and the auto-start/WARN branch can fire. Probe the
        # mint directly: after a spawn-path mint the instance config + token
        # exist (pre-fix they did not).
        seat._mint_instance_proxy("codex", "codex-2")   # what _spawn now calls
        home = seat._proxy_home("codex", "codex-2")
        self.assertTrue(os.path.exists(os.path.join(home, "config.yaml")))
        self.assertTrue(os.path.exists(os.path.join(home, "token")))
        # and the token resolves NON-empty (the dead-seat signature was empty)
        self.assertTrue(seat._read_token("codex", "codex-2"))

    def test_spawn_refuses_proxy_key_family_instances(self):
        # MED: the family gate lived only on `launch`; `spawn kimi-2` minted a
        # launch line pointed at a sibling family's port. Now refused rc 2.
        os.makedirs(seat.seat_dir("kimi"), exist_ok=True)
        seat._write_private(os.path.join(seat.seat_dir("kimi"), "config.yaml"),
                            "port: 8318\n", mode=0o600)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["spawn", "kimi-2"])
        self.assertEqual(rc, 2)
        self.assertIn("mode=proxy", err.getvalue())

    def test_spawn_refuses_instance_one_name(self):
        # MED: `codex-1` maps onto the family AND base+1 collides with the
        # adjacent family's base port (codex-1 -> 8318 = kimi's). Refused rc 2.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["spawn", "codex-1"])
        self.assertEqual(rc, 2)
        self.assertIn("not a distinct instance", err.getvalue())

    def test_resume_applies_the_same_instance_gate(self):
        # MED (fable-comp on 2a93541): the spawn-seam gate lived ONLY on
        # _spawn — `_resume` minted instance assets for the very seats spawn
        # refuses (kimi-2 proxy-key, codex-1 not-distinct). ONE shared
        # predicate now serves both verbs; resume refuses rc 2 BEFORE minting.
        for args, marker in ((["resume", "kimi-2"], "mode=proxy"),
                             (["resume", "codex-1"], "not a distinct instance")):
            # Mint the family/instance dirs so the gate — not the
            # missing-launch.sh refusal — is the branch under test.
            d = seat._instance_dir(args[1].rsplit("-", 1)[0], args[1])
            os.makedirs(d, exist_ok=True)
            seat._write_private(os.path.join(d, "launch.sh"),
                                "#!/bin/sh\n", mode=0o700)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = seat.cmd_seat(args)
            self.assertEqual(rc, 2, "%s must be refused" % args)
            self.assertIn(marker, err.getvalue())

    def test_down_threads_one_authenticated_record(self):
        # HIGH (codex-2 advisory carried forward): _down authenticates from ONE
        # owned snapshot read under the lock, and threads THAT record through
        # signal+unlink — a hostile later re-read can never split the verify
        # from the kill (the 224e6b5 None-subscript crash class). Probe: the
        # FIRST (authentication) read returns a valid owned record; any LATER
        # read (the unlink guard's re-check) returns None. _down must still
        # complete rc 0 without crashing — the only effect of the hostile
        # re-read is that the unlink guard declines to unlink (fail-closed).
        home = seat._proxy_home("codex", "codex-2")
        os.makedirs(home, exist_ok=True)
        live = os.getpid()
        ident = seat._pid_identity(live)
        pidfile = os.path.join(home, "proxy.pid")
        seat._write_private(pidfile, "%d %s\n" % (live, ident), mode=0o600)
        reads = []

        real_record = seat._proxy_pid_record

        def counting_record(family, s=None):
            reads.append(1)
            if len(reads) == 1:
                return real_record(family, s)
            return None          # hostile: vanish on any re-read

        real_kill = os.kill

        def no_real_signal(pid, sig=0):
            if sig not in (0,):
                return           # swallow SIGTERM/SIGKILL aimed at ourselves
            return real_kill(pid, sig)

        with mock.patch.object(seat, "_proxy_pid_record", counting_record), \
                mock.patch.object(seat.os, "kill", no_real_signal):
            # capture stdout: _down's print is buffered on sys.stdout, and an
            # un-flushed buffer leaks into a LATER test's fd-1 capture (the
            # test_seats deliver-hook JSON probe) — cross-test pollution.
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = seat._down("codex", "codex-2")
        self.assertEqual(rc, 0, "hostile re-read must not crash the down")
        self.assertEqual(len(reads), 2,
                         "expected ONE authentication read + ONE unlink-guard "
                         "re-check, got %d" % len(reads))
        self.assertTrue(os.path.exists(pidfile),
                        "unlink guard fails closed: record left in place when "
                        "the re-read vanished")

    def test_hostile_pidfile_corpus_fails_closed(self):
        # LOW: every hostile pidfile shape must fail CLOSED (no signal, no
        # crash) — including an overflow-sized pid (was an uncaught
        # OverflowError). Probe each through _running_pid with our own live pid.
        home = seat._proxy_home("codex", "codex-2")
        os.makedirs(home, exist_ok=True)
        live = os.getpid()
        ident = seat._pid_identity(live)
        for body in ("garbage\n", "", "-1 proc:x\n", "0 proc:x\n",
                     "99999999999999999999 proc:x\n",      # overflow
                     "%d proc:wrong-identity\n" % live,    # live but not ours
                     "%d\n" % live,                        # legacy bare pid
                     ):
            seat._write_private(os.path.join(home, "proxy.pid"), body, mode=0o600)
            self.assertIsNone(seat._running_pid("codex", "codex-2"),
                              "hostile pidfile must fail closed: %r" % body)
            self.assertIsNone(seat._running_pid_rec("codex", "codex-2"),
                              "hostile pidfile must fail closed (rec): %r" % body)

    def test_sub2_pid_reaps_without_kill_advice(self):
        # fable adversarial MED: a corrupt proxy.pid carrying 0/1/-1 must never
        # surface `kill -1`/`kill 0` in _down's remediation — agents paste that
        # advice verbatim and kill -1 SIGTERMs the whole signal set. The record
        # parse itself rejects pid < 2, so _down takes the stale-reap path.
        home = seat._proxy_home("codex", "codex-2")
        os.makedirs(home, exist_ok=True)
        pidpath = os.path.join(home, "proxy.pid")
        for bad in ("-1 proc:x\n", "0 proc:x\n", "1 proc:x\n", "-1\n", "0\n"):
            seat._write_private(pidpath, bad, mode=0o600)
            self.assertIsNone(seat._proxy_pid_record("codex", "codex-2"),
                              "pid<2 must parse as no-record: %r" % bad)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                seat._down("codex", seat="codex-2")
            blob = out.getvalue() + err.getvalue()
            self.assertNotIn("kill -1", blob, "leaked kill -1 for %r" % bad)
            self.assertNotIn("kill 0", blob, "leaked kill 0 for %r" % bad)


if __name__ == "__main__":
    unittest.main()
