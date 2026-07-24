#!/usr/bin/env python3
"""idle_dispatch tests — the stranded-obligation join (open dispatch x idle
recipient). Hermetic: dispatches.open_rows, presence, claims, derive_seat, and
dm are all patched, so no ledger/roster/chat node is touched. Verifies the
watchdog flags EXACTLY the stranded dispatches and DMs the resolved sender."""
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-idle-"))

from helm import idle_dispatch  # noqa: E402


def _row(rid, recipient, source="sess-oi", sender=None, deadline_s=2700):
    return {"id": rid, "recipient": recipient, "source": source,
            "sender": sender, "lane": "review", "deadline_s": deadline_s}


class IdleDispatchTest(unittest.TestCase):
    def setUp(self):
        # a fresh HELM_HOME per test so the fcntl latch state never leaks
        self.tmp = tempfile.mkdtemp(prefix="helm-test-idle-")
        self._prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self.tmp
        # default world: no claims, sender resolves to coordinator, every
        # recipient is old enough + quiet (idle) unless a test overrides
        self.rows = []
        self.presence = {}      # recipient -> "fresh"|"quiet"|"absent"
        self.claims = {}        # claim resource key -> {}
        self.age = 3600         # default dispatch age (> IDLE_DISPATCH_S)
        self.dms = []           # captured (to, text)
        self.p = mock.patch.multiple(
            "helm.idle_dispatch.dispatches",
            open_rows=mock.Mock(side_effect=lambda: list(self.rows)),
            _age_s=mock.Mock(side_effect=lambda r, now=None: self.age),
            _is_overdue=mock.Mock(side_effect=lambda r, now=None: self.age >= r["deadline_s"]),
        )
        self.s = mock.patch.multiple(
            "helm.idle_dispatch.seats",
            _live_claims=mock.Mock(side_effect=lambda: dict(self.claims)),
            presence_of=mock.Mock(side_effect=lambda ls: ls),   # ls IS the bucket
            last_seen=mock.Mock(side_effect=lambda rec: self.presence.get(rec, "quiet")),
            derive_seat=mock.Mock(side_effect=lambda src: "coordinator"),
            dm=mock.Mock(side_effect=lambda to, text, **k: self.dms.append((to, text))),
        )
        self.p.start(); self.s.start()

    def tearDown(self):
        self.p.stop(); self.s.stop()
        if self._prior is not None:
            os.environ["HELM_HOME"] = self._prior

    def test_stranded_dispatch_dms_the_resolved_sender_exactly_once(self):
        self.rows = [_row("aaaaaaaa1111", "ds4pro")]
        self.presence = {"ds4pro": "absent"}
        res = idle_dispatch.check()
        self.assertEqual(len(res["alerted"]), 1)
        self.assertEqual(len(self.dms), 1)
        to, text = self.dms[0]
        self.assertEqual(to, "coordinator")           # DM the sender, not a broadcast
        self.assertIn("ds4pro", text)
        self.assertIn("aaaaaaaa", text)                    # the id8

    def test_fresh_recipient_is_busy_not_stranded(self):
        self.rows = [_row("bbbbbbbb2222", "kimi")]
        self.presence = {"kimi": "fresh"}                  # crossed a tool boundary recently
        res = idle_dispatch.check()
        self.assertEqual(res["alerted"], [])
        self.assertEqual(self.dms, [])

    def test_self_dispatch_never_dms_yourself(self):
        # recipient == resolved sender (coordinator) -> no coordinator to wake
        self.rows = [_row("cccccccc3333", "coordinator")]
        self.presence = {"coordinator": "absent"}
        res = idle_dispatch.check()
        self.assertEqual(res["alerted"], [])

    def test_too_fresh_dispatch_gets_the_recipient_time(self):
        self.rows = [_row("dddddddd4444", "ds4pro")]
        self.presence = {"ds4pro": "absent"}
        self.age = idle_dispatch.IDLE_DISPATCH_S - 1       # under the soft window
        res = idle_dispatch.check()
        self.assertEqual(res["alerted"], [])

    def test_claimed_dispatch_is_being_worked(self):
        self.rows = [_row("eeeeeeee5555", "ds4pro")]
        self.presence = {"ds4pro": "quiet"}
        self.claims = {"dispatch:eeeeeeee": {"session": "x"}}   # live claim on it
        res = idle_dispatch.check()
        self.assertEqual(res["alerted"], [])

    def test_unreadable_claims_fails_closed_no_flag(self):
        self.rows = [_row("ffffffff6666", "ds4pro")]
        self.presence = {"ds4pro": "absent"}
        with mock.patch("helm.idle_dispatch.seats._live_claims", return_value=None):
            res = idle_dispatch.check()
        self.assertEqual(res["findings"], [])              # unsure -> never flag

    def test_latched_once_per_episode_then_rearms(self):
        self.rows = [_row("aaaaaaaa1111", "ds4pro")]
        self.presence = {"ds4pro": "absent"}
        idle_dispatch.check()
        self.dms.clear()
        idle_dispatch.check()                              # within LATCH_TTL_S
        self.assertEqual(self.dms, [])                     # latched, no second DM
        # the dispatch resolves (gone from open_rows) -> latch re-arms
        self.rows = []
        idle_dispatch.check()
        self.rows = [_row("aaaaaaaa1111", "ds4pro")]       # re-strands
        idle_dispatch.check()
        self.assertEqual(len(self.dms), 1)                 # alerts again after re-arm

    def test_dry_run_is_non_mutating(self):
        self.rows = [_row("aaaaaaaa1111", "ds4pro")]
        self.presence = {"ds4pro": "absent"}
        idle_dispatch.check(post=False)                    # dry-run: no DM, no latch write
        self.assertEqual(self.dms, [])
        res = idle_dispatch.check(post=True)               # a real run still alerts
        self.assertEqual(len(self.dms), 1)
        self.assertEqual(len(res["alerted"]), 1)


if __name__ == "__main__":
    unittest.main()
