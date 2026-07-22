#!/usr/bin/env python3
"""helm dispatch — the durable hand-off ledger. Every test drives a scratch
HELM_HOME so the live fleet ledger is never touched."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest

from helm import dispatches, seats

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL")


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(args)
    return rc, out.getvalue(), err.getvalue()


class DispatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-dispatch-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def age(self, rid, seconds):
        """Rewrite a row's ts so it reads `seconds` old — the ledger is
        append-only, so append an aged snapshot rather than editing in place
        (same path the real code takes)."""
        r = dict(dispatches.rows()[rid])
        r["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                time.gmtime(time.time() - seconds))
        self.assertTrue(dispatches.ownerasks._append(
            r, dispatches.ledger_path()))


class LifecycleTest(DispatchBase):
    def test_posting_is_not_done_only_a_verdict_closes(self):
        row = dispatches.add("codex-3", "session-pid-resolver", ref="ce1e7dd")
        self.assertEqual(row["status"], "open")
        # ack records pickup and DELIBERATELY does not close: an ack is a
        # promise, and promises are what this ledger exists to stop trusting.
        r, why = dispatches.mark_ack(row["id"], "post-1")
        self.assertIsNone(why)
        self.assertEqual(r["status"], "acked")
        self.assertEqual([x["id"] for x in dispatches.open_rows()], [row["id"]])
        r, why = dispatches.mark_verdict(row["id"], "abc1234")
        self.assertIsNone(why)
        self.assertEqual(r["status"], "verdict")
        self.assertEqual(dispatches.open_rows(), [])

    def test_verdict_is_final(self):
        row = dispatches.add("kimi", "lane-x")
        dispatches.mark_verdict(row["id"], "sha")
        r, why = dispatches.mark_verdict(row["id"], "sha2")
        self.assertIsNone(r)
        self.assertIn("already has a verdict", why)
        r, why = dispatches.mark_ack(row["id"], "late-ack")
        self.assertIsNone(r)

    def test_refs_are_required_so_a_close_always_carries_evidence(self):
        row = dispatches.add("codex", "lane-y")
        for fn in (dispatches.mark_ack, dispatches.mark_verdict):
            r, why = fn(row["id"], "   ")
            self.assertIsNone(r)
            self.assertIn("needs a ref", why)

    def test_unknown_id_is_named_not_swallowed(self):
        r, why = dispatches.mark_verdict("deadbeef", "sha")
        self.assertIsNone(r)
        self.assertIn("no such dispatch", why)

    def test_add_requires_recipient_and_lane(self):
        self.assertIsNone(dispatches.add("", "lane"))
        self.assertIsNone(dispatches.add("seat", "  "))


class OverdueTest(DispatchBase):
    def test_overdue_needs_the_deadline_to_actually_pass(self):
        row = dispatches.add("codex-3", "lane-a", deadline_s=600)
        self.assertEqual(dispatches.overdue(), [])
        self.age(row["id"], 599)
        self.assertEqual(dispatches.overdue(), [])
        self.age(row["id"], 601)
        self.assertEqual([r["id"] for r in dispatches.overdue()], [row["id"]])

    def test_a_verdict_removes_a_row_from_overdue(self):
        row = dispatches.add("codex-3", "lane-b", deadline_s=60)
        self.age(row["id"], 3600)
        self.assertTrue(dispatches.overdue())
        dispatches.mark_verdict(row["id"], "sha")
        self.assertEqual(dispatches.overdue(), [])

    def test_an_acked_row_still_goes_overdue(self):
        """Picking work up is not doing it — the whole point of ack not
        closing the row is that a silent seat after an ack is exactly the
        failure mode that started this."""
        row = dispatches.add("codex-3", "lane-c", deadline_s=60)
        dispatches.mark_ack(row["id"], "post-9")
        self.age(row["id"], 3600)
        self.assertEqual([r["id"] for r in dispatches.overdue()], [row["id"]])

    def test_utc_parse_does_not_skew_by_the_local_offset(self):
        """pk.now_ts() is UTC; parsing it with a LOCAL-time reader would shift
        every age by the tz offset and hide (or invent) overdue rows. A row
        stamped now must read ~0s old in any timezone."""
        row = dispatches.add("s", "lane-tz", deadline_s=60)
        self.assertLess(dispatches._age_s(dispatches.rows()[row["id"]]), 30)
        self.assertEqual(dispatches.overdue(), [])

    def test_unparseable_ts_reads_as_new_never_overdue(self):
        """Fail-open direction matters: a garbled row must never manufacture
        a false alarm that sends someone chasing a healthy seat."""
        self.assertEqual(dispatches._age_s({"ts": "not-a-time"}), 0)
        self.assertEqual(dispatches._age_s({}), 0)


class WhisperTest(DispatchBase):
    def test_overdue_dispatch_reaches_the_stop_whisper(self):
        """The RSH half: a ledger nothing surfaces is dead scaffolding."""
        self.assertIsNone(seats._dispatch_candidate())
        row = dispatches.add("codex-3", "session-pid-resolver", deadline_s=60)
        self.age(row["id"], 3600)
        got = seats._dispatch_candidate()
        self.assertIsNotNone(got)
        fp, text = got
        self.assertIn(row["id"], fp)
        self.assertIn("OVERDUE", text)
        self.assertIn("CHECK IN", text)
        self.assertIn("helm dispatch verdict", text)
        # and it must NOT tell anyone to reassign on age alone
        self.assertIn("do NOT", text)

    def test_fingerprint_changes_on_status_so_it_refires_once(self):
        row = dispatches.add("codex-3", "lane-d", deadline_s=60)
        self.age(row["id"], 3600)
        fp_open = seats._dispatch_candidate()[0]
        dispatches.mark_ack(row["id"], "post-2")
        self.age(row["id"], 3600)
        fp_acked = seats._dispatch_candidate()[0]
        self.assertNotEqual(fp_open, fp_acked)

    def test_whisper_goes_silent_once_the_verdict_lands(self):
        row = dispatches.add("codex-3", "lane-e", deadline_s=60)
        self.age(row["id"], 3600)
        self.assertIsNotNone(seats._dispatch_candidate())
        dispatches.mark_verdict(row["id"], "sha")
        self.assertIsNone(seats._dispatch_candidate())


class CmdTest(DispatchBase):
    def test_cli_round_trip(self):
        rc, out, _e = run(dispatches.cmd_dispatch,
                          ["add", "codex-3", "lane-f", "--ref", "abc",
                           "--deadline", "900"])
        self.assertEqual(rc, 0)
        self.assertIn("check back in 15m", out)
        rid = list(dispatches.rows())[0]
        rc, out, _e = run(dispatches.cmd_dispatch, ["list"])
        self.assertEqual(rc, 0)
        self.assertIn("lane-f", out)
        self.assertIn("abc", out)
        rc, out, _e = run(dispatches.cmd_dispatch, ["verdict", rid, "sha9"])
        self.assertEqual(rc, 0)
        rc, out, _e = run(dispatches.cmd_dispatch, ["list", "--open"])
        self.assertIn("nothing outstanding", out)

    def test_list_overdue_warns_against_reassigning_on_age(self):
        row = dispatches.add("codex-3", "lane-g", deadline_s=60)
        self.age(row["id"], 3600)
        rc, out, _e = run(dispatches.cmd_dispatch, ["list", "--overdue"])
        self.assertEqual(rc, 0)
        self.assertIn("OVERDUE", out)
        self.assertIn("do not reassign", out.lower())

    def test_json_is_machine_readable(self):
        dispatches.add("codex-3", "lane-h", ref="r1")
        rc, out, _e = run(dispatches.cmd_dispatch, ["list", "--json"])
        self.assertEqual(rc, 0)
        got = json.loads(out)
        self.assertEqual(got[0]["lane"], "lane-h")
        self.assertEqual(got[0]["ref"], "r1")

    def test_bad_usage_is_rc2_never_a_traceback(self):
        for args in ([], ["add"], ["add", "only-one"], ["bogus"],
                     ["ack", "id-only"]):
            rc, _o, err = run(dispatches.cmd_dispatch, args)
            self.assertEqual(rc, 2, args)
            self.assertNotIn("Traceback", err)

    def test_non_numeric_deadline_is_refused(self):
        rc, _o, err = run(dispatches.cmd_dispatch,
                          ["add", "s", "l", "--deadline", "soon"])
        self.assertEqual(rc, 2)
        self.assertIn("SECONDS", err)


class LedgerSeparationTest(DispatchBase):
    def test_dispatches_do_not_pollute_the_owner_ask_ledger(self):
        """Both ledgers share mechanics, NOT storage — an owner ask and a
        dispatch are different obligations with different closers."""
        from helm import ownerasks
        dispatches.add("codex-3", "lane-i")
        self.assertEqual(ownerasks.rows(), {})
        self.assertNotEqual(dispatches.ledger_path(), ownerasks.ledger_path())
        ownerasks.add("an owner ask")
        self.assertEqual(len(dispatches.rows()), 1)
        self.assertEqual(len(ownerasks.rows()), 1)


if __name__ == "__main__":
    unittest.main()
