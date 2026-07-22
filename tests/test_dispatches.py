#!/usr/bin/env python3
"""Dispatch ledger: durable handoff, exact-tip verdict, and hostile storage tests.
All writes use scratch HELM_HOME/HELM_CHAT_DIR; the real ledger is read-only."""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from helm import dispatches, eventledger, seats

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "HELM_CELL_BIN", "HELM_CELL_PROFILE",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(args)
    return rc, out.getvalue(), err.getvalue()


class DispatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-dispatch-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.main = self.git("symbolic-ref", "--short", "HEAD")
        self.a = self.commit("a")
        self.git("branch", "side", self.a)
        self.b = self.commit("b")
        self.c = self.commit("c")
        self.git("checkout", "-q", "side")
        self.side = self.commit("side")
        self.git("checkout", "-q", self.main)

    def tearDown(self):
        for key, value in self.prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args, cwd=None):
        p = subprocess.run(["git", "-C", cwd or self.repo, *args],
                           capture_output=True, text=True, check=True)
        return p.stdout.strip()

    def commit(self, text):
        path = os.path.join(self.repo, "state")
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        self.git("add", "state")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def add(self, **kwargs):
        defaults = {"recipient": "codex-3", "lane": "lane-a", "ref": self.a,
                    "repo": self.repo}
        defaults.update(kwargs)
        row = dispatches.add(**defaults)
        self.assertIsNotNone(row)
        return row

    def age(self, rid, seconds, ts=None):
        """Backdate every snapshot of one synthetic row consistently.  Real
        code never rewrites the append-only ledger; this fixture controls the
        immutable opening timestamp before exercising a fresh disk replay."""
        path = dispatches.ledger_path()
        stamp = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(time.time() - seconds))
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for row in events:
                if row.get("id") == rid:
                    row["ts"] = stamp
                f.write(json.dumps(row, separators=(",", ":")) + "\n")


class LifecycleTest(DispatchBase):
    def test_delivery_observation_is_not_done_only_exact_tip_verdict_closes(self):
        row = self.add(lane="session-pid-resolver")
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["delivery"], "needs-confirmation")
        seen, why = dispatches._mark_delivered(row["id"], "post-1")
        self.assertIsNone(why)
        self.assertEqual(seen["delivery"], "observed")
        self.assertEqual(seen["status"], "open")
        self.assertEqual([r["id"] for r in dispatches.open_rows()], [row["id"]])
        verdict, why = dispatches.mark_verdict(row["id"], self.a, "review-post-9")
        self.assertIsNone(why)
        self.assertEqual(verdict["reviewed_tip"], self.a)
        self.assertEqual(dispatches.open_rows(), [])

    def test_verdict_is_terminal_but_identical_retry_is_idempotent(self):
        row = self.add()
        first, why = dispatches.mark_verdict(row["id"], self.a, "safe")
        self.assertIsNone(why)
        again, why = dispatches.mark_verdict(row["id"], self.a, "safe")
        self.assertIsNone(why)
        self.assertEqual(again, first)
        self.assertEqual(len(dispatches.history(row["id"])), 2)
        _row, why = dispatches.mark_verdict(row["id"], self.a, "different")
        self.assertIn("already has a verdict", why)

    def test_every_new_row_requires_an_exact_ref_so_it_is_closable(self):
        self.assertIsNone(dispatches.add("seat", "lane", repo=self.repo))
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["add", "seat", "lane", "--repo", self.repo])
        self.assertEqual(rc, 2)
        self.assertIn("requires --ref TIP", err)

    def test_bad_identity_metadata_and_deadlines_are_refused(self):
        for recipient in ("", "team a", "seat\nINJECT", "x" * 65):
            self.assertIsNone(dispatches.add(recipient, "lane"))
        self.assertIsNone(dispatches.add("seat", "lane\nINJECT"))
        self.assertIsNone(dispatches.add("seat", "lane", deadline_s=0))
        self.assertIsNone(dispatches.add("seat", "lane", deadline_s=-1))
        self.assertIsNone(dispatches.add("seat", "lane", deadline_s=10**9))


