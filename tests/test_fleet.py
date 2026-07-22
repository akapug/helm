#!/usr/bin/env python3
"""helm.fleet — the composition-truth verb. Hermetic: /proc and roster mocked."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import fleet  # noqa: E402


class FleetRowsTest(unittest.TestCase):
    def _wire(self, envs, daemons=frozenset(), sids=None):
        return [
            mock.patch.object(fleet, "_claude_pids", lambda: sorted(envs)),
            mock.patch.object(fleet, "_daemon_pids", lambda: set(daemons)),
            mock.patch.object(fleet, "_environ", lambda pid: envs.get(pid, {})),
            mock.patch.object(fleet, "_daemon_for",
                              lambda pid, ds: (sorted(ds)[0] if ds else None)),
            mock.patch.object(fleet, "_sid_for",
                              lambda pid, env: (sids or {}).get(pid, (None, None))),
        ]

    def test_stamps_and_deck_are_read_from_the_live_env(self):
        envs = {10: {"HELM_CHAT_NAME": "a-seat",
                     "CLAUDE_CODE_CHILD_SESSION": "1",
                     "CLAUDE_CODE_SESSION_ID": "x",
                     "HELM_SKILL_DECK": "/home/u/dev/mission-control/skills"}}
        ps = self._wire(envs, {99}, {10: ("sid-a", "record")})
        for p in ps: p.start()
        try:
            rows, daemons = fleet.rows()
        finally:
            for p in ps: p.stop()
        r = rows[0]
        self.assertEqual((r["seat"], r["stamps"], r["deck"], r["daemon"]),
                         ("a-seat", 2, "MC", 99))

    def test_a_daemonless_process_is_flagged_headless(self):
        ps = self._wire({7: {}}, set(), {7: ("s", "argv~ancestor")})
        for p in ps: p.start()
        try:
            rows, _ = fleet.rows()
        finally:
            for p in ps: p.stop()
        self.assertIsNone(rows[0]["daemon"])
        # the argv rung is labeled as the weaker source it is
        self.assertEqual(rows[0]["sid_src"], "argv~ancestor")

    def test_every_column_is_probed_never_cached(self):
        # the verb exists BECAUSE cached mental models rot: rows() must call
        # the live probes on every invocation
        calls = {"n": 0}
        def envs(pid):
            calls["n"] += 1
            return {}
        ps = self._wire({1: {}, 2: {}}, set(), {})
        ps[2] = mock.patch.object(fleet, "_environ", envs)
        for p in ps: p.start()
        try:
            fleet.rows(); fleet.rows()
        finally:
            for p in ps: p.stop()
        self.assertEqual(calls["n"], 4)
