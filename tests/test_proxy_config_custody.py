#!/usr/bin/env python3
"""Task/1967: existing proxy config and listener incarnation custody."""
import contextlib
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from tests._tmphome import pin_suite_guard
from helm import autocompact
from helm import seat
from helm import seat_health
from helm import seat_paths
from helm import seat_proxy

# THE POOL FAMILY'S OWN ROUTE, ASKED OF THE CATALOG. The two arms below plant
# "the config helm would generate today" for this family and then assert what
# regeneration does or does not disturb, so the planted provider block has to
# be one `_eligible_providers` can still match to a catalogued route: it pairs
# a block's provider NAME with its claude-side ALIAS, and a block matching no
# route leaves the eligible set empty, which is an unreadable desired state
# and RAISES before either assertion is reached.
#
# THE EARLIER SPELLING DERIVED ONLY THE UPSTREAM MODEL, off whichever family
# declared an `openrouter` pool row, and hard-named the provider and the alias
# beside it. True while THIS family declared that row; the split that gave the
# flash route its own family left the fixture planting a provider this family
# no longer catalogues, under this family's alias, with the OTHER family's
# upstream. Asking the catalog for this family's own first route is the
# question that stays true across the next split, and spells neither the
# provider nor the upstream here.
POOL_FAMILY = "ds4pro"  # noqa: SEAT_NAME — the FAMILY key, never a seat
POOL_ROUTE = seat.proxy_routes(POOL_FAMILY)[0]
POOL_PORT = seat.FAMILIES[POOL_FAMILY]["port"]


class FakeProcess:
    pid = 5000
    returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -signal.SIGKILL


class ProxyConfigCustodyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-proxy-custody-")
        self.prior = {name: os.environ.get(name) for name in
                      ("HELM_HOME", "HELM_PROXY_BIN", "HELM_SUITE_GUARD")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        self.suite_guard = pin_suite_guard(self, self.tmp)
        self.binary = os.path.join(self.tmp, "cli-proxy-api")
        with open(self.binary, "w") as f:
            f.write("#!/bin/sh\n"
                    "printf 'CLIProxyAPI Version: test\\n'\n"
                    "if [ \"$1\" = --version ]; then exit 2; fi\n"
                    "if [ \"$1\" = -h ]; then\n"
                    "  printf 'Usage of cli-proxy-api\\n  -config string\\n' >&2\n"
                    "fi\n"
                    "exit 0\n")
        os.chmod(self.binary, 0o700)
        os.environ["HELM_PROXY_BIN"] = self.binary

    def tearDown(self):
        for name, value in self.prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, family, text, seat_name=None):
        home = seat._proxy_home(family, seat_name)
        os.makedirs(home, exist_ok=True)
        path = os.path.join(home, "config.yaml")
        seat._write_private(path, text)
        return path

    @staticmethod
    def stale_oauth(port=8324):
        text = seat._config_yaml(port, "/exact/auth", "exact-inbound-token",
                                 channel="codex", model="gpt-6-astra")
        text = text.replace('auth-dir: "/exact/auth"\n',
                            'auth-dir: "/exact/auth" # retained value\n')
        text = text.replace('  - "exact-inbound-token"\n',
                            '  - "exact-inbound-token" # primary\n'
                            '  - "second-inbound-token"\n')
        text = text.replace("  disable-control-panel: true\n",
                            "  disable-control-panel: true\n"
                            "  unrelated-control: keep\n")
        text = text.replace("streaming:\n", "custom-before:\n  keep: alpha\nstreaming:\n")
        text = text.replace("  bootstrap-retries: 2\n",
                            "  bootstrap-retries: 2\n  unrelated-stream: keep\n")
        text = text.replace("oauth-model-alias:\n",
                            "custom-middle:\n  keep: beta\noauth-model-alias:\n")
        text = text.replace(
            "  codex:\n", "  codex:\n"
            '    - name: "custom-upstream"\n'
            '      alias: "unrelated-oauth-alias"\n', 1)
        text = text.replace("      fork: true\n",
                            "      fork: true\n      force-mapping: true\n", 1)
        return text + "custom-after:\n  keep: gamma\n"

    def test_codex_7_regeneration_preserves_instance_secrets_and_unknown_blocks(self):
        path = self.write_config("codex", self.stale_oauth(), "codex-7")
        changed, why = seat.regenerate_proxy_config(path, "codex", "codex-7")
        self.assertTrue(changed)
        self.assertIsNone(why)
        with open(path) as f:
            got = f.read()
        self.assertIn("port: 8324\n", got)
        self.assertNotIn("port: 8317\n", got)
        self.assertIn('auth-dir: "/exact/auth"', got)
        self.assertIn('  - "exact-inbound-token"', got)
        for marker in ("second-inbound-token", "unrelated-control",
                       "unrelated-stream", "custom-before", "custom-middle",
                       "custom-after", "unrelated-oauth-alias"):
            self.assertIn(marker, got)
        self.assertNotIn("force-mapping", got)
        self.assertIsNone(seat.proxy_alias_drift(path, "codex", "codex-7"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_key_backed_regeneration_preserves_provider_route_keys_and_nested_content(self):
        key_one = 'outbound "one"\\path\nnext'
        key_two = "outbound-two\\tail"
        text = seat._config_yaml_key(
            POOL_PORT, "inbound", POOL_ROUTE["provider"],
            "https://pool-provided.example/v1",
            POOL_ROUTE["alias"], key_one, POOL_ROUTE["upstream_model"],
            api_keys=(key_one, key_two))
        text = text.replace("    models:\n",
                            "    unrelated-provider-field:\n      keep: yes\n    models:\n")
        text = text.replace(
            "nonstream-keepalive-interval:",
            '      - name: "other-upstream"\n'
            '        alias: "unrelated-model"\n'
            '        display-name: "keep-me"\n'
            "custom-top-level:\n  keep: yes\n"
            "nonstream-keepalive-interval:")
        path = self.write_config(POOL_FAMILY, text)
        changed, why = seat.regenerate_proxy_config(path, POOL_FAMILY)
        self.assertFalse(changed, "current generated aliases plus unrelated content stay byte-stable")
        self.assertIsNone(why)
        with open(path) as f:
            got = f.read()
        for value in (POOL_ROUTE["provider"], "https://pool-provided.example/v1",
                      POOL_ROUTE["upstream_model"],
                      json.dumps(key_one, ensure_ascii=False),
                      json.dumps(key_two, ensure_ascii=False), "unrelated-provider-field",
                      "unrelated-model", "keep-me",
                      "custom-top-level"):
            self.assertIn(value, got)
        self.assertNotIn('name: "None"', got)

    def test_unrelated_key_provider_does_not_block_selected_provider_migration(self):
        text = seat._config_yaml_key(
            POOL_PORT, "inbound", POOL_ROUTE["provider"],
            "https://pool.example/v1",
            POOL_ROUTE["alias"], "selected-key", POOL_ROUTE["upstream_model"])
        unrelated = (
            '  - name: "unrelated"\n'
            '    base-url: "https://unrelated.example/v1"\n'
            '    api-key-entries:\n'
            '      - api-key: "unrelated-key"\n'
            '    models:\n'
            '      - name: "other-upstream"\n'
            '        alias: "other-model"\n')
        selected = '  - name: "%s"\n' % POOL_ROUTE["provider"]
        text = text.replace(
            selected, unrelated + selected.rstrip("\n") + " # selected\n", 1)
        text = text.replace('        alias: "claude-opus-5"\n', "", 1)
        path = self.write_config(POOL_FAMILY, text)
        changed, why = seat.regenerate_proxy_config(path, POOL_FAMILY)
        self.assertTrue(changed)
        self.assertIsNone(why)
        with open(path) as f:
            got = f.read()
        self.assertIn('name: "unrelated"', got)
        self.assertIn('api-key: "unrelated-key"', got)
        self.assertIn('alias: "other-model"', got)
        self.assertIn('alias: "claude-opus-5"', got)

    def test_selected_provider_key_order_proxy_url_and_model_metadata_survive(self):
        text = seat._config_yaml_key(
            8318, "inbound", "moonshot", "https://api.moonshot.ai/v1",
            "kimi-k3", "outbound", "kimi-k3")
        text = text.replace(
            '  - name: "moonshot"\n    base-url:',
            '  - priority: 1\n    base-url:')
        text = text.replace(
            '    api-key-entries:\n      - api-key: "outbound"',
            '    name: "moonshot"\n    api-key-entries:\n'
            '      - api-key: "outbound"\n'
            '        proxy-url: "http://127.0.0.1:9999"')
        text = text.replace(
            '        alias: "kimi-k3"\n',
            '        alias: "kimi-k3"\n'
            '        display-name: "Kimi with vision"\n'
            '        input-modalities: ["text", "image"]\n', 1)
        text = text.replace('        alias: "claude-opus-5"\n', "", 1)
        path = self.write_config("kimi", text)
        changed, why = seat.regenerate_proxy_config(path, "kimi")
        self.assertTrue(changed)
        self.assertIsNone(why)
        with open(path) as f:
            got = f.read()
        self.assertEqual(got.count('name: "moonshot"'), 1)
        for marker in ("priority: 1", "proxy-url:", "display-name:",
                       "input-modalities:", 'alias: "claude-opus-5"'):
            self.assertIn(marker, got)

    def test_flow_style_inbound_keys_are_preserved_during_oauth_migration(self):
        text = seat._config_yaml(
            8317, "/auth", "primary", channel="codex", model="gpt-6-astra")
        text = text.replace('api-keys:\n  - "primary"\n',
                            'api-keys: ["primary", "secondary"]\n')
        text = text.replace("      force-mapping: true\n", "", 1)
        text = text.replace('      fork: true\n',
                            '      fork: true\n      force-mapping: true\n', 1)
        path = self.write_config("codex", text)
        changed, why = seat.regenerate_proxy_config(path, "codex")
        self.assertTrue(changed)
        self.assertIsNone(why)
        with open(path) as f:
            got = f.read()
        self.assertIn('api-keys: ["primary", "secondary"]', got)
        self.assertNotIn("force-mapping", got)

    def test_kimi_regeneration_keeps_key_selected_endpoint_and_secret(self):
        secret = 'sk-kimi-secret "quoted"\\tail'
        text = seat._config_yaml_key(
            8318, "inbound", "moonshot", "https://api.kimi.com/coding/v1",
            "kimi-k3", secret, "kimi-k3").replace(
                '        alias: "claude-fable-5-1"\n', "", 1)
        path = self.write_config("kimi", text)
        changed, why = seat.regenerate_proxy_config(path, "kimi")
        self.assertTrue(changed)
        self.assertIsNone(why)
        with open(path) as f:
            got = f.read()
        self.assertIn('base-url: "https://api.kimi.com/coding/v1"', got)
        self.assertIn(json.dumps(secret, ensure_ascii=False), got)
        self.assertIn('alias: "claude-fable-5-1"', got)
        self.assertIsNone(seat.proxy_alias_drift(path, "kimi"))

    def test_missing_oauth_alias_block_is_drift_and_doctor_is_non_green(self):
        path = self.write_config(
            "codex", seat._config_yaml(8317, "/auth", "token").split(
                "# built-in subagent", 1)[0])
        lines = seat._alias_drift_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("codex OAuth alias", lines[0])
        with mock.patch.object(seat, "_proxy_bin", return_value=self.binary), \
                mock.patch.object(seat.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(
                    seat.subprocess, "run", return_value=mock.Mock(
                        returncode=0,
                        stdout="CLIProxyAPI Version: test\n  -config string\n")), \
                mock.patch.object(seat, "codex_cred_state",
                                  return_value=(seat.CRED_VALID, "/cred", None)), \
                mock.patch.object(seat, "_cred_exp", return_value=9999999999), \
                mock.patch.object(seat, "_status"), \
                mock.patch.object(seat, "_running_pid", return_value=None):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(seat._doctor([]), 1)
        self.assertTrue(os.path.exists(path))

    def test_launch_and_spawn_refuse_persisted_or_explicit_alias_runtime(self):
        self.write_config(
            "kimi", seat._config_yaml_key(
                8318, "inbound", "moonshot", "https://api.moonshot.ai/v1",
                "kimi-k3", "outbound", "kimi-k3"))
        with mock.patch.object(seat, "_persisted_model",
                               return_value="claude-opus-5"):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(seat.cmd_seat(["launch", "kimi"]), 2)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(seat.cmd_seat(
                ["spawn", "kimi", "--model", "claude-opus-5", "--print"]), 2)

    def test_doctor_marks_non_alias_generated_policy_drift(self):
        """`_config_drift_lines` renders ONE line per seat and its detail
        PREFERS `plan["alias_drift"]` over the generic policy sentence, so this
        arm can only discriminate a policy-only drift while its base config is
        what the shipped generator would write TODAY. `family=` is what makes
        it so: without it the generator emits the launch model on all four
        frontmatter ids, the codex family's `subagent_tiers` table makes that
        alias drift, and the alias sentence wins the one line -- which is the
        round-one red, a fixture that predated the tier table rather than a
        reporter defect."""
        text = seat._config_yaml(
            8317, "/auth", "token", channel="codex", model="gpt-6-astra",
            family="codex")
        # THE CONTROL, and it is the arm's whole premise: the UNMUTATED base is
        # already the desired state, so every line below is attributable to the
        # one byte this arm changes. Blast radius: this control reads only this
        # test's own tmp HELM_HOME through the real planner, so it can redden
        # for exactly one reason -- the generator's output for family codex is
        # not what the planner regenerates -- which is what a fixture built
        # without `family=` (or a later tier-table change) does.
        canonical = self.write_config("codex", text)
        self.assertEqual(seat._config_drift_lines(), [])
        self.assertEqual(seat._alias_drift_lines(), [])
        path = self.write_config(
            "codex", text.replace("  bootstrap-retries: 2\n",
                                  "  bootstrap-retries: 1\n"))
        self.assertEqual(path, canonical)      # the same file, one byte apart
        lines = seat._config_drift_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("generated proxy policy differs", lines[0])
        # and the alias policy is NOT what drifted -- the discrimination this
        # arm exists for, asserted on the reader that renders alias drift alone
        self.assertEqual(seat._alias_drift_lines(), [])
        self.assertTrue(os.path.exists(path))

    def test_a_live_config_with_the_meter_off_is_stale_and_the_desired_state_turns_it_on(self):
        """The reconcile (`_ensure_row` -> `proxy_config_plan`) must treat
        every config minted before the meter as STALE, or the flag flips
        only for seats minted after it. Positive: the shipped generator's
        output with the meter lines put back to the pre-meter spelling
        plans as changed, the rendered desired state carries the meter, and
        the doctor line names the seat. Control: the unmutated output is
        already the desired state (changed False, no drift line), so the
        one edit is the whole cause."""
        text = seat._config_yaml(
            8317, "/auth", "token", channel="codex", model="gpt-6-astra",
            family="codex")
        path = self.write_config("codex", text)
        plan = seat.proxy_config_plan(path, "codex", "codex")
        self.assertFalse(plan["changed"])
        self.assertEqual(seat._config_drift_lines(), [])
        live = text.replace("usage-statistics-enabled: true\n"
                            "redis-usage-queue-retention-seconds: 3600\n",
                            "usage-statistics-enabled: false\n")
        self.assertNotEqual(live, text)
        self.assertEqual(self.write_config("codex", live), path)
        plan = seat.proxy_config_plan(path, "codex", "codex")
        self.assertTrue(plan["changed"])
        # the merge replaces an owned key in place and appends one the live
        # file never had, so the two lines are asserted separately
        self.assertIn("usage-statistics-enabled: true\n", plan["text"])
        self.assertIn("redis-usage-queue-retention-seconds: 3600\n", plan["text"])
        self.assertNotIn("usage-statistics-enabled: false", plan["text"])
        lines = seat._config_drift_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("generated proxy policy differs", lines[0])
        # and the rendered state is SETTLED: written back, it plans as
        # current and the doctor line is gone
        seat._write_private(path, plan["text"])
        self.assertFalse(seat.proxy_config_plan(path, "codex", "codex")["changed"])
        self.assertEqual(seat._config_drift_lines(), [])

    def test_the_spawn_hands_the_sidecar_its_management_secret_by_environment(self):
        """The fork registers management routes (and with them the usage
        queue the meter pops) only when a secret exists at start. Positive:
        the spawn mints `mgmt.token` (0600) beside the config and passes its
        value as MANAGEMENT_PASSWORD in the child's ENVIRONMENT. Controls:
        the value is absent from argv (cmdline is world-readable) and from
        the parent environment before the spawn, and the config's own
        secret-key stays empty, so nothing is bcrypted back into the file the
        reconcile compares."""
        cfgd = os.path.join(self.tmp, "sidecar")
        os.makedirs(cfgd)
        config = os.path.join(cfgd, "config.yaml")
        seat._write_private(config, seat._config_yaml(
            8317, "/auth", "token", channel="codex", model="gpt-6-astra",
            family="codex"))
        with open(config) as f:
            self.assertIn('  secret-key: ""\n', f.read())
        self.assertFalse(os.path.exists(os.path.join(cfgd, "mgmt.token")))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MANAGEMENT_PASSWORD", None)
            with mock.patch.object(seat_proxy.subprocess, "Popen",
                                   return_value=FakeProcess()) as popen, \
                    mock.patch.object(seat_proxy, "_pid_identity",
                                      return_value="proc:born"), \
                    mock.patch.object(seat_proxy, "_port_open", return_value=True):
                process, err = seat_proxy._launch_proxy_process(
                    self.binary, config, cfgd, 8317)
        self.assertIsNone(err)
        self.assertIsNotNone(process)
        secret_path = os.path.join(cfgd, "mgmt.token")
        with open(secret_path) as f:
            minted = f.read().strip()
        self.assertEqual(len(minted), 64)
        self.assertEqual(stat.S_IMODE(os.stat(secret_path).st_mode), 0o600)
        argv = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["MANAGEMENT_PASSWORD"], minted)
        self.assertNotIn(minted, " ".join(argv))
        # a bool, not a membership assertion on the mapping: a failure here must
        # name the key and never render the parent environment (hygiene rung)
        self.assertFalse("MANAGEMENT_PASSWORD" in os.environ,
                         "MANAGEMENT_PASSWORD leaked into the parent environment")
        # a second spawn keeps the same secret: the reader's copy stays valid
        self.assertEqual(seat_paths._mgmt_secret(cfgd), minted)

    def test_atomic_private_write_failure_retains_original_bytes_and_mode(self):
        path = os.path.join(self.tmp, "secret")
        seat._write_private(path, "old-secret\n", mode=0o640)
        with open(path, "rb") as f:
            before = f.read()
        with mock.patch.object(seat_paths.os, "replace",
                               side_effect=OSError("injected replace failure")):
            with self.assertRaises(OSError):
                seat._write_private(path, "new-secret\n", mode=0o600)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        self.assertFalse(any(name.startswith(".private-")
                             for name in os.listdir(self.tmp)))

    def stale_running_kimi(self):
        path = self.write_config(
            "kimi", seat._config_yaml_key(
                8318, "inbound", "moonshot", "https://api.moonshot.ai/v1",
                "kimi-k3", "outbound", "kimi-k3").replace(
                    "        alias: \"claude-opus-5\"\n",
                    "        alias: \"claude-opus-5\"\n"
                    "        force-mapping: true\n", 1))
        launch = seat._proxy_launch_inputs(path, self.binary)
        owned = {"pid": 4242, "identity": "proc:old", "launch": launch}
        seat._write_private(os.path.join(seat._proxy_home("kimi"), "proxy.pid"),
                            "4242 proc:old %s\n" % seat._encode_launch_inputs(launch))
        return path, owned

    def test_proxy_binary_ready_uses_supported_help_contract(self):
        version = subprocess.run(
            [self.binary, "--version"], capture_output=True, check=False)
        self.assertEqual(version.returncode, 2)
        self.assertEqual(seat_proxy._proxy_binary_ready(self.binary), (True, None))

    def test_proxy_binary_ready_rejects_missing_config_flag(self):
        with open(self.binary, "w") as f:
            f.write("#!/bin/sh\nprintf 'usage only\\n' >&2\nexit 0\n")
        ready, why = seat_proxy._proxy_binary_ready(self.binary)
        self.assertFalse(ready)
        self.assertIn("required -config flag", why)

    def test_failed_probe_preserves_config_and_listener(self):  # noqa: VACUOUS_ASSERTION — rc/config prove refusal
        path, owned = self.stale_running_kimi()
        with open(path, "rb") as f:
            before = f.read()
        self.assertGreater(len(before), 0)
        with open(self.binary, "w") as f:
            f.write("#!/bin/sh\nexit 2\n")
        with mock.patch.object(seat, "_running_pid_rec", return_value=owned), \
                mock.patch.object(seat.signal, "pidfd_send_signal") as send, \
                mock.patch.object(seat_proxy, "_launch_proxy_process") as launch:
            with contextlib.redirect_stderr(io.StringIO()):
                rc = seat._up("kimi", quiet=True)
        self.assertEqual(rc, 1)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        send.assert_not_called()
        launch.assert_not_called()

    def test_proxy_binary_probe_timeout_is_unready(self):
        with mock.patch.object(
                seat_proxy.subprocess, "run",
                side_effect=subprocess.TimeoutExpired([self.binary, "-h"], 5)):
            ready, why = seat_proxy._proxy_binary_ready(self.binary)
        self.assertFalse(ready)
        self.assertIn("timed out", why)

    def test_doctor_accepts_the_same_supported_help_contract(self):
        out = io.StringIO()
        with mock.patch.object(seat_health, "_proxy_bin",
                               return_value=self.binary), \
                mock.patch.object(seat_health.shutil, "which",
                                  return_value="/usr/bin/claude"), \
                mock.patch.object(seat_health, "codex_cred_state",
                                  return_value=("valid", None, "unreadable")), \
                mock.patch.object(seat_health, "_status"), \
                mock.patch.object(seat_health, "_minted_seats",
                                  return_value=()), \
                mock.patch.object(seat_health, "_config_drift_lines",
                                  return_value=[]), \
                mock.patch.object(autocompact, "report_lines",
                                  return_value=[]), \
                contextlib.redirect_stdout(out):
            rc = seat_health._doctor([])
        self.assertEqual(rc, 0)
        self.assertIn("proxy binary: %s (CLIProxyAPI Version: test)" %
                      self.binary, out.getvalue())

    def test_doctor_fails_when_help_probe_fails(self):
        with open(self.binary, "w") as f:
            f.write("#!/bin/sh\nexit 2\n")
        out = io.StringIO()
        with mock.patch.object(seat_health, "_proxy_bin",
                               return_value=self.binary), \
                mock.patch.object(seat_health.shutil, "which",
                                  return_value="/usr/bin/claude"), \
                mock.patch.object(seat_health, "codex_cred_state",
                                  return_value=("valid", None, "unreadable")), \
                mock.patch.object(seat_health, "_status"), \
                mock.patch.object(seat_health, "_minted_seats",
                                  return_value=()), \
                mock.patch.object(seat_health, "_config_drift_lines",
                                  return_value=[]), \
                mock.patch.object(autocompact, "report_lines",
                                  return_value=[]), \
                contextlib.redirect_stdout(out):
            rc = seat_health._doctor([])
        self.assertEqual(rc, 1)
        self.assertIn("present, unusable: -h exited 2", out.getvalue())

    def test_doctor_rejects_help_without_config_flag(self):
        with open(self.binary, "w") as f:
            f.write("#!/bin/sh\nprintf 'usage only\\n' >&2\nexit 0\n")
        out = io.StringIO()
        with mock.patch.object(seat_health, "_proxy_bin",
                               return_value=self.binary), \
                mock.patch.object(seat_health.shutil, "which",
                                  return_value="/usr/bin/claude"), \
                mock.patch.object(seat_health, "codex_cred_state",
                                  return_value=("valid", None, "unreadable")), \
                mock.patch.object(seat_health, "_status"), \
                mock.patch.object(seat_health, "_minted_seats",
                                  return_value=()), \
                mock.patch.object(seat_health, "_config_drift_lines",
                                  return_value=[]), \
                mock.patch.object(autocompact, "report_lines",
                                  return_value=[]), \
                contextlib.redirect_stdout(out):
            rc = seat_health._doctor([])
        self.assertEqual(rc, 1)
        self.assertIn("required -config flag", out.getvalue())

    def test_missing_replacement_binary_leaves_stale_config_and_listener_intact(self):
        path, owned = self.stale_running_kimi()
        with open(path, "rb") as f:
            before = f.read()
        os.environ["HELM_PROXY_BIN"] = os.path.join(self.tmp, "missing-proxy")
        with mock.patch.object(seat, "_running_pid_rec", return_value=owned), \
                mock.patch.object(seat.os, "kill") as kill, \
                mock.patch.object(seat.subprocess, "Popen") as popen:
            with contextlib.redirect_stderr(io.StringIO()):
                rc = seat._up("kimi", quiet=True)
        self.assertEqual(rc, 1)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        kill.assert_not_called()
        popen.assert_not_called()

    def test_nonexecutable_replacement_leaves_listener_and_config_intact(self):
        path, owned = self.stale_running_kimi()
        with open(path, "rb") as f:
            before = f.read()
        os.chmod(self.binary, 0o600)
        with mock.patch.object(seat, "_running_pid_rec", return_value=owned), \
                mock.patch.object(seat.signal, "pidfd_send_signal") as send, \
                mock.patch.object(seat.subprocess, "Popen") as popen:
            with contextlib.redirect_stderr(io.StringIO()):
                rc = seat._up("kimi", quiet=True)
        self.assertEqual(rc, 1)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        send.assert_not_called()
        popen.assert_not_called()

    def test_restart_revalidates_birth_before_signal_and_never_spawns_on_reuse(self):
        _path, owned = self.stale_running_kimi()
        with mock.patch.object(seat, "_running_pid_rec", return_value=owned), \
                mock.patch.object(seat_proxy, "_proxy_binary_ready",
                                  return_value=(True, None)), \
                mock.patch.object(seat, "_pid_alive", return_value=True), \
                mock.patch.object(seat, "_pid_identity", return_value="proc:reused"), \
                mock.patch.object(seat.os, "pidfd_open", return_value=99), \
                mock.patch.object(seat_proxy.os, "close"), \
                mock.patch.object(seat.signal, "pidfd_send_signal") as send, \
                mock.patch.object(seat.subprocess, "Popen") as popen:
            with contextlib.redirect_stderr(io.StringIO()):
                rc = seat._up("kimi", quiet=True)
        self.assertEqual(rc, 1)
        send.assert_not_called()
        popen.assert_not_called()

    def test_listener_exit_between_birth_check_and_sigterm_is_clean_stop(self):
        _path, owned = self.stale_running_kimi()
        with mock.patch.object(seat, "_running_pid_rec", return_value=owned), \
                mock.patch.object(seat_proxy, "_proxy_binary_ready",
                                  return_value=(True, None)), \
                mock.patch.object(seat, "_pid_alive", return_value=True), \
                mock.patch.object(seat, "_pid_identity", return_value="proc:old"), \
                mock.patch.object(seat.os, "pidfd_open", return_value=99), \
                mock.patch.object(seat_proxy.os, "close"), \
                mock.patch.object(seat_proxy, "_pidfd_exited", return_value=False), \
                mock.patch.object(seat.signal, "pidfd_send_signal",
                                  side_effect=ProcessLookupError), \
                mock.patch.object(seat, "_port_open",
                                  side_effect=(False, True, True)), \
                mock.patch.object(seat.subprocess, "Popen",
                                  return_value=FakeProcess()) as popen:
            self.assertEqual(seat._up("kimi", quiet=True), 0)
        popen.assert_called_once()

    def test_stubborn_exact_listener_refuses_without_sigkill_or_spawn(self):
        path, owned = self.stale_running_kimi()
        with open(path, "rb") as f:
            before = f.read()
        signals = []
        with mock.patch.object(seat, "_running_pid_rec", return_value=owned), \
                mock.patch.object(seat_proxy, "_proxy_binary_ready",
                                  return_value=(True, None)), \
                mock.patch.object(seat, "_pid_alive", return_value=True), \
                mock.patch.object(seat, "_pid_identity", return_value="proc:old"), \
                mock.patch.object(seat.os, "pidfd_open", return_value=99), \
                mock.patch.object(seat_proxy.os, "close"), \
                mock.patch.object(seat_proxy, "_pidfd_exited", return_value=False), \
                mock.patch.object(
                    seat.signal, "pidfd_send_signal",
                    side_effect=lambda _fd, sig: signals.append(sig)), \
                mock.patch.object(seat.time, "sleep"), \
                mock.patch.object(seat.subprocess, "Popen") as popen:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = seat._up("kimi", quiet=True)
        self.assertEqual(rc, 1)
        self.assertEqual(signals, [signal.SIGTERM])
        self.assertIn("survived SIGTERM", err.getvalue())
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        popen.assert_not_called()

    def test_force_stop_waits_for_sigkill_exit_before_unlinking(self):
        _path, owned = self.stale_running_kimi()
        signals = []
        with mock.patch.object(seat, "_pid_alive", return_value=True), \
                mock.patch.object(seat, "_pid_identity", return_value="proc:old"), \
                mock.patch.object(seat.os, "pidfd_open", return_value=99), \
                mock.patch.object(seat_proxy.os, "close"), \
                mock.patch.object(
                    seat_proxy, "_pidfd_exited",
                    side_effect=(False,) * 26 + (True,)), \
                mock.patch.object(
                    seat.signal, "pidfd_send_signal",
                    side_effect=lambda _fd, sig: signals.append(sig)), \
                mock.patch.object(seat.time, "sleep"):
            stopped, why = seat_proxy._stop_owned_proxy(
                "kimi", "kimi", owned, force=True)
        self.assertTrue(stopped)
        self.assertIsNone(why)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertFalse(os.path.exists(
            os.path.join(seat._proxy_home("kimi"), "proxy.pid")))

    def test_force_stop_retains_ownership_when_sigkill_exit_is_unproved(self):
        _path, owned = self.stale_running_kimi()
        signals = []
        with mock.patch.object(seat, "_pid_alive", return_value=True), \
                mock.patch.object(seat, "_pid_identity", return_value="proc:old"), \
                mock.patch.object(seat.os, "pidfd_open", return_value=99), \
                mock.patch.object(seat_proxy.os, "close"), \
                mock.patch.object(seat_proxy, "_pidfd_exited", return_value=False), \
                mock.patch.object(
                    seat.signal, "pidfd_send_signal",
                    side_effect=lambda _fd, sig: signals.append(sig)), \
                mock.patch.object(seat.time, "sleep"):
            stopped, why = seat_proxy._stop_owned_proxy(
                "kimi", "kimi", owned, force=True)
        self.assertFalse(stopped)
        self.assertIn("survived SIGKILL", why)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertTrue(os.path.exists(
            os.path.join(seat._proxy_home("kimi"), "proxy.pid")))

    def test_regeneration_failure_keeps_bytes_listener_and_signal_quiet(self):
        path = self.write_config("kimi", "port: 8318\nmalformed: true\n")
        with open(path, "rb") as f:
            before = f.read()
        owned = {"pid": 4242, "identity": "proc:old", "launch": None}
        with mock.patch.object(seat, "_running_pid_rec", return_value=owned), \
                mock.patch.object(seat.os, "kill") as kill, \
                mock.patch.object(seat.subprocess, "Popen") as popen:
            with contextlib.redirect_stderr(io.StringIO()):
                rc = seat._up("kimi", quiet=True)
        self.assertEqual(rc, 1)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        kill.assert_not_called()
        popen.assert_not_called()

    def test_failed_replacement_restores_previous_config_and_listener(self):
        path, owned = self.stale_running_kimi()
        with open(path, "rb") as f:
            before = f.read()
        restored = FakeProcess()
        with mock.patch.object(seat, "_running_pid_rec", return_value=owned), \
                mock.patch.object(seat_proxy, "_proxy_binary_ready",
                                  return_value=(True, None)), \
                mock.patch.object(seat, "_pid_alive", return_value=True), \
                mock.patch.object(seat, "_pid_identity",
                                  side_effect=("proc:old", "proc:new")), \
                mock.patch.object(seat.os, "pidfd_open", return_value=99), \
                mock.patch.object(seat_proxy.os, "close"), \
                mock.patch.object(seat_proxy, "_pidfd_exited",
                                  side_effect=(False, True)), \
                mock.patch.object(seat.signal, "pidfd_send_signal"), \
                mock.patch.object(seat, "_port_open",
                                  side_effect=(False, True, True)), \
                mock.patch.object(seat.subprocess, "Popen",
                                  side_effect=(OSError("injected exec failure"),
                                               restored)) as popen:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(seat._up("kimi", quiet=True), 1)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(seat._proxy_pid_record("kimi")["identity"], "proc:new")

    def test_digest_change_restarts_and_records_new_launch_inputs(self):
        path, owned = self.stale_running_kimi()
        alive = iter((True, False))

        def pid_alive(pid):
            return next(alive) if pid == 4242 else True

        def identity(pid):
            return "proc:old" if pid == 4242 else "proc:new"

        process = FakeProcess()
        signals = []
        with mock.patch.object(seat, "_running_pid_rec", return_value=owned), \
                mock.patch.object(seat_proxy, "_proxy_binary_ready",
                                  return_value=(True, None)), \
                mock.patch.object(seat, "_pid_alive", side_effect=pid_alive), \
                mock.patch.object(seat, "_pid_identity", side_effect=identity), \
                mock.patch.object(seat.os, "pidfd_open", return_value=99), \
                mock.patch.object(seat_proxy.os, "close"), \
                mock.patch.object(seat_proxy, "_pidfd_exited",
                                  side_effect=(False, True)), \
                mock.patch.object(
                    seat.signal, "pidfd_send_signal",
                    side_effect=lambda _fd, sig: signals.append(sig)), \
                mock.patch.object(seat, "_port_open",
                                  side_effect=(False, True, True)), \
                mock.patch.object(seat.subprocess, "Popen",
                                  return_value=process) as popen:
            self.assertEqual(seat._up("kimi", quiet=True), 0)
        self.assertEqual(signals, [signal.SIGTERM])
        popen.assert_called_once()
        rec = seat._proxy_pid_record("kimi")
        self.assertEqual(rec["pid"], 5000)
        self.assertEqual(rec["identity"], "proc:new")
        self.assertEqual(rec["launch"], seat._proxy_launch_inputs(path, self.binary))
        with open(path) as f:
            self.assertNotIn("force-mapping", f.read())


if __name__ == "__main__":
    unittest.main()
