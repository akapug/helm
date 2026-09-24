#!/usr/bin/env python3
"""The config a proxy is RUNNING vs the one the generator promised.

MEASURED 2026-07-29, and the shape is the point. ds4pro's proxy config carried
no `nonstream-keepalive-interval`. kimi's — written in the SAME SECOND, by the
same generator path — carried 15. Neither had been re-minted; both had been
hand-patched after the fix landed, and ds4pro was the one that got missed.

`_config_yaml`'s docstring already named the consequence, in advance:

    a long non-streaming pass (compaction's ~360k summarize — the longest
    single request a session makes) sits silent while the upstream thinks, the
    proxy reaps the idle socket, and Claude Code gets an empty HTTP 200

From outside that is a seat that simply stopped answering. The owner had been
reporting exactly that about ds4pro for a week, as unreliability, and every
diagnosis went looking at the seat rather than at the socket under it.

THE MISS IS THE INTERESTING PART. The generator was fixed. The docstring even
predicted this failure — "the live family configs carry 15s by hand; the
generator must emit it too or every re-mint silently strips the fix" — and it
still happened, because the prediction covered configs that WOULD be re-minted
and said nothing about the ones that never were. A fix at the point of
generation cannot reach a file that is never generated again. Nothing read the
live files back, so a hand-patch that skipped one family stayed skipped.

That is why this is a CENSUS and not a stricter generator: the check has to
read what is actually on disk, because that is the only artifact the proxy
ever loads.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import seat  # noqa: E402

# ONE LITERAL, ONE MARKABLE LINE. `ds4pro` is the FAMILY key proxy_config_plan
# is computed for, not a seat name -- the seat-name rung matches the string and
# cannot tell the two apart, and several of the sites below are line
# continuations that cannot carry a trailing comment at all. Naming it once
# puts the claim about what it MEANS where a reader will find it.
FAMILY = "ds4pro"  # noqa: SEAT_NAME — the FAMILY key, never a seat

# THE PLANTED CONFIG IS *THIS FAMILY'S* OWN ROUTE, ASKED OF THE CATALOG. GOOD
# is "the config helm would generate today" for FAMILY, and a provider block is
# part of that desired state only when `_eligible_providers` can match it to a
# catalogued route -- which it does on the pair (provider name, claude-side
# alias). A block matching NO route leaves the eligible set EMPTY, and an empty
# eligible set is an unreadable desired state that RAISES, so these arms would
# read the doctor's "desired state unreadable" branch instead of the drift line
# they are about.
#
# THE EARLIER SPELLING DERIVED ONLY THE UPSTREAM MODEL, off whichever family
# declared an `openrouter` pool row, and hard-named the provider and the alias
# beside it. That was true while THIS family declared that row. The split that
# gave the flash route its own family left the fixture planting a provider this
# family no longer catalogues, carrying an upstream that now comes from the
# OTHER family, under this family's alias -- a config helm would generate for
# nobody. Asking the catalog for the family's own first route is the question
# that survives the next split as well, and it names no provider and no
# upstream here at all.
_ROUTE = seat.proxy_routes(FAMILY)[0]
GOOD = seat._config_yaml_key(
    seat.FAMILIES[FAMILY]["port"], "tok", _ROUTE["provider"],
    _ROUTE["base_url"], _ROUTE["alias"], "outbound",
    _ROUTE["upstream_model"])

DRIFTED = GOOD.replace("nonstream-keepalive-interval: 15\n", "")


class ValuesTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-cfg-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _w(self, text, name="config.yaml"):
        p = os.path.join(self.d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_only_TOP_LEVEL_scalars_are_read(self):
        """`allow-remote` is nested under remote-management. Reading it as a
        top-level key would let a nested value satisfy — or contradict — an
        invariant that was never about it."""
        v = seat._config_values(self._w(GOOD))
        self.assertIn("nonstream-keepalive-interval", v)
        self.assertNotIn("allow-remote", v)

    def test_an_inline_comment_is_not_part_of_the_value(self):
        """The live configs carry them: `transient-error-cooldown-seconds: 5
        # 60s default benches a cred on any transient blip`. A value compared
        with its comment attached never equals the invariant, so every
        commented line would report as drift forever."""
        v = seat._config_values(
            self._w("nonstream-keepalive-interval: 15  # compaction survival\n"))
        self.assertEqual(v["nonstream-keepalive-interval"], "15")

    def test_a_comment_line_is_not_a_key(self):
        v = seat._config_values(self._w("# nonstream-keepalive-interval: 99\n"))
        self.assertEqual(v, {})

    def test_an_unreadable_config_is_None_not_empty(self):
        """Empty would read as 'every invariant absent' and alarm on a
        permissions problem. None lets the caller stay silent."""
        self.assertIsNone(seat._config_values(os.path.join(self.d, "nope.yaml")))


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-drift-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _w(self, text):
        p = os.path.join(self.d, "config.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_a_conforming_config_reports_nothing(self):
        self.assertEqual(seat.config_drift(self._w(GOOD)), [])

    def test_THE_ds4pro_SHAPE_is_reported_with_got_None(self):
        """The regression itself: the key absent entirely, not merely wrong."""
        d = seat.config_drift(self._w(DRIFTED))
        self.assertEqual(len(d), 1)
        key, want, got, why = d[0]
        self.assertEqual(key, "nonstream-keepalive-interval")
        self.assertEqual(want, "15")
        self.assertIsNone(got, "ABSENT must be distinguishable from a wrong value")
        self.assertIn("EMPTY HTTP 200", why)

    def test_a_WRONG_value_is_reported_too_and_carries_it(self):
        """A stripped key and a hand-lowered value fail the same way at
        runtime; an operator needs to see which one they have."""
        d = seat.config_drift(self._w(GOOD.replace(": 15", ": 0")))
        self.assertEqual(d[0][2], "0")

    def test_an_unreadable_config_reports_no_drift(self):
        """doctor must never turn a permissions problem into a config alarm."""
        self.assertEqual(seat.config_drift(os.path.join(self.d, "nope.yaml")), [])


class DoctorLinesTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-dl-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _home(self, text):
        os.makedirs(self.d, exist_ok=True)
        with open(os.path.join(self.d, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(text)
        return self.d

    def test_a_drifted_seat_names_itself_in_the_line(self):
        with mock.patch.object(seat, "_minted_seats",
                               return_value=[(FAMILY, FAMILY)]), \
                mock.patch.object(seat, "_proxy_home",
                                  return_value=self._home(DRIFTED)):
            lines = seat._config_drift_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn(FAMILY, lines[0])
        self.assertIn("generated proxy policy differs", lines[0])

    def test_a_clean_fleet_produces_no_lines(self):
        with mock.patch.object(seat, "_minted_seats",
                               return_value=[(FAMILY, FAMILY)]), \
                mock.patch.object(seat, "_proxy_home",
                                  return_value=self._home(GOOD)):
            self.assertEqual(seat._config_drift_lines(), [])

    def test_an_UNREADABLE_provider_is_NAMED_even_when_nothing_drifts(self):
        """THE SIGNED CONTRACT'S REPORTING LEG. A provider whose own-depth YAML
        this generator cannot read is SKIPPED rather than refused, so no other
        line on this surface says it exists -- and the moment that matters is
        the SETTLED one, because once the readable routes are canonical the
        plan reports no change and a route that quietly left the desired state
        would be invisible on the only path that renders a seat's config.

        So the line is asserted on a config that is otherwise CLEAN: the drift
        line is absent, and the opaque line is present anyway. A returned dict
        field alone is not a report.
        """
        # A SECOND PROVIDER, UNREADABLE, BESIDE THE CANONICAL ONE. It has to
        # be a NEIGHBOUR rather than the only block: a config whose ONLY
        # provider is unreadable has an empty eligible set, which refuses and
        # takes the OTHER branch of this renderer -- the "desired state
        # unreadable" line -- and would prove nothing about reporting a SKIP.
        opaque = GOOD.replace(
            'nonstream-keepalive-interval: 15\n',
            '  - name: "retained-thirdparty"\n'
            '    <<: {defaults: true}\n'
            '    base-url: "https://example.invalid/v1"\n'
            'nonstream-keepalive-interval: 15\n', 1)
        self.assertNotEqual(opaque, GOOD, "the fixture did not change")
        with mock.patch.object(seat, "_minted_seats",
                               return_value=[(FAMILY, FAMILY)]), \
                mock.patch.object(seat, "_proxy_home",
                                  return_value=self._home(opaque)):
            lines = seat._config_drift_lines()
        opaque_lines = [ln for ln in lines if ln.startswith("config opaque:")]
        self.assertEqual(len(opaque_lines), 1, lines)
        self.assertIn(FAMILY, opaque_lines[0])
        self.assertIn("SKIPPED", opaque_lines[0])
        self.assertIn("retained-thirdparty", opaque_lines[0])
        # BOTH NUMBERS, so a reader and the code cannot mean different blocks.
        self.assertIn("index 1", opaque_lines[0])
        self.assertIn("2nd in file order", opaque_lines[0])
        # AND NOTHING DRIFTED: the readable route is canonical, so this line
        # is not a by-product of a rewrite.
        self.assertEqual([ln for ln in lines
                          if ln.startswith("config drift:")], [], lines)
        # THE CONTROL, on the SAME surface and the same seat: the clean config
        # produces NO opaque line. Without it the assertion above is satisfied
        # by a renderer that names an opaque section unconditionally.
        with mock.patch.object(seat, "_minted_seats",
                               return_value=[(FAMILY, FAMILY)]), \
                mock.patch.object(seat, "_proxy_home",
                                  return_value=self._home(GOOD)):
            self.assertEqual(
                [ln for ln in seat._config_drift_lines()
                 if ln.startswith("config opaque:")], [])

    def test_the_opaque_line_never_prints_a_CREDENTIAL(self):
        """MEASURED DISCLOSURE, not a theoretical one. An earlier cut of this
        report carried the item's first non-empty line so an operator could
        find it by eye. A provider mapping does not have to start with `name:`
        -- put `api-key:` first and that line IS the credential, printed to the
        doctor's console and into every log that captures it.

        So only the decoded `name` scalar is published, and its absence is said
        in words. The arm plants a secret whose ONLY appearance would be that
        raw line, and asserts on the rendered output rather than on the field.
        """
        secret = "sk-" + "S" * 24
        leaky = GOOD.replace(
            'nonstream-keepalive-interval: 15\n',
            '  - api-key: "%s"\n'
            '    <<: {defaults: true}\n'
            '    name: "retained-thirdparty"\n'
            '    base-url: "https://example.invalid/v1"\n'
            'nonstream-keepalive-interval: 15\n' % secret, 1)
        with mock.patch.object(seat, "_minted_seats",
                               return_value=[(FAMILY, FAMILY)]), \
                mock.patch.object(seat, "_proxy_home",
                                  return_value=self._home(leaky)):
            lines = seat._config_drift_lines()
        rendered = "\n".join(lines)
        # THE UNCONDITIONAL POSITIVE CONTROL ON THE SAME STRING: the block IS
        # reported, so the absence below is redaction and not silence.
        self.assertIn("SKIPPED", rendered, lines)
        self.assertIn("retained-thirdparty", rendered)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("api-key", rendered)

    def test_a_seat_with_no_config_file_is_skipped_not_reported(self):
        """A minted seat whose proxy was never brought up has no config. That
        is `helm seat up`'s story, not a drift finding."""
        with mock.patch.object(seat, "_minted_seats",
                               return_value=[("ghost", "ghost")]), \
                mock.patch.object(seat, "_proxy_home",
                                  return_value=os.path.join(self.d, "absent")):
            self.assertEqual(seat._config_drift_lines(), [])