class AtomicSendTest(DispatchBase):
    def test_send_is_one_first_class_handoff_and_retry_never_resends(self):
        row, why, posted = dispatches.send(
            "codex-3", "review", "Review this tip", self.a, repo=self.repo,
            key="review-1", sign=False)
        self.assertIsNone(why)
        self.assertTrue(posted)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["delivery"], "observed")
        again, why, posted = dispatches.send(
            "codex-3", "review", "Review this tip", self.a, repo=self.repo,
            key="review-1", sign=False)
        self.assertIn("do not resend", why)
        self.assertFalse(posted)
        self.assertEqual(again["id"], row["id"])
        self.assertEqual(len(dispatches.rows()), 1)
        dm_rows, _ = seats.chat.read(seats.dm_lane("codex-3"))
        self.assertEqual([r["id"] for r in dm_rows], [row["delivery_ref"]])

    def test_ledger_stage_failure_rolls_back_before_any_delivery(self):
        with mock.patch.object(eventledger, "append_unlocked", return_value=False), \
                mock.patch.object(seats, "dm") as dm:
            row, why, posted = dispatches.send(
                "codex-3", "review", "Do it", self.a, repo=self.repo,
                key="stage-fail", sign=False)
        self.assertIsNone(row)
        self.assertIn("NOT recorded", why)
        self.assertFalse(posted)
        dm.assert_not_called()
        self.assertEqual(dispatches.rows(), {})

    def test_failed_delivery_is_needs_confirmation_and_never_auto_resends(self):
        with mock.patch.object(seats, "dm", return_value=(None, "recipient down")):
            failed, why, posted = dispatches.send(
                "codex-3", "review", "Do it", self.a, repo=self.repo,
                key="delivery-fail", sign=False)
        self.assertFalse(posted)
        self.assertIn("NEEDS CONFIRMATION", why)
        self.assertIn("recipient down", why)
        self.assertEqual(failed["status"], "open")
        self.assertEqual(failed["delivery"], "needs-confirmation")
        fp, text = seats._dispatch_candidate()
        self.assertIn(failed["id"], fp)
        self.assertIn("NEEDS CONFIRMATION", text)
        self.assertIn("do NOT resend", text)
        # The retry runs UNMOCKED: if the never-resend guard failed, a real DM
        # would land in the lane and the last assertion would catch it.
        again, why, posted = dispatches.send(
            "codex-3", "review", "Do it", self.a, repo=self.repo,
            key="delivery-fail", sign=False)
        self.assertFalse(posted)
        self.assertIn("do not resend", why)
        self.assertEqual(again["id"], failed["id"])
        self.assertEqual(len(dispatches.rows()), 1)
        self.assertEqual(seats.chat.read(seats.dm_lane("codex-3"))[0], [])

    def test_crash_window_after_dm_stays_ambiguous_without_duplicate_message(self):
        real = eventledger.append_unlocked

        def fail_activation(path, row):
            if row.get("event") == "delivered":
                return False
            return real(path, row)

        with mock.patch.object(eventledger, "append_unlocked", side_effect=fail_activation):
            staged, why, posted = dispatches.send(
                "codex-3", "review", "Do it", self.a, repo=self.repo,
                key="activation-fail", sign=False)
        self.assertFalse(posted)
        self.assertIn("NEEDS CONFIRMATION", why)
        self.assertEqual(staged["status"], "open")
        self.assertEqual(staged["delivery"], "needs-confirmation")
        again, why, posted = dispatches.send(
            "codex-3", "review", "Do it", self.a, repo=self.repo,
            key="activation-fail", sign=False)
        self.assertFalse(posted)
        self.assertIn("do not resend", why)
        self.assertEqual(again["id"], staged["id"])
        self.assertEqual(len(seats.chat.read(seats.dm_lane("codex-3"))[0]), 1)

    def test_auto_key_uses_canonical_recipient_and_exact_tip(self):
        first, why, posted = dispatches.send(
            "@codex-3", "review", "same work", self.a[:10], repo=self.repo,
            sign=False)
        self.assertIsNone(why)
        self.assertTrue(posted)
        again, why, posted = dispatches.send(
            "codex-3", "review", "same work", self.a, repo=self.repo,
            sign=False)
        self.assertIn("do not resend", why)
        self.assertFalse(posted)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(dispatches.rows()), 1)

    def test_same_key_cannot_alias_different_payload(self):
        dispatches.send("codex-3", "review", "one", self.a, repo=self.repo,
                        key="same", sign=False)
        row, why, posted = dispatches.send(
            "codex-3", "review", "two", self.a, repo=self.repo,
            key="same", sign=False)
        self.assertIsNone(row)
        self.assertIn("different work", why)
        self.assertFalse(posted)

    def test_key_namespace_and_semantic_collision_cover_sender_repo_and_metadata(self):
        first, why, _ = dispatches.send(
            "codex-3", "review", "one", self.a, repo=self.repo, key="shared",
            note="first", deadline_s=60, sign=False)
        self.assertIsNone(why)
        clash, why, _ = dispatches.send(
            "codex-3", "review", "one", self.a, repo=self.repo, key="shared",
            note="changed", deadline_s=120, sign=False)
        self.assertIsNone(clash)
        self.assertIn("different work", why)
        os.environ["HELM_CHAT_NAME"] = "other-integrator"
        other, why, _ = dispatches.send(
            "codex-3", "review", "one", self.a, repo=self.repo, key="shared",
            note="first", deadline_s=60, sign=False)
        self.assertIsNone(why)
        self.assertNotEqual(other["id"], first["id"])
        os.environ["HELM_CHAT_NAME"] = "integrator"
        clone = os.path.join(self.tmp, "clone")
        subprocess.run(["git", "clone", "-q", self.repo, clone], check=True)
        separate, why, _ = dispatches.send(
            "codex-3", "review", "one", self.a, repo=clone, key="shared",
            note="first", deadline_s=60, sign=False)
        self.assertIsNone(why)
        self.assertNotEqual(separate["id"], first["id"])

    def test_sender_identity_is_an_exact_token_not_an_output_injection(self):
        os.environ["HELM_CHAT_NAME"] = "bad\nseat"
        row, why, posted = dispatches.send(
            "codex-3", "review", "one", self.a, repo=self.repo, sign=False)
        self.assertIsNone(row)
        self.assertIn("sender", why)
        self.assertFalse(posted)

    def test_exact_token_recipient_is_preserved(self):
        seats.write_roster("team.a")
        seats.write_roster("team-a")
        one, why, _ = dispatches.send("team.a", "x", "one", self.a,
                                      repo=self.repo, key="dot", sign=False)
        two, why2, _ = dispatches.send("team-a", "x", "two", self.a,
                                       repo=self.repo, key="dash", sign=False)
        self.assertIsNone(why)
        self.assertIsNone(why2)
        self.assertNotEqual(one["recipient"], two["recipient"])
        self.assertNotEqual(one["delivery_ref"], two["delivery_ref"])


