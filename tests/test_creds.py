#!/usr/bin/env python3
"""creds/swap tests — the account scorecard row mapping and the rollover-rescue
resume-block shape. Hermetic: the provider, homes list and session rows are all
stubbed in-process; no real cred home, catalog or provider is touched."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import creds, homes, seat, sessions  # noqa: E402
from helm.providers import ProviderError  # noqa: E402

SID = "aaaaaaaa-1111-2222-3333-444444444444"


class StubProvider:
    """Canned rows in the exact shapes providers.py documents (the
    test_web_quota pattern)."""

    def __init__(self, accounts=(), states=(), windows=()):
        self._accounts = list(accounts)
        self._states = list(states)
        self._windows = list(windows)

    def accounts(self):
        return self._accounts

    def cred_state(self):
        return self._states

    def windows(self):
        return self._windows


class BrokenProvider:
    def __getattr__(self, name):
        def boom(*a, **k):
            raise ProviderError("no quota provider on this machine")
        return boom


def two_account_provider():
    return StubProvider(
        accounts=[{"account": "dry@x.com", "provider": "anthropic", "tier": "max"},
                  {"account": "fresh@x.com", "provider": "anthropic", "tier": "pro"}],
        states=[{"account": "dry@x.com", "cred_state": "dry",
                 "headroom_pct": 2, "resets_at_ms": 1},
                {"account": "fresh@x.com", "cred_state": "ok",
                 "headroom_pct": 80, "resets_at_ms": None}],
        windows=[{"account": "fresh@x.com", "windows_left": 3.5,
                  "windows_per_week": 12.0, "verdict": "plenty"}])


HOMES = [{"name": "h-dry", "identity": "dry@x.com", "provider": "claude",
          "path": "/creds/h-dry", "aliases": ["dry"]},
         {"name": "h-fresh", "identity": "fresh@x.com", "provider": "claude",
          "path": "/creds/h-fresh", "aliases": []}]


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(list(args))
    return rc, out.getvalue(), err.getvalue()


class RowsTest(unittest.TestCase):
    def test_row_mapping(self):
        with mock.patch.object(creds, "default_provider",
                               return_value=two_account_provider()):
            rows = creds._rows()
        self.assertEqual(len(rows), 2)
        dry = next(r for r in rows if r["account"] == "dry@x.com")
        self.assertEqual(dry["provider"], "anthropic")
        self.assertEqual(dry["tier"], "max")
        self.assertAlmostEqual(dry["headroom"], 0.02)  # pct -> fraction
        self.assertEqual(dry["state"], "dry")
        self.assertEqual(dry["resets_at_ms"], 1)
        self.assertIsNone(dry["windows_left"])
        fresh = next(r for r in rows if r["account"] == "fresh@x.com")
        self.assertAlmostEqual(fresh["headroom"], 0.80)
        self.assertEqual((fresh["windows_left"], fresh["windows_per_week"],
                          fresh["verdict"]), (3.5, 12.0, "plenty"))

    def test_cmd_creds_scorecard(self):
        with mock.patch.object(creds, "default_provider",
                               return_value=two_account_provider()):
            rc, out, _ = run(creds.cmd_creds, [])
        self.assertEqual(rc, 0)
        self.assertIn("helm creds (2 accounts):", out)
        lines = out.splitlines()
        # headroom-desc within provider: fresh (80%) before dry (2%)
        self.assertLess(next(i for i, l in enumerate(lines) if "fresh@x.com" in l),
                        next(i for i, l in enumerate(lines) if "dry@x.com" in l))
        fresh_line = next(l for l in lines if "fresh@x.com" in l)
        self.assertIn("80%", fresh_line)
        self.assertIn("3.5/12.0", fresh_line)
        self.assertIn("plenty", fresh_line)
        dry_line = next(l for l in lines if "dry@x.com" in l)
        self.assertIn("2%", dry_line)
        self.assertIn("now", dry_line)  # resets_at_ms in the past -> "now"

    def test_provider_error_degrades_with_the_reassurance(self):
        with mock.patch.object(creds, "default_provider",
                               return_value=BrokenProvider()):
            rc, out, _ = run(creds.cmd_creds, [])
        self.assertEqual(rc, 1)
        self.assertIn("no quota provider on this machine", out)
        self.assertIn("sessions/resume still work", out)

    def test_no_accounts(self):
        with mock.patch.object(creds, "default_provider",
                               return_value=StubProvider()):
            rc, out, _ = run(creds.cmd_creds, [])
        self.assertEqual(rc, 0)
        self.assertIn("no accounts found", out)


class SwapTest(unittest.TestCase):
    def swap(self, target, rows=None, homes_list=None):
        rows = rows if rows is not None else [
            {"h": "claude", "i": SID, "cwd": "/work/alpha"}]
        with mock.patch.object(homes, "homes_list",
                               return_value=homes_list or HOMES), \
                mock.patch.object(creds, "default_provider",
                                  return_value=two_account_provider()), \
                mock.patch.object(sessions, "rows_for", return_value=rows):
            return run(creds.cmd_swap, [target])

    def test_swap_picks_healthiest_alternative_and_prefixes_env(self):
        rc, out, _ = self.swap("h-dry")
        self.assertEqual(rc, 0)
        self.assertIn("healthiest claude alternative: fresh@x.com (headroom 80%",
                      out)
        self.assertIn(
            "    cd '/work/alpha' && env CLAUDE_CONFIG_DIR=/creds/h-fresh "
            + ResumeCommandTest.UNSET + "claude --resume " + SID, out)

    def test_swap_alias_resolves_the_home(self):
        rc, out, _ = self.swap("dry")
        self.assertEqual(rc, 0)
        self.assertIn("fresh@x.com", out)

    def test_swap_cwd_containing_provider_word_is_not_corrupted(self):
        # the pre-fix substring replace injected the env prefix INSIDE the
        # quoted cd path when the cwd contained "claude "
        rc, out, _ = self.swap(
            "h-dry", rows=[{"h": "claude", "i": SID, "cwd": "/work/claude stuff"}])
        self.assertEqual(rc, 0)
        self.assertIn(
            "    cd '/work/claude stuff' && env CLAUDE_CONFIG_DIR=/creds/h-fresh "
            + ResumeCommandTest.UNSET + "claude --resume " + SID, out)
        self.assertNotIn("cd '/work/env ", out)

    def test_swap_no_alternative(self):
        solo = [HOMES[0]]
        prov = StubProvider(
            accounts=[{"account": "dry@x.com", "provider": "anthropic"}],
            states=[{"account": "dry@x.com", "cred_state": "dry", "headroom_pct": 2}])
        with mock.patch.object(homes, "homes_list", return_value=solo), \
                mock.patch.object(creds, "default_provider", return_value=prov):
            rc, out, _ = run(creds.cmd_swap, ["h-dry"])
        self.assertEqual(rc, 1)
        self.assertIn("no alternative claude account", out)

    def test_swap_unknown_home(self):
        with mock.patch.object(homes, "homes_list", return_value=HOMES):
            rc, _, err = run(creds.cmd_swap, ["ghost"])
        self.assertEqual(rc, 1)
        self.assertIn("no cred home matches 'ghost'", err)

    def test_swap_usage(self):
        rc, _, err = run(creds.cmd_swap, [])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_swap_no_recent_sessions(self):
        rc, out, _ = self.swap("h-dry", rows=[])
        self.assertEqual(rc, 0)
        self.assertIn("no recent claude sessions in the catalog", out)


class ResumeCommandTest(unittest.TestCase):
    """Pin sessions.resume_command's exact per-harness format — the string
    cmd_swap's env-prefix injection depends on. The child-stamp unset run
    (env -u …, child-stamp-kills-seat-persistence) precedes the harness
    binary so a paste into a stamped shell can't resume as a subprocess
    child (transcript persistence silently OFF)."""

    # DERIVED FROM THE SEAM, NEVER RE-SPELLED. This constant used to hand-copy
    # the child-stamp prefix, so it went stale the instant the proxy triple
    # joined it (#107) — five tests failed for pinning a prefix rather than a
    # behaviour. What THESE tests own is COMPOSITION: that the command carries
    # the prefix, in the right place, without corrupting the quoted cd. The
    # prefix's CONTENTS are pinned exactly once, in test_seat's
    # paste-prefix arm, which fails if either register goes missing.
    UNSET = seat.paste_unset_prefix()

    def test_claude_format(self):
        self.assertEqual(
            sessions.resume_command({"h": "claude", "i": SID, "cwd": "/work/alpha"}),
            "cd '/work/alpha' && " + self.UNSET + "claude --resume " + SID)

    def test_codex_format(self):
        self.assertEqual(
            sessions.resume_command({"h": "codex", "i": SID, "cwd": "/work/beta"}),
            "cd '/work/beta' && " + self.UNSET + "codex resume " + SID)

    def test_missing_cwd_defaults_to_dot(self):
        self.assertEqual(
            sessions.resume_command({"h": "claude", "i": SID, "cwd": None}),
            "cd '.' && " + self.UNSET + "claude --resume " + SID)


if __name__ == "__main__":
    unittest.main()
