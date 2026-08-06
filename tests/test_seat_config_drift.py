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

GOOD = """host: "127.0.0.1"
port: 8360
api-keys:
  - "tok"
debug: false
remote-management:
  allow-remote: false
nonstream-keepalive-interval: 15
"""

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
                               return_value=[("ds4pro", "ds4pro")]), \
                mock.patch.object(seat, "_proxy_home",
                                  return_value=self._home(DRIFTED)):
            lines = seat._config_drift_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("ds4pro", lines[0])
        self.assertIn("ABSENT", lines[0])

    def test_a_clean_fleet_produces_no_lines(self):
        with mock.patch.object(seat, "_minted_seats",
                               return_value=[("ds4pro", "ds4pro")]), \
                mock.patch.object(seat, "_proxy_home",
                                  return_value=self._home(GOOD)):
            self.assertEqual(seat._config_drift_lines(), [])

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