class HistoricalCompatTest(DispatchBase):
    """The shipping surface is dispatch/delivered/verdict only. Rows the old
    schemas already wrote keep replaying truthfully — never rebound, never
    silently dropped — and the removed verbs stay removed."""

    def test_already_written_retarget_rows_still_replay_for_legacy_opens(self):
        ts = dispatches.pk.now_ts()
        legacy = {"id": "ce1e7dd0", "ts": ts, "recipient": "codex-3",
                  "lane": "legacy", "ref": self.a[:7], "note": None,
                  "deadline_s": 60, "source": "old", "status": "open",
                  "ack_ref": None, "verdict_ref": None, "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), legacy))
        move = {"id": "ce1e7dd0", "event": "retarget", "tip": self.b,
                "ref": self.b, "ts": dispatches.pk.now_ts()}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), move))
        got = dispatches.rows()["ce1e7dd0"]
        self.assertEqual(got["tip"], self.b)
        self.assertIsNone(got["migration"])
        stale, why = dispatches.mark_verdict("ce1e7dd0", self.a, "reviewed-a")
        self.assertIsNone(stale)
        self.assertIn("stale verdict", why)
        closed, why = dispatches.mark_verdict("ce1e7dd0", self.b, "reviewed-b")
        self.assertIsNone(why)
        self.assertEqual(closed["reviewed_tip"], self.b)

    def test_ambiguous_and_foreign_refs_are_refused_at_dispatch_time(self):
        self.git("branch", "dup", self.b)
        self.git("tag", "dup", self.a)
        self.assertIsNone(dispatches.add("codex-3", "lane", ref="dup",
                                         repo=self.repo))
        foreign = os.path.join(self.tmp, "foreign")
        os.makedirs(foreign)
        subprocess.run(["git", "-C", foreign, "init", "-q"], check=True)
        self.assertIsNone(dispatches.add("codex-3", "lane", ref=self.c,
                                         repo=foreign))
        self.assertEqual(dispatches.rows(), {})

    def test_concurrent_conflicting_verdicts_close_exactly_once(self):
        row = self.add(ref=self.a)
        barrier = threading.Barrier(3)
        out = []

        def close(evidence):
            barrier.wait()
            out.append(dispatches.mark_verdict(row["id"], self.a, evidence))

        threads = [threading.Thread(target=close, args=("review-1",)),
                   threading.Thread(target=close, args=("review-2",))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        winners = [got for got, why in out if why is None]
        losers = [why for got, why in out if why is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn("already has a verdict", losers[0])
        final = dispatches.rows()[row["id"]]
        self.assertEqual(final["status"], "verdict")
        self.assertEqual(final["verdict_ref"], winners[0]["verdict_ref"])

    def test_v2_ref_less_row_surfaces_needs_redispatch_and_never_binds(self):
        ts = dispatches.pk.now_ts()
        row = {"v": 2, "id": "0123456789abcdef0123456789abcdef", "seq": 0,
               "event": "add", "ts": ts, "recipient": "codex-3",
               "lane": "legacy-v2-unbound", "ref": None, "tip": None,
               "original_ref": None, "original_tip": None, "note": None,
               "deadline_s": 60, "source": "old-v2", "repo": None,
               "repo_id": None, "dispatch_key": None, "message_id": None,
               "message_hash": None, "sender": None, "status": "pending",
               "ack_ref": None, "verdict_ref": None, "reviewed_tip": None,
               "delivery_ref": None, "delivery_error": None,
               "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        got = dispatches.rows()[row["id"]]
        self.assertEqual(got["migration"], "needs-redispatch")
        fp, text = seats._dispatch_candidate()
        self.assertIn("needs-redispatch", fp)
        self.assertIn("NEEDS REDISPATCH", text)
        rc, _out, _err = run(dispatches.cmd_dispatch,
                             ["bind", row["id"], self.a])
        self.assertEqual(rc, 2)                    # bind is not a verb
        blocked, why = dispatches.mark_verdict(row["id"], self.a, "safe")
        self.assertIsNone(blocked)
        self.assertIn("redispatch", why)
        self.assertIn(row["id"], [r["id"] for r in dispatches.open_rows()])

    def test_validated_v1_no_seq_transitions_preserve_old_closures(self):
        ts = dispatches.pk.now_ts()
        base = {"id": "1a2b3c4d", "ts": ts, "recipient": "codex-3",
                "lane": "legacy-close", "ref": self.a[:7], "note": None,
                "deadline_s": 60, "source": "old", "status": "open",
                "ack_ref": None, "verdict_ref": None, "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), base))
        acked = dict(base, status="acked", ack_ref="post-1",
                     last_updated=dispatches.pk.now_ts())
        self.assertTrue(eventledger.append(dispatches.ledger_path(), acked))
        closed = dict(acked, status="verdict", verdict_ref="safe",
                      last_updated=dispatches.pk.now_ts())
        self.assertTrue(eventledger.append(dispatches.ledger_path(), closed))
        got = dispatches.rows()[base["id"]]
        self.assertEqual(got["status"], "verdict")
        self.assertEqual(got["verdict_ref"], "safe")
        self.assertNotIn(base["id"], [r["id"] for r in dispatches.open_rows()])

    def test_legacy_short_and_symbolic_refs_are_never_rebound_at_replay(self):
        # Both refs RESOLVE in this repo right now — replay must still refuse
        # to adopt them: resolution happened at write time or not at all.
        ts = dispatches.pk.now_ts()
        for rid, ref in (("ce1e7dd0", self.a[:7]), ("deadc0de", self.main)):
            legacy = {"id": rid, "ts": ts, "recipient": "codex-3",
                      "lane": "legacy-" + rid, "ref": ref, "note": None,
                      "deadline_s": 60, "source": "old", "status": "open",
                      "ack_ref": None, "verdict_ref": None, "last_updated": ts}
            self.assertTrue(eventledger.append(dispatches.ledger_path(), legacy))
            got = dispatches.rows()[rid]
            self.assertIsNone(got["tip"])
            self.assertEqual(got["migration"], "needs-redispatch")
            blocked, why = dispatches.mark_verdict(rid, self.a, "evidence")
            self.assertIsNone(blocked)
            self.assertIn("redispatch", why)


class OverdueWhisperTest(DispatchBase):
    def test_deadline_is_advisory_and_delivered_still_goes_overdue(self):
        row = self.add(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-9")
        self.age(row["id"], 3600)
        self.assertEqual([r["id"] for r in dispatches.overdue()], [row["id"]])
        fp, text = seats._dispatch_candidate()
        self.assertIn(row["id"], fp)
        self.assertIn("PENDING VERDICT", text)
        self.assertIn("NEEDS CHECK-IN", text)
        self.assertIn("do NOT", text)

    def test_needs_confirmation_outranks_an_overdue_check_in(self):
        old = self.add(deadline_s=60)
        dispatches._mark_delivered(old["id"], "post-1")
        self.age(old["id"], 3600)
        with mock.patch.object(seats, "dm", return_value=(None, "down")):
            failed, why, posted = dispatches.send(
                "codex-4", "new", "deliver", self.a, repo=self.repo,
                key="confirm-first", sign=False)
        self.assertFalse(posted)
        self.assertIn("NEEDS CONFIRMATION", why)
        fp, text = seats._dispatch_candidate()
        self.assertIn(failed["id"], fp)
        self.assertNotIn(old["id"], fp)
        self.assertIn("NEEDS CONFIRMATION", text)
        self.assertIn("do NOT", text)

    def test_unavailable_ledger_is_unknown_not_silent_zero(self):
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")):
            rc, _out, err = run(dispatches.cmd_dispatch, ["list"])
            self.assertEqual(rc, 1)
            self.assertIn("obligations UNKNOWN", err)
            fp, text = seats._dispatch_candidate()
            self.assertEqual(fp, "dispatch:ledger-unavailable")
            self.assertIn("UNAVAILABLE", text)
            self.assertIn("UNKNOWN", text)

    def test_stop_whisper_walks_confirm_overdue_then_verdict_silences_it(self):
        row = self.add(deadline_s=60)
        fp, text = seats._dispatch_candidate()   # fresh add: delivery unproven
        self.assertIn("needs-confirmation", fp)
        self.assertIn("NEEDS CONFIRMATION", text)
        _seen, why = dispatches._mark_delivered(row["id"], "post-1")
        self.assertIsNone(why)
        self.assertIsNone(seats._dispatch_candidate())   # observed + young
        self.age(row["id"], 3600)
        self.assertIsNotNone(seats._dispatch_candidate())  # reloads from disk
        dispatches.mark_verdict(row["id"], self.a, "safe")
        self.assertIsNone(seats._dispatch_candidate())

    def test_utc_calendar_malformed_and_future_clock_semantics(self):
        row = self.add(deadline_s=60)
        self.assertLess(dispatches._age_s(row), 30)
        self.assertEqual(dispatches._age_s({"ts": "not-a-time"}), 0)
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(time.time() + 3600))
        self.assertEqual(dispatches._age_s({"ts": future}), 0)

    def test_list_spells_pending_and_needs_without_auto_reassign(self):
        row = self.add(deadline_s=60)
        self.age(row["id"], 3600)
        rc, out, err = run(dispatches.cmd_dispatch, ["list", "--overdue"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("PENDING VERDICT", out)
        self.assertIn("NEEDS CHECK-IN", out)
        self.assertIn("do not reassign", out)


class StorageSafetyTest(DispatchBase):
    def test_corrupt_duplicate_and_truncated_tail_cannot_erase_good_row(self):
        row = self.add()
        path = dispatches.ledger_path()
        with open(path, "ab") as f:
            f.write(b'not-json\n')
            bad = dict(row, seq=0, lane="collision", status="verdict")
            f.write((json.dumps(bad) + "\n").encode())
            f.write(b'{"id":"' + row["id"].encode() + b'","status":"verdict"}')
        got = dispatches.rows()[row["id"]]
        self.assertEqual(got["lane"], row["lane"])
        self.assertEqual(got["status"], "open")

    def test_next_append_repairs_only_truncated_tail_and_remains_replayable(self):
        first = self.add(lane="first")
        with open(dispatches.ledger_path(), "ab") as f:
            f.write(b'{"id":"torn","status":"pending"')
        second = self.add(lane="second")
        got = dispatches.rows()
        self.assertEqual(set(got), {first["id"], second["id"]})
        with open(dispatches.ledger_path(), "rb") as f:
            self.assertTrue(f.read().endswith(b"\n"))

    def test_well_formed_fake_close_event_cannot_erase_pending_obligation(self):
        row = self.add()
        fake = dict(row, seq=1, event="ack", status="verdict",
                    verdict_ref="forged", reviewed_tip=self.a)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), fake))
        got = dispatches.rows()[row["id"]]
        # The forged row stays VISIBLE in history (append-only ledger) but a
        # v3 obligation only closes on a strict verdict event naming its tip.
        self.assertEqual(got["status"], "open")
        self.assertEqual(len(dispatches.history(row["id"])), 2)

    def test_well_shaped_ack_verdict_and_retarget_cannot_rewrite_tip(self):
        row = self.add(ref=self.b)
        forged_ack = dict(row, seq=1, event="ack", status="acked",
                          ack_ref="post", ref=self.c, tip=self.c)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged_ack))
        forged_verdict = dict(row, seq=1, event="verdict", status="verdict",
                              ref=self.c, tip=self.c, reviewed_tip=self.c,
                              verdict_ref="forged")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged_verdict))
        forged_move = dict(row, seq=1, event="retarget", ref=self.side,
                           tip=self.side, retarget_from=self.b,
                           retarget_to=self.side)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged_move))
        got = dispatches.rows()[row["id"]]
        self.assertEqual(got["tip"], self.b)
        self.assertEqual(got["status"], "open")
        self.assertEqual(len(dispatches.history(row["id"])), 4)

    def test_symlink_ledger_is_refused_without_touching_target(self):
        os.makedirs(os.path.dirname(dispatches.ledger_path()), exist_ok=True)
        victim = os.path.join(self.tmp, "victim")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("safe")
        os.symlink(victim, dispatches.ledger_path())
        self.assertIsNone(dispatches.add("codex-3", "lane", ref=self.a, repo=self.repo))
        with open(victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "safe")
        self.assertEqual(dispatches.rows(), {})

    def test_hardlinked_ledger_is_refused_without_touching_target(self):
        os.makedirs(os.path.dirname(dispatches.ledger_path()), exist_ok=True)
        victim = os.path.join(self.tmp, "hardlink-victim")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("safe")
        os.link(victim, dispatches.ledger_path())
        self.assertIsNone(dispatches.add("codex-3", "lane", ref=self.a,
                                         repo=self.repo))
        with open(victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "safe")
        _rows, unavailable = dispatches.snapshot()
        self.assertIn("private regular file", unavailable)
        rc, _out, err = run(dispatches.cmd_dispatch, ["list"])
        self.assertEqual(rc, 1)
        self.assertIn("obligations UNKNOWN", err)

    def test_symlinked_home_parent_is_refused_before_ledger_creation(self):
        target = os.path.join(self.tmp, "redirect-target")
        os.makedirs(target)
        link = os.path.join(self.tmp, "redirect-home")
        os.symlink(target, link)
        os.environ["HELM_HOME"] = link
        self.assertIsNone(dispatches.add("codex-3", "lane", ref=self.a, repo=self.repo))
        self.assertFalse(os.path.exists(os.path.join(target, "_global")))
        self.assertEqual(dispatches.rows(), {})

    def test_first_creation_fsyncs_parent_and_ledger_directory_entries(self):
        real = eventledger._fsync_dir
        with mock.patch.object(eventledger, "_fsync_dir", wraps=real) as sync:
            row = self.add()
        self.assertIsNotNone(row)
        synced = [os.path.realpath(call.args[0]) for call in sync.call_args_list]
        self.assertIn(os.path.realpath(os.path.dirname(dispatches.ledger_path())),
                      synced)
        self.assertGreaterEqual(len(synced), 2)

    def test_short_write_rolls_back_and_file_permissions_are_private(self):
        path = dispatches.ledger_path()
        real_write = os.write

        def partial(fd, payload):
            return real_write(fd, payload[:len(payload) // 2])

        with mock.patch.object(os, "write", side_effect=partial):
            self.assertFalse(eventledger.append(path, {"id": "deadbeef"}))
        self.assertEqual(os.path.getsize(path), 0)
        row = self.add()
        self.assertIn(row["id"], dispatches.rows())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path + ".lock").st_mode), 0o600)

    def test_concurrent_adds_are_all_replayable(self):
        made = []
        barrier = threading.Barrier(25)

        def add_one(i):
            barrier.wait()
            made.append(dispatches.add("seat-%d" % i, "lane", ref=self.a,
                                         repo=self.repo))

        threads = [threading.Thread(target=add_one, args=(i,)) for i in range(24)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertTrue(all(made))
        self.assertEqual(len(dispatches.rows()), 24)

    def test_large_replay_keeps_stop_probe_bounded(self):
        path = dispatches.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        now = dispatches.pk.now_ts()
        info = dispatches._repo_info(self.repo)
        with open(path, "w", encoding="utf-8") as f:
            for i in range(5000):
                row = {"v": 2, "id": "%032x" % i, "seq": 0, "event": "add",
                       "ts": now, "recipient": "seat", "lane": "lane",
                       "ref": self.a, "tip": self.a, "original_ref": self.a,
                       "original_tip": self.a, "note": None, "deadline_s": 2700,
                       "source": "scale", "repo": info["repo"],
                       "repo_id": info["repo_id"],
                       "dispatch_key": None, "message_id": None,
                       "message_hash": None, "sender": None, "status": "pending",
                       "ack_ref": None, "verdict_ref": None,
                       "reviewed_tip": None, "delivery_ref": "post-%d" % i,
                       "delivery_error": None, "last_updated": now}
                f.write(json.dumps(row) + "\n")
        started = time.monotonic()
        self.assertIsNone(seats._dispatch_candidate())
        self.assertLess(time.monotonic() - started, 1.5)


class CmdTest(DispatchBase):
    def test_cli_send_verdict_round_trip(self):
        rc, out, err = run(dispatches.cmd_dispatch, [
            "send", "codex-3", "review", "review", "this", "--ref", self.a,
            "--repo", self.repo, "--key", "cli-review", "--deadline", "900"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("PENDING VERDICT", out)
        rid = next(iter(dispatches.rows()))
        rc, out, err = run(dispatches.cmd_dispatch,
                           ["verdict", rid, self.a, "safe"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("VERDICT", out)

    def test_bad_usage_is_rc2_without_traceback(self):
        cases = ([], ["add"], ["send", "s", "l", "message"], ["bogus"],
                 ["ack", "id"], ["bind", "id"], ["retarget", "id", "old"],
                 ["verdict", "id", "tip"], ["list", "--wat"])
        for args in cases:
            rc, out, err = run(dispatches.cmd_dispatch, args)
            self.assertEqual(rc, 2, args)
            self.assertNotIn("Traceback", out + err)

    def test_cli_verb_is_wired(self):
        from helm import cli
        self.assertIn("dispatch", cli.VERBS)


class LedgerSeparationTest(DispatchBase):
    def test_dispatches_and_ownerasks_share_mechanics_not_files_or_closers(self):
        from helm import ownerasks
        self.add()
        ownerasks.add("an owner ask")
        self.assertNotEqual(dispatches.ledger_path(), ownerasks.ledger_path())
        self.assertEqual(len(dispatches.rows()), 1)
        self.assertEqual(len(ownerasks.rows()), 1)
        self.assertIs(dispatches.eventledger, ownerasks.eventledger)


if __name__ == "__main__":
    unittest.main()
