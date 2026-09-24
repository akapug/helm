#!/usr/bin/env python3
"""`helm pi` — the eval's arm-pinning generator, and why it exists early.

0.3's §E-8 converged on shipping the EVIDENCE plus the thin seat adapter and
DEFERRING the pi extension "until the eval proves pi earns the surface." Right
instinct, wrong order, and measuring the host settled it:

  §E-5 pins the two arms as the same codex model, same tier, same task suite,
  under two harnesses. On this host `cc-codex` reaches codex through helm's
  CLIProxyAPI on the owner's SUBSCRIPTION OAuth, and pi has exactly ONE
  provider configured: openrouter. Run as-is, the eval compares
  Claude-Code-over-subscription with pi-over-OpenRouter and credits the whole
  difference to the harness — different provider, different billing, different
  rate limits, possibly a different model build. That is precisely the confound
  §E-5 was written to forbid.

pi's supported way to point at another endpoint is an extension calling
`pi.registerProvider(...)`, so a minimal extension is a PREREQUISITE of the
eval rather than a reward for it. This module generates only the provider
override; §B.2's reflex/chat bridge stays deferred as converged.

TWO INVARIANTS THESE PIN, and the second is the one that would hurt:

  1. The generated provider points at the SAME proxy cc-codex uses, so the
     comparison isolates the harness.
  2. NO CREDENTIAL MATERIAL IS EVER WRITTEN. pi interpolates `$VAR` in config
     values, so the shim carries an env REFERENCE and the token stays in the
     seat's 0600 proxy config. A generator that helpfully inlined the key would
     produce a world-readable .ts holding a live credential, and it would look
     completely fine in review.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import pi  # noqa: E402


class SourceTest(unittest.TestCase):
    def src(self, **kw):
        kw.setdefault("seat", "codex")
        kw.setdefault("port", 8317)
        kw.setdefault("model", "gpt-5.6-sol")
        return pi.extension_source(**kw)

    def test_it_points_at_the_seats_own_proxy(self):
        """The whole reason the file exists: same endpoint as cc-codex, so the
        eval measures the harness rather than the provider."""
        self.assertIn('baseUrl: "http://127.0.0.1:8317/v1"', self.src())

    def test_the_key_is_read_from_child_env_never_inlined(self):
        """THE ONE THAT WOULD HURT. A generator that inlined the token would
        write a live credential into a plain .ts under the user's home, and the
        diff would look entirely reasonable."""
        s = self.src()
        self.assertIn('apiKey: process.env["%s"] || ""' % pi.KEY_ENV, s)
        self.assertNotIn("sk-", s)

    def test_NO_credential_shaped_string_survives_generation(self):
        """A blunt negative control against every future edit: nothing in the
        output may look like key material, whatever the template does."""
        import re
        s = self.src()
        self.assertEqual(re.findall(r"[A-Za-z0-9_\-]{40,}", s), [])

    def test_the_openai_compatible_route_is_declared(self):
        """CLIProxyAPI speaks openai-completions on the route this targets;
        picking the wrong `api` fails at request time, not at generation."""
        self.assertIn('api: "openai-completions"', self.src())

    def test_an_upstream_alias_overrides_the_claude_side_name(self):
        """Families whose provider-side id differs from the claude-side alias
        (ds4pro: ds4-pro -> deepseek/deepseek-v4-pro) must send the UPSTREAM
        id, or the proxy rejects a model it has never heard of."""
        s = self.src(model="ds4-pro", upstream="deepseek/deepseek-v4-pro")
        self.assertIn('id: "deepseek/deepseek-v4-pro"', s)

    def test_the_provider_name_is_namespaced_to_helm(self):
        """A provider called `codex` would collide with pi's own built-ins and
        silently override them for every session on the box."""
        self.assertEqual(pi.provider_name("codex"), "helm-codex")
        self.assertIn('registerProvider("helm-codex"', self.src())


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pi-ext-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_dry_run_writes_NOTHING(self):
        out = os.path.join(self.d, "helm-codex.ts")
        path, src, err = pi.write_extension("codex", out=out)
        self.assertIsNone(err)
        self.assertTrue(src)
        self.assertFalse(os.path.exists(out), "a preview must not install")

    def test_apply_writes_the_file(self):
        out = os.path.join(self.d, "nested", "helm-codex.ts")
        path, src, err = pi.write_extension("codex", out=out, apply=True)
        self.assertIsNone(err)
        self.assertTrue(os.path.exists(out))
        self.assertIn("registerProvider", open(out, encoding="utf-8").read())

    def test_an_unknown_seat_is_refused_not_generated(self):
        """Generating a provider for a seat that does not exist would produce a
        shim pointing at a port nothing listens on — a failure that surfaces
        only mid-eval, as noise in the numbers."""
        path, src, err = pi.write_extension("not-a-seat")
        self.assertIsNone(src)
        self.assertIn("unknown seat", err)


class PortTest(unittest.TestCase):
    def test_the_port_comes_from_the_family_table_not_the_0600_config(self):
        """There is no reason to open a file holding a credential to learn a
        port number, and every reason not to."""
        port, err = pi.seat_port("codex")
        self.assertIsNone(err)
        self.assertTrue(isinstance(port, int) and port > 0)

    def test_an_unknown_family_reports_the_reason(self):
        port, err = pi.seat_port("nope-9")
        self.assertIsNone(port)
        self.assertIn("unknown seat", err)


class CmdTest(unittest.TestCase):
    def test_status_never_fails_when_pi_is_absent_from_PATH(self):
        """Most machines have no pi. status must report, not raise."""
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(pi.cmd_pi(["status"]), 1)

    def test_a_bare_verb_prints_usage_and_refuses(self):
        self.assertEqual(pi.cmd_pi([]), 2)

    def test_help_exits_zero(self):
        self.assertEqual(pi.cmd_pi(["--help"]), 0)

    def test_an_unknown_flag_is_NAMED_not_ignored(self):
        """helm's law. A verb that silently drops `--sate codex` generates a
        shim for the DEFAULT seat while the operator believes otherwise."""
        self.assertNotEqual(pi.cmd_pi(["extension", "--sate", "codex"]), 0)

    def test_a_valued_flag_keeps_its_value(self):
        """Regression: the first draft pre-filtered the tail to dashed tokens
        before guarding it, which strips a valued flag's value — `--seat codex`
        arrived as a bare `--seat` and the verb refused its own valid input."""
        self.assertEqual(pi.cmd_pi(["extension", "--seat", "codex"]), 0)


if __name__ == "__main__":
    unittest.main()