class ProxyDebugKnobTest(unittest.TestCase):
    """`debug:` was a constant standing where an operator knob belonged.

    The fleet spent a week trying to diagnose a silent token-drop with proxy
    logging structurally impossible to enable — every fix round was blind,
    which is a plausible reason none of them stuck. Same bug class as the
    `_seat_env` hardcode that defeated HELM_NODE_COORD_FEE: a constant is
    invisible until the day the knob is needed.
    """

    def _debug_of(self, text):
        return [l for l in text.splitlines() if l.startswith("debug:")][0]

    def test_the_default_is_OFF(self):
        """These configs front sessions carrying the owner's work; a debug
        proxy logs request and response shapes. Opt-in, never sticky."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HELM_PROXY_DEBUG", None)
            self.assertEqual(seat.proxy_debug(), "false")

    def test_falsey_spellings_stay_off(self):
        for v in ("", "0", "false", "no"):
            with mock.patch.dict(os.environ, {"HELM_PROXY_DEBUG": v}):
                self.assertEqual(seat.proxy_debug(), "false", v)

    def test_the_knob_reaches_BOTH_generators(self):
        """One generator serves OAuth families and the other serves proxy-key
        families. A knob wired into one of them is a knob that works for half
        the fleet and silently does not for the other — which is exactly how
        ds4pro came to differ from kimi in the first place."""
        with mock.patch.dict(os.environ, {"HELM_PROXY_DEBUG": "1"}):
            self.assertEqual(
                self._debug_of(seat._config_yaml(1, "/auth", "tok")),
                "debug: true")
            self.assertEqual(
                self._debug_of(seat._config_yaml_key(
                    1, "tok", "prov", "https://x.invalid/v1", "mdl", "key")),
                "debug: true")

    def test_the_keyed_generator_keeps_its_fields_in_the_right_slots(self):
        """%-args bind by POSITION. `debug:` sits above the
        openai-compatibility block, so appending the new value instead of
        inserting it would shift provider -> base-url -> api-key by one and
        emit a config that still PARSES — a silent misconfiguration, not a
        crash. Pinned by asserting a field that would move if it regressed."""
        out = seat._config_yaml_key(8360, "tok", "opencode-go",
                                    "https://example.invalid/v1", "ds4-pro",
                                    "KEYMATERIAL", upstream="vendor/real-model")
        self.assertIn('base-url: "https://example.invalid/v1"', out)
        self.assertIn('- name: "vendor/real-model"', out)
        self.assertIn('alias: "ds4-pro"', out)
        self.assertIn('name: "opencode-go"', out)

if __name__ == "__main__":
    unittest.main()


class SidecarMeterKnobTest(unittest.TestCase):
    """`usage-statistics-enabled: false` was a constant standing where the
    only meter that can attribute a pooled account's burn to a seat belongs.

    With it off the fork discards every usage record at its sink, so no
    reader could ever say which sidecar spent which share of a pooled
    account. The knob must reach BOTH generators (the same half-fleet hazard
    as the debug knob above) and change NOTHING else: the controls below are
    the generators' exact outputs pinned before the meter was switched on.
    """

    KIMI_BEFORE = 'host: "127.0.0.1"\nport: 8318\napi-keys:\n  - "tok"\ndebug: false\nusage-statistics-enabled: false\nremote-management:\n  allow-remote: false\n  secret-key: ""\n  disable-control-panel: true\nopenai-compatibility:\n  - name: "moonshot"\n    base-url: "https://api.moonshot.ai/v1"\n    api-key-entries:\n      - api-key: "key"\n    models:\n      - name: "kimi-k3"\n        alias: "kimi-k3"\n      - name: "kimi-k3"\n        alias: "claude-opus-5"\n      - name: "kimi-k3"\n        alias: "claude-sonnet-5"\n      - name: "kimi-k3"\n        alias: "claude-haiku-4-5-20251001"\n      - name: "kimi-k3"\n        alias: "claude-fable-5-1"\n      - name: "kimi-k3"\n        alias: "claude-haiku-4-5"\n      - name: "kimi-k3"\n        alias: "claude-opus-5-5"\nnonstream-keepalive-interval: 15\ntransient-error-cooldown-seconds: 5\nstreaming:\n  keepalive-seconds: 15\n  bootstrap-retries: 2\n'

    CODEX_BEFORE = 'host: "127.0.0.1"\nport: 8317\nauth-dir: "/auth"\napi-keys:\n  - "tok"\ndebug: false\nusage-statistics-enabled: false\nremote-management:\n  allow-remote: false\n  secret-key: ""\n  disable-control-panel: true\nnonstream-keepalive-interval: 15\ntransient-error-cooldown-seconds: 5\nstreaming:\n  keepalive-seconds: 15\n  bootstrap-retries: 2\n# built-in subagent frontmatter ids -> this family\'s model (task/1948)\n# (per id where the family declares subagent_tiers: one pane, two models)\noauth-model-alias:\n  codex:\n    - name: "gpt-6-astra"\n      alias: "claude-opus-5"\n      fork: true\n    - name: "gpt-5.6-sol"\n      alias: "claude-sonnet-5"\n      fork: true\n    - name: "gpt-5.6-sol"\n      alias: "claude-haiku-4-5-20251001"\n      fork: true\n    - name: "gpt-6-astra"\n      alias: "claude-fable-5-1"\n      fork: true\n    - name: "gpt-5.6-sol"\n      alias: "claude-haiku-4-5"\n      fork: true\n    - name: "gpt-6-astra"\n      alias: "claude-opus-5-5"\n      fork: true\n'

    METER = ("usage-statistics-enabled: true\n"
             "redis-usage-queue-retention-seconds: 3600\n")

    def _clean(self):
        return mock.patch.dict(os.environ, {"HELM_PROXY_DEBUG": ""})

    def test_the_meter_is_on_in_BOTH_generators(self):
        """Positive: each generator emits the flag true and the fork's
        ceiling retention right under it. Control: the old `false` spelling
        is gone from both, so a reconcile cannot find it canonical."""
        with self._clean():
            oauth = seat._config_yaml(8317, "/auth", "tok", channel="codex",
                                      model="gpt-6-astra", family="codex")
            keyed = seat._config_yaml_key(
                8318, "tok", "moonshot", "https://api.moonshot.ai/v1",
                "kimi-k3", "key")
        for text in (oauth, keyed):
            self.assertIn("debug: false\n" + self.METER + "remote-management:\n",
                          text)
            self.assertNotIn("usage-statistics-enabled: false", text)

    def test_the_keyed_output_is_otherwise_byte_identical_to_its_pin(self):
        """The kimi control: today's keyed output minus the two meter lines
        is the pinned pre-meter output, byte for byte — so the flag flip is
        the whole diff and no positional %-slot moved."""
        with self._clean():
            keyed = seat._config_yaml_key(
                8318, "tok", "moonshot", "https://api.moonshot.ai/v1",
                "kimi-k3", "key")
        self.assertEqual(keyed.replace(self.METER, "usage-statistics-enabled: false\n"),
                         self.KIMI_BEFORE)
        self.assertNotEqual(keyed, self.KIMI_BEFORE)   # the pin is not the output

    def test_the_oauth_output_is_otherwise_byte_identical_to_its_pin(self):
        """The codex control, same shape: the tier-table alias block and every
        keepalive line survive unchanged."""
        with self._clean():
            oauth = seat._config_yaml(8317, "/auth", "tok", channel="codex",
                                      model="gpt-6-astra", family="codex")
        self.assertEqual(oauth.replace(self.METER, "usage-statistics-enabled: false\n"),
                         self.CODEX_BEFORE)
        self.assertNotEqual(oauth, self.CODEX_BEFORE)
