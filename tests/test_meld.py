#!/usr/bin/env python3
"""helm meld — the mindmeld preset. Hermetic: HELM_CHAT_DIR + HELM_HOME are
tmp dirs, HELM_CHAT_NODE_URL set-but-empty kills the signed transport,
ambient session ids scrubbed (test_seats.py's exact envelope). Lineage pins:
the predecessor meld script's scars (epoch fence, F1 control echoes, F2 fail-closed self-skip,
room×actor state, clip-proof invite head) + the helm-native laws (act-moment
mentions only, latency-pure sign=False, bounds as behavior)."""
import contextlib
import io
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import council
from helm import chat, meld, pk, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_OWNER_NAMES",
            "HELM_CHAT_DELIVER", "HELM_MELD_CAP", "HELM_MELD_RECV_TIMEOUT_S",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL")


class MeldBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-meld-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open_meld(self):
        """invite(a→b) + join(b) → (room, epoch). The standard fixture."""
        room, _ = meld.invite("seat-b", "converge the wire format", seat="seat-a")
        meld.join(room, seat="seat-b")
        return room, meld.state(room, "seat-a")["epoch"]


class TestInvite(MeldBase):
    def test_invite_seeds_problem_then_clip_proof_invite(self):
        room, lines = meld.invite("@seat-b", "converge the wire format",
                                  seat="seat-a")
        rows, total = chat.read(room)
        self.assertEqual(total, 2)
        seed, inv = rows[0]["text"], rows[1]["text"]
        self.assertIn("PROBLEM: converge the wire format", seed)
        self.assertTrue(seed.endswith("[HOLD]"))
        self.assertIn("MELD DISCIPLINE", seed)         # discipline rides the seed
        # clip-proof head (the predecessor harness's #115, inverted): the join command must sit
        # inside the first 200 BYTES — the delivery clip can never eat it.
        head = seats._clip(seats._scrub(inv))
        self.assertIn("helm chat meld join %s" % room, head)
        self.assertTrue(inv.startswith("@seat-b "))
        st = meld.state(room, "seat-a")
        self.assertEqual((st["role"], st["status"], st["exchanges"]),
                         ("convener", "invited", 0))
        self.assertIn(room, chat.list_rooms())          # owner surface: the
        # room shows in rooms/web channel list the moment it exists

    def test_invite_refuses_empty_problem_before_creating_room(self):  # noqa: VACUOUS_ASSERTION — valid invite controls the refusal loop
        for topic in ("", "   ", "\t\n"):
            with self.assertRaises(SystemExit) as cm:
                meld.invite("seat-b", topic, seat="seat-a")
            self.assertIn("MELD-EMPTY-PROBLEM", str(cm.exception))
            self.assertFalse(chat.list_rooms())
        # Positive control on the same room observable: one non-empty problem
        # creates exactly the normal meld room.
        room, _ = meld.invite("seat-b", "converge", seat="seat-a")
        self.assertIn(room, chat.list_rooms())

    def test_invite_refuses_topic_addressing_nonmember(self):
        with self.assertRaises(SystemExit) as cm:
            meld.invite("seat-b", "@seat-c verifies the shape", seat="seat-a")
        self.assertIn("MEMBERSHIP-MISMATCH", str(cm.exception))
        self.assertIn("seat-c", str(cm.exception))
        self.assertFalse(chat.list_rooms())
        # Positive control on the same observable: a plain-name REFERENCE does
        # not address seat-c and therefore creates the room normally.
        room, _ = meld.invite("seat-b", "seat-c verifies the shape", seat="seat-a")
        self.assertIn(room, chat.list_rooms())

    def test_invite_delivers_to_tracked_peer(self):
        """The wake layer: a tracked seat (joined main) meets the meld room
        cursor-less → backfill from 0 → the invite @mention delivers."""
        seats.join(session="sid-b", cwd=self.tmp, seat="seat-b")
        room, _ = meld.invite("seat-b", "converge the wire format",
                              seat="seat-a")
        line = seats.deliver_any(session="sid-b", seat="seat-b")
        self.assertIsNotNone(line)
        self.assertIn("#%s" % room, line)
        self.assertIn("MELD-INVITE", line)

    def test_meld_posts_ride_unsigned(self):
        """Latency purity: every meld post passes sign=False — the signing
        leg (a node round-trip) must never fire mid-meld."""
        signs = []
        real = chat._signed_row

        def spy(row, text, profile, sign):
            signs.append(sign)
            return real(row, text, profile, sign)

        with mock.patch.object(chat, "_signed_row", side_effect=spy):
            room, _ = meld.invite("seat-b", "topic x", seat="seat-a")
            meld.join(room, seat="seat-b")
            meld.say(room, "YIELD", "chunk", seat="seat-b")
        self.assertTrue(signs and all(s is False for s in signs))


class TestJoinReady(MeldBase):
    def test_join_posts_control_only_ready_that_wakes_convener(self):
        room, _ = meld.invite("seat-b", "topic x", seat="seat-a")
        meld.join(room, seat="seat-b")
        ready = chat.read(room)[0][-1]
        self.assertIn("READY:", ready["text"])
        self.assertIsNone(meld._MARKER_RE.search(ready["text"]))  # control-only
        self.assertTrue(seats.deliverable(ready, "seat-a"))       # wake-back:
        # a READY that lands silently strands GO forever (a predecessor-harness live incident)
        st = meld.state(room, "seat-b")
        self.assertEqual((st["role"], st["status"], st["peer"]),
                         ("joiner", "active", "seat-a"))

    def test_join_without_seed_refuses(self):
        chat.post("just chatter", room="meld-1-x", who="someone")
        with self.assertRaises(SystemExit):
            meld.join("meld-1-x", seat="seat-b")

    def test_state_is_room_x_actor(self):
        """The predecessor meld's live-dogfood REFUTE: both actors share one chat dir;
        the joiner's state write must never clobber the convener's role."""
        room, _ = self.open_meld()
        self.assertNotEqual(meld.state_path(room, "seat-a"),
                            meld.state_path(room, "seat-b"))
        self.assertEqual(meld.state(room, "seat-a")["role"], "convener")
        self.assertEqual(meld.state(room, "seat-b")["role"], "joiner")


class TestRecv(MeldBase):
    def test_convener_recv_returns_once_on_ready_then_active(self):
        room, _ = self.open_meld()
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 0)
        self.assertIn("READY", lines[0])
        self.assertEqual(meld.state(room, "seat-a")["status"], "active")
        # a second READY row is a control echo now (F1) — skipped, timeout
        chat.post("[MELD e:%d] READY:%d (again)" %
                  ((meld.state(room, "seat-a")["epoch"],) * 2),
                  room=room, who="seat-b", sign=False)
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, meld.EXIT_BOUND)

    def test_joiner_first_chunk_is_the_seed(self):
        room, epoch = self.open_meld()
        code, lines = meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        self.assertEqual(code, 0)
        self.assertIn("PROBLEM:", lines[0])
        self.assertIn("PEER'S", lines[1])              # [HOLD] → floor stays

    def test_recv_skips_own_stale_epoch_markerless_and_unattributed(self):
        room, epoch = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)   # consume READY
        chat.post("[MELD e:999999] ghost [YIELD]", room=room, who="seat-b",
                  sign=False)                                   # stale epoch
        chat.post("markerless chatter", room=room, who="seat-b", sign=False)
        chat.post("[MELD e:%d] own row [YIELD]" % epoch, room=room,
                  who="seat-a", sign=False)                     # own (F2)
        chat._append({"ts": pk.now_ts(), "from": "",                # crafted:
                      "text": "[MELD e:%d] anon [YIELD]" % epoch},  # post()
                     room)                # would name it — unattributable (F2)
        chat.post("[MELD e:%d] the real chunk [YIELD]" % epoch, room=room,
                  who="seat-b", sign=False)
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 0)
        self.assertIn("the real chunk", lines[0])
        self.assertEqual(meld.state(room, "seat-a")["exchanges"], 1)

    def test_member_chunk_addressing_nonmember_aborts_instead_of_converging(self):
        """A pinned member cannot assign a load-bearing role outside the pin.

        The plain room write is the bypass that live-fired: the convener named
        @seat-c in a YIELD chunk although only seat-b was invited. Outsider rows
        still cannot kill the meld; this is an accepted MEMBER's own chunk.
        """
        room, epoch = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)  # seed [HOLD]
        chat.post("[MELD e:%d] @seat-c verify the shape [YIELD]" % epoch,
                  room=room, who="seat-a", sign=False)
        code, lines = meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        self.assertEqual(code, meld.EXIT_ABORT)
        self.assertIn("MEMBERSHIP-MISMATCH", "\n".join(lines))
        self.assertIn("seat-c", "\n".join(lines))
        self.assertEqual(meld.state(room, "seat-b")["status"], "aborted")
        events, unavailable = meld._events(meld.lifecycle_path(room))
        self.assertIsNone(unavailable)
        self.assertEqual(events[-1]["transition"], "peer-membership-mismatch")

    def test_recv_bounds_are_behavior(self):
        room, epoch = self.open_meld()
        st = meld.state(room, "seat-a")
        st["exchanges"], st["status"] = st["cap"], "active"
        meld._write_state(room, "seat-a", st)
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, meld.EXIT_BOUND)                 # cap
        self.assertIn("meld say %s --marker DONE" % room, "\n".join(lines))
        st["exchanges"] = 0
        meld._write_state(room, "seat-a", st)
        code, _ = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, meld.EXIT_BOUND)                 # timeout

    def test_a_timeout_shorter_than_one_poll_is_honoured(self):  # noqa: VACUOUS_ASSERTION — the observable is the elapsed wall, asserted POSITIVELY from both sides (>= the timeout, < 1 s), beside the EXIT_BOUND code
        """The bound is the wait, not the first poll after it: the loop checked
        its deadline and then slept a whole poll, so a 0.05 s recv on a 2 s
        cadence returned after 2 s."""
        room, _ = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)    # READY
        t0 = time.time()
        code, _ = meld.recv(room, timeout=0.05, seat="seat-a", poll=2.0)
        elapsed = time.time() - t0
        self.assertEqual(code, meld.EXIT_BOUND)
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertLess(elapsed, 1.0)

    def test_timeout_before_any_join_says_nojoin_not_window_over(self):  # noqa: VACUOUS_ASSERTION — the second half is the unconditional control: the SAME seat, room and verb, asserted to emit the window-over line once the meld has actually run.
        """task/2445, reported by a seat USING helm: recv told the convener
        "the synchronous window is over" while the codex peer was still
        joining. There was no window — not one chunk had arrived — and the
        instruction it gave closes a meld that never opened.

        BOTH ARMS ON THE SAME OBSERVABLE, one call each, because "it does not
        say window-over" is worthless without proof that this probe CAN see the
        window-over line: the second arm is that control, on the same room and
        the same verb, and it is asserted unconditionally.
        """
        room, _ = meld.invite("seat-b", "converge the wire format",
                              seat="seat-a")      # invited; seat-b never joins
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        out = "\n".join(lines)
        self.assertEqual(code, meld.EXIT_BOUND)
        self.assertIn("MELD-NOJOIN", out)
        self.assertIn("seat-b", out)              # NAMES who is still out
        self.assertNotIn("the synchronous window is over", out)
        self.assertIn("recv %s" % room, out)      # the true next action

        # THE CONTROL: the same seat, the same room, once the meld has actually
        # run — now the window really did close and the old line is correct.
        meld.join(room, seat="seat-b")
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)       # READY
        meld.say(room, "YIELD", "my half", seat="seat-b")
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)       # the chunk
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        out = "\n".join(lines)
        self.assertEqual(code, meld.EXIT_BOUND)
        self.assertIn("the synchronous window is over", out)
        self.assertNotIn("MELD-NOJOIN", out)

    def test_nojoin_names_only_the_peers_still_out_in_a_standup(self):  # noqa: VACUOUS_ASSERTION — `assertIn("seat-c", out)` is the unconditional control on the same emitted string, so an empty or missing line fails before the absence arm is reached.
        """A 2+ standup: one peer in, one still out. Every READY is recorded
        now, not only the one that flips the convener active — before that the
        later joins were skipped as control echoes, so the state could not tell
        a half-joined standup from an empty room."""
        room, _ = meld.invite("seat-b,seat-c", "converge the wire format",
                              seat="seat-a")
        meld.join(room, seat="seat-b")
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)       # b's READY
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        out = "\n".join(lines)
        self.assertEqual(code, meld.EXIT_BOUND)
        self.assertIn("MELD-NOJOIN", out)
        self.assertIn("seat-c", out)
        self.assertNotIn("not-joined=seat-b", out)   # b is in; do not chase it

    def test_abort_is_fail_loud(self):
        room, epoch = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)    # READY
        meld.say(room, "ABORT", "wrong premise", seat="seat-b")
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, meld.EXIT_ABORT)
        self.assertEqual(meld.state(room, "seat-a")["status"], "aborted")
        code, _ = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 2)                               # dead is dead

    def test_no_state_refuses(self):
        code, _ = meld.recv("meld-1-none", timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 2)


class TestSay(MeldBase):
    def test_act_moment_mentions_only(self):
        """YIELD/HOLD chunks never pollute the peer's delivery cursor;
        DONE/ABORT must land (the closing wake)."""
        room, epoch = self.open_meld()
        meld.say(room, "YIELD", "mid-meld chunk", seat="seat-a")
        meld.say(room, "HOLD", "more coming", seat="seat-a")
        rows = chat.read(room)[0]
        y, h = rows[-2], rows[-1]
        self.assertFalse(seats.deliverable(y, "seat-b"))
        self.assertFalse(seats.deliverable(h, "seat-b"))
        meld.say(room, "DONE", "state: agreed; next: b lands it", seat="seat-a")
        done = chat.read(room)[0][-1]
        self.assertTrue(seats.deliverable(done, "seat-b"))
        self.assertTrue(done["text"].endswith("[DONE]"))

    def test_say_requires_text_and_known_marker(self):
        room, _ = self.open_meld()
        with self.assertRaises(SystemExit):
            meld.say(room, "YIELD", "   ", seat="seat-a")
        with self.assertRaises(SystemExit):
            meld.say(room, "MAYBE", "x", seat="seat-a")

    def test_say_refuses_floor_chunk_addressing_nonmember(self):  # noqa: VACUOUS_ASSERTION — valid-YIELD count controls the absence
        room, _ = self.open_meld()
        before = chat.read(room)[1]
        with self.assertRaises(SystemExit) as cm:
            meld.say(room, "YIELD", "@seat-c verify the shape", seat="seat-a")
        self.assertIn("MEMBERSHIP-MISMATCH", str(cm.exception))
        self.assertIn("seat-c", str(cm.exception))
        self.assertEqual(chat.read(room)[1], before)       # refusal posts nothing
        self.assertNotEqual(meld.state(room, "seat-a")["status"], "aborted")
        # Positive control on the same append count: a member-only floor chunk
        # posts and advances the room by exactly one row.
        meld.say(room, "YIELD", "seat-c is a non-participant reference",
                 seat="seat-a")
        self.assertEqual(chat.read(room)[1], before + 1)

    def test_done_status_transitions(self):
        """After your own DONE, recv is the COUNTERSIGN WATCH (
        2026-07-23: the closer went blind — recv refused, so the convener
        could never confirm the peer's close through the meld surface)."""
        room, _ = self.open_meld()
        out = "\n".join(meld.say(room, "DONE", "closing", seat="seat-a"))
        self.assertIn("countersign", out)             # the closer is told how
        self.assertEqual(meld.state(room, "seat-a")["status"], "done")
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, meld.EXIT_BOUND)       # watch, not refusal
        self.assertIn("no countersign", "\n".join(lines))
        meld.say(room, "DONE", "ack", seat="seat-b")
        before = meld.state(room, "seat-a")["exchanges"]
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 0)                     # the countersign lands
        self.assertIn("done-mutual", "\n".join(lines))
        st = meld.state(room, "seat-a")
        self.assertEqual(st["status"], "done-mutual")
        self.assertEqual(st["exchanges"], before + 1)  # countersign COUNTED
        code, _ = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 2)                     # sealed is sealed

    def test_say_into_a_sealed_meld_is_refused_not_a_silent_regression(self):
        """say DONE into a SEALED (done-mutual) meld must REFUSE like recv does
        — NOT silently regress done-mutual -> done and re-post an @peer mention.
        Pre-fix that regression made `meld status` lie and drove a phantom 90s
        countersign watch for an already-consumed countersign — the exact
        double-command an agent replays after compaction/resume (fable dual-gate
        finding, 2026-07-23)."""
        room, _ = self.open_meld()
        meld.say(room, "DONE", "closing", seat="seat-a")       # a -> done
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)   # watch, no countersign
        meld.say(room, "DONE", "ack", seat="seat-b")           # b -> done
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)   # countersign -> done-mutual
        self.assertEqual(meld.state(room, "seat-a")["status"], "done-mutual")
        with self.assertRaises(SystemExit):                    # sealed refuses say too
            meld.say(room, "DONE", "again", seat="seat-a")
        self.assertEqual(meld.state(room, "seat-a")["status"],  # NOT regressed to "done"
                         "done-mutual")


class TestConvergenceLoop(MeldBase):
    def test_full_meld_end_to_end(self):
        """invite → join → READY → GO [YIELD] → answer [YIELD] → DONE both
        ways; the room is the artifact; log-flush picks it up out-of-band."""
        room, epoch = self.open_meld()
        code, _ = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 0)                     # READY → GO moment
        meld.say(room, "YIELD", "propose: rows carry {id,ts,from,text}",
                 seat="seat-a")
        code, lines = meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        self.assertEqual(code, 0)                     # seed [HOLD]
        code, lines = meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        self.assertEqual(code, 0)                     # skips own READY + invite
        self.assertIn("propose:", lines[0])
        self.assertIn("YOURS", lines[1])
        meld.say(room, "YIELD", "agree, plus origin stamps owner rails only",
                 seat="seat-b")
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 0)
        self.assertIn("agree", lines[0])
        meld.say(room, "DONE", "state: converged; next: a lands it",
                 seat="seat-a")
        code, lines = meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        self.assertEqual(code, 0)
        self.assertIn("peer left", lines[1])
        self.assertEqual(meld.state(room, "seat-b")["status"], "peer-done")
        meld.say(room, "DONE", "ack", seat="seat-b")
        self.assertEqual(meld.state(room, "seat-b")["status"], "done-mutual")
        self.assertGreaterEqual(chat.read(room)[1], 7)  # the room IS the record
        self.assertGreater(chat.log_flush(rooms=[room]), 0)  # durable after

    def test_status_lists_live_melds(self):
        room, _ = self.open_meld()
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn(room, out)
        self.assertIn("role=convener", out)


class TestPinnedPair(MeldBase):
    """Live-fire 2026-07-23: recv accepted READY + chunks from ANY non-self
    seat — a meld convened for one seat was consummated by another, and a
    third seat could kill any meld with a forged DONE/ABORT."""

    def test_forged_ready_from_third_seat_never_goes(self):
        room, _ = meld.invite("seat-b", "topic x", seat="seat-a")
        epoch = meld.state(room, "seat-a")["epoch"]
        chat.post("[MELD e:%d] READY:%d (seat-c joined %s)" % (epoch, epoch, room),
                  room=room, who="seat-c", sign=False)
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, meld.EXIT_BOUND)       # no GO from a stranger
        self.assertEqual(meld.state(room, "seat-a")["status"], "invited")
        self.assertIn("non-member", "\n".join(lines))  # noted, never silent
        self.assertIn("seat-c", "\n".join(lines))       # named, not just counted
        meld.join(room, seat="seat-b")                # the real peer
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 0)
        self.assertIn("READY", lines[0])

    def test_forged_abort_from_third_seat_cannot_kill(self):
        room, epoch = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)   # consume READY
        chat.post("[MELD e:%d] die [ABORT]" % epoch, room=room, who="seat-c",
                  sign=False)
        code, _ = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, meld.EXIT_BOUND)       # bound, NOT abort
        self.assertNotEqual(meld.state(room, "seat-a")["status"], "aborted")

    def test_join_refuses_uninvited_seat(self):
        room, _ = meld.invite("seat-b", "topic x", seat="seat-a")
        with self.assertRaises(SystemExit) as cm:
            meld.join(room, seat="seat-c")
        self.assertIn("seat-b", str(cm.exception))    # names the invited seat

    def test_pre_pin_seed_grandfathers_unpinned(self):
        """A seed without invited= (pre-fix meld) still joins."""
        chat.post("[MELD e:7] PROBLEM: old | convener=seat-a cap=5 "
                  "recv-timeout=90s | MELD DISCIPLINE: ... [HOLD]",
                  room="meld-7-old", who="seat-a", sign=False)
        out = "\n".join(meld.join("meld-7-old", seat="seat-c"))
        self.assertIn("MELD-JOINED", out)


class TestIdentity(MeldBase):
    def test_self_seat_is_env_first_like_whoname(self):
        """Live-fire 2026-07-23 identity trap: roster-first _self_seat
        silently overrode HELM_CHAT_NAME for any roster-known session."""
        seats.join(session="sid-x", cwd=self.tmp, seat="roster-name")
        os.environ["CLAUDE_SESSION_ID"] = "sid-x"
        self.assertEqual(meld._self_seat(), "roster-name")  # roster floor holds
        os.environ["HELM_CHAT_NAME"] = "env-name"
        self.assertEqual(meld._self_seat(), "env-name")     # env WINS now

    def test_invite_warns_when_peer_untracked(self):
        _room, lines = meld.invite("seat-b", "topic x", seat="seat-a")
        self.assertIn("NOT on the chat roster", "\n".join(lines))

    def test_invite_no_warning_for_tracked_peer(self):
        seats.join(session="sid-b", cwd=self.tmp, seat="seat-b")
        _room, lines = meld.invite("seat-b", "topic x", seat="seat-a")
        out = "\n".join(lines)
        self.assertNotIn("NOT on the chat roster", out)
        self.assertIn("never lost, only delayed", out)

    def test_invite_rejects_hostile_peer_name(self):
        with self.assertRaises(SystemExit):
            meld.invite("evil\x1b[2Jseat", "topic x", seat="seat-a")


class TestCLI(MeldBase):
    def test_chat_routes_meld(self):
        rc = chat.cmd_chat(["meld", "invite", "seat-b", "wire", "format"])
        self.assertEqual(rc, 0)
        rooms = [r for r in chat.list_rooms() if r.startswith("meld-")]
        self.assertEqual(len(rooms), 1)

    def test_usage_on_garbage(self):
        self.assertEqual(chat.cmd_chat(["meld", "bogus"]), 2)
        self.assertEqual(chat.cmd_chat(["meld", "recv"]), 2)

    def test_seat_flag_names_the_actor(self):
        rc = meld.cmd(["invite", "seat-b", "wire", "--seat", "seat-a"])
        self.assertEqual(rc, 0)
        room = next(r for r in chat.list_rooms() if r.startswith("meld-"))
        self.assertEqual(meld.state(room, "seat-a")["role"], "convener")
        self.assertEqual(meld.cmd(["invite", "seat-b", "x", "--seat"]), 2)
        self.assertEqual(
            meld.cmd(["invite", "seat-b", "x", "--seat", "e\x1bvil"]), 2)

    def test_council_and_standup_spellings_route_and_echo(self):
        """One preset, three spellings (premise
        council-is-the-number-one-feature: meld = the genus; council/standup
        = species, never replacements). The spelling typed echoes back in
        the posted invite and every printed next-command."""
        rc = chat.cmd_chat(["council", "invite", "seat-b", "--tip", "deadbeef",
                            "wire", "format"])
        self.assertEqual(rc, 0)
        room = next(r for r in chat.list_rooms() if r.startswith("meld-"))
        inv = chat.read(room)[0][1]["text"]
        self.assertIn("helm chat council join %s" % room, inv)  # joiner side
        head = seats._clip(seats._scrub(inv))                   # still clip-proof
        self.assertIn("helm chat council join %s" % room, head)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = chat.cmd_chat(["standup", "status", "--seat", "seat-z"])
        self.assertEqual(rc, 0)
        # Without a readable lifecycle journal, an empty slate is UNKNOWN —
        # never a fabricated "no live melds" after a reboot or lost flush.
        self.assertIn("helm chat standup: meld state UNKNOWN for seat seat-z",
                      out.getvalue())

    def test_invite_wait_collapses_invite_and_first_recv(self):
        """--wait = invite + recv in one call (every convener's literal next
        command)."""
        os.environ["HELM_MELD_RECV_TIMEOUT_S"] = "0"
        rc = meld.cmd(["invite", "seat-b", "wire", "--wait", "--seat", "seat-a"])
        self.assertEqual(rc, meld.EXIT_BOUND)         # blocked, bounded out
        room = next(r for r in chat.list_rooms() if r.startswith("meld-"))
        self.assertEqual(meld.state(room, "seat-a")["status"], "invited")


class TestSelfSeat(MeldBase):
    def test_self_seat_survives_a_deleted_cwd(self):
        """LOW (fable adversarial @2b4d496, probe A7): _self_seat's bare
        os.getcwd() crashed ALL five meld verbs (invite/join/recv/say/status
        default their seat through it, with no fail-open wrapper) when the
        process cwd was a pruned worktree. safe_cwd fails open to None and
        derive_seat/auto_name handle cwd=None — the verb keeps its
        family-derived auto-name instead of a FileNotFoundError."""
        os.environ["CLAUDE_SESSION_ID"] = "sid-meld-gone-cwd"
        saved = os.path.dirname(os.path.abspath(__file__))
        self.addCleanup(os.chdir, saved)
        d = tempfile.mkdtemp(dir=self.tmp)
        os.chdir(d)
        os.rmdir(d)
        self.assertRaises(OSError, os.getcwd)   # the probe's precondition
        self.assertTrue(meld._self_seat())      # named, not crashed


class TestStandupMultiParty(MeldBase):
    """standup = the informal 2+ species (owner canon 2026-07-23): the pinned
    PAIR generalized to a pinned SET. The set is the anti-hijack allowlist —
    it admits every member and refuses every outsider (the 2026-07-23 hijack
    hardening, WIDENED, never loosened). council's N-of-M/verdict machinery
    stays 0.3; this is only 'more than one invited seat can converge'."""

    def three_way(self):
        """convener seat-a invites {seat-b, seat-c}; both join → room."""
        room, _ = meld.invite("seat-b,seat-c", "the wire format", seat="seat-a")
        meld.join(room, seat="seat-b")
        meld.join(room, seat="seat-c")
        return room

    def test_invite_pins_the_whole_set(self):
        room, _ = meld.invite("seat-b, @seat-c", "topic x", seat="seat-a")
        self.assertEqual(meld.state(room, "seat-a")["peers"],
                         ["seat-b", "seat-c"])
        allrows = "\n".join(r["text"] for r in chat.read(room)[0])
        self.assertIn("invited=seat-b,seat-c", allrows)   # the set, comma-joined
        self.assertIn("@seat-b", allrows)                 # BOTH invited @mentioned
        self.assertIn("@seat-c", allrows)

    def test_cannot_invite_self(self):
        with self.assertRaises(SystemExit):
            meld.invite("seat-a,seat-b", "topic", seat="seat-a")

    def test_duplicate_peers_deduped(self):
        room, _ = meld.invite("seat-b,seat-b,seat-c", "topic", seat="seat-a")
        self.assertEqual(meld.state(room, "seat-a")["peers"],
                         ["seat-b", "seat-c"])

    def test_every_member_joins_outsider_refused(self):
        room, _ = meld.invite("seat-b,seat-c", "topic", seat="seat-a")
        meld.join(room, seat="seat-b")                    # invited → OK
        meld.join(room, seat="seat-c")                    # invited → OK
        self.assertEqual(meld.state(room, "seat-c")["role"], "joiner")
        with self.assertRaises(SystemExit):               # outsider REFUSED
            meld.join(room, seat="seat-d")

    def test_joiner_accepts_convener_and_other_members(self):
        room = self.three_way()
        # seat-b's accept-set = convener + the OTHER invited, never self
        self.assertEqual(set(meld.state(room, "seat-b")["peers"]),
                         {"seat-a", "seat-c"})

    def test_convener_receives_from_every_member(self):
        room = self.three_way()
        code, _ = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)  # GO
        self.assertEqual(code, 0)
        meld.say(room, "YIELD", "frame: pick the wire shape", seat="seat-a")
        meld.say(room, "YIELD", "b: length-prefixed frames", seat="seat-b")
        meld.say(room, "YIELD", "c: RS-delimited rows", seat="seat-c")
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 0)
        self.assertIn("length-prefixed", lines[0])        # heard seat-b
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 0)
        self.assertIn("RS-delimited", lines[0])           # AND heard seat-c

    def test_outsider_chunk_ignored_never_obeyed(self):
        room = self.three_way()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)   # GO
        epoch = meld.state(room, "seat-a")["epoch"]
        chat.post("[MELD e:%d] hijack [ABORT]" % epoch, room=room,
                  who="seat-d", sign=False)                    # forged ABORT
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertNotEqual(code, meld.EXIT_ABORT)             # a stranger can't kill it
        self.assertNotEqual(meld.state(room, "seat-a")["status"], "aborted")
        self.assertIn("non-member", "\n".join(lines))          # noted, never silent
        self.assertIn("seat-d", "\n".join(lines))

    def test_multiparty_done_needs_every_member(self):
        room = self.three_way()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)   # GO
        meld.say(room, "YIELD", "frame", seat="seat-a")
        meld.say(room, "DONE", "b done", seat="seat-b")
        meld.say(room, "DONE", "c done", seat="seat-c")
        # ONE member's DONE does NOT seal a 2+ standup — noted, floor stays open
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 0)
        self.assertIn("1 of 2", "\n".join(lines))
        self.assertEqual(meld.state(room, "seat-a")["status"], "active")
        # the LAST member's DONE seals it
        code, _ = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        self.assertEqual(code, 0)
        self.assertEqual(meld.state(room, "seat-a")["status"], "peer-done")

    def test_pair_floor_wording_is_unchanged(self):
        """The PAIR floor IS exclusive — the original wording is the truth
        there and must not drift (task #21 changes 2+ only)."""
        room, _ = meld.invite("seat-b", "topic", seat="seat-a")
        meld.join(room, seat="seat-b")
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)      # GO
        meld.say(room, "YIELD", "a speaks", seat="seat-a")
        code, lines = meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        code, lines = meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        self.assertIn("floor: YOURS", "\n".join(lines))

    def test_multiparty_floor_is_honest_not_exclusive(self):
        """task #21: in a 2+ standup the floor is NOT exclusive — any member
        may speak next. Claiming 'YOURS' was a lie the protocol could not
        keep; the truth names the yielder + who is still unheard."""
        room = self.three_way()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)      # GO
        meld.say(room, "YIELD", "frame the problem", seat="seat-a")
        code, lines = meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        while "frame the problem" not in lines[0]:                # skip seed/READY
            code, lines = meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        out = "\n".join(lines)
        self.assertIn("floor: OPEN", out)          # honest, never exclusive
        self.assertNotIn("floor: YOURS", out)      # the lie is gone
        self.assertIn("seat-a", out)               # who yielded
        self.assertIn("seat-c", out)               # who is still unheard

    def test_still_to_hear_drops_a_member_who_already_spoke(self):
        """A re-gate: the set tracked DONE only, so a member who had
        already YIELDed their view stayed listed as unheard — the line nagged
        for input that was already given."""
        room = self.three_way()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)      # GO
        meld.say(room, "YIELD", "c speaks first", seat="seat-c")
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        while "c speaks first" not in lines[0]:
            code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        meld.say(room, "YIELD", "a replies", seat="seat-a")
        meld.say(room, "YIELD", "b speaks", seat="seat-b")
        code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        while "b speaks" not in lines[0]:
            code, lines = meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        out = "\n".join(lines)
        self.assertIn("still to hear from", out)
        self.assertNotIn("seat-c", out.split("still to hear from")[1])

    def test_multiparty_hold_keeps_the_floor_named(self):
        room = self.three_way()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)      # GO
        meld.say(room, "HOLD", "still thinking", seat="seat-a")
        code, lines = meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        while "still thinking" not in lines[0]:
            code, lines = meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        self.assertIn("floor: seat-a's", "\n".join(lines))         # named holder

    def test_single_peer_invite_is_still_the_2party_pair(self):
        room, _ = meld.invite("seat-b", "topic", seat="seat-a")   # size-1 set
        self.assertEqual(meld.state(room, "seat-a")["peers"], ["seat-b"])
        with self.assertRaises(SystemExit):               # still refuses outsiders
            meld.join(room, seat="seat-c")


class TestLifecycleJournal(MeldBase):
    def events(self, room, durable=False):
        path = meld.lifecycle_path(room, durable=durable)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def complete_pair(self):
        room, _ = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)  # READY
        meld.say(room, "YIELD", "a view", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)  # seed
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)  # a view
        meld.say(room, "DONE", "a done", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        meld.say(room, "DONE", "b countersigns", seat="seat-b")
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        return room

    def test_room_global_sequence_and_typed_transitions_stay_in_ram(self):
        room = self.complete_pair()
        events = self.events(room)
        self.assertEqual([e["seq"] for e in events],
                         list(range(1, len(events) + 1)))
        self.assertEqual(len({e["id"] for e in events}), len(events))
        self.assertEqual(events[0]["transition"], "invited")
        self.assertEqual(events[1]["transition"], "accepted")
        self.assertIn("ready", [e["transition"] for e in events])
        self.assertIn("local-yield", [e["transition"] for e in events])
        self.assertIn("peer-done", [e["transition"] for e in events])
        self.assertNotIn("idx", json.dumps(events))
        self.assertFalse(os.path.exists(meld.lifecycle_path(room, durable=True)),
                         "meld transitions must not touch durable storage")
        self.assertEqual(meld.state(room, "seat-a")["status"], "done-mutual")
        self.assertEqual(meld.state(room, "seat-a")["spoke_peers"], ["seat-b"])

    def test_failed_ram_append_does_not_advance_snapshot(self):  # noqa: VACUOUS_ASSERTION — successful append control proves advancement first
        room, _ = self.open_meld()
        before = dict(meld.state(room, "seat-a"))
        meld.say(room, "YIELD", "control advances", seat="seat-a")
        advanced = dict(meld.state(room, "seat-a"))
        self.assertGreater(advanced["journal_seq"], before["journal_seq"])
        with mock.patch.object(meld.eventledger, "append", return_value=False):
            with self.assertRaises(meld.LifecycleError):
                meld.say(room, "HOLD", "not journalled", seat="seat-a")
        before = advanced
        after = meld.state(room, "seat-a")
        self.assertEqual(after["journal_seq"], before["journal_seq"])
        self.assertEqual(after["status"], before["status"])

    def test_snapshot_failure_repairs_from_ram_event(self):  # noqa: VACUOUS_ASSERTION — pre-failure snapshot/event equality is the positive control
        room, _ = self.open_meld()
        control = meld.state(room, "seat-a")
        self.assertEqual(control["journal_seq"], self.events(room)[-2]["seq"])
        real = meld._write_state
        calls = []

        def fail_once(room_, seat_, st):
            calls.append((room_, seat_))
            if len(calls) == 1:
                raise OSError("snapshot unavailable")
            return real(room_, seat_, st)

        with mock.patch.object(meld, "_write_state", side_effect=fail_once):
            with self.assertRaises(OSError):
                meld.say(room, "YIELD", "journal wins", seat="seat-a")
        repaired = meld.state(room, "seat-a")
        self.assertEqual(repaired["status"], "active")
        self.assertEqual(repaired["journal_seq"], self.events(room)[-1]["seq"])

    def test_flush_wipe_replay_restores_state_and_room_history(self):
        room = self.complete_pair()
        self.assertGreater(chat.log_flush(rooms=[room]), 0)
        durable = self.events(room, durable=True)
        self.assertEqual([e["seq"] for e in durable],
                         list(range(1, len(durable) + 1)))
        self.assertTrue(os.path.exists(chat.room_path(room)))
        shutil.rmtree(chat.chat_dir())
        out = chat.restore_journal(apply=True)
        self.assertEqual(out["meld"]["rooms"][room]["state"], "ok")
        for actor in ("seat-a", "seat-b"):
            st = meld.state(room, actor)
            self.assertEqual(st["status"], "done-mutual")
            self.assertEqual(st["idx"], 0)
            # the restore is ROOM provenance in the stream, not an actor bit:
            # both actors derive the same replayed durability from the
            # restored-from-journal marker the replay appended.
            self.assertEqual(meld._durability(room, st), "replayed")
        self.assertIn("restored-from-journal",
                      [e["transition"] for e in self.events(room)])
        # room CONTENT restores too — the 2026-08-02 reboot proved the
        # conversation is the work product, not clutter (lifecycle skeletons
        # alone left the fleet rebuilding specs from transcripts by hand)
        self.assertTrue(os.path.exists(chat.room_path(room)))
        rows, _total = chat.read(room)
        self.assertTrue(any("RESTORED from the disk journal" in str(r.get("text"))
                            for r in rows))
        self.assertEqual(chat.restore_journal(apply=True)["meld"]["rooms"][room]["state"],
                         "ok")

    def test_duplicate_is_idempotent_but_conflict_is_unknown(self):
        room = self.complete_pair()
        chat.log_flush(rooms=[room])
        path = meld.lifecycle_path(room, durable=True)
        first = self.events(room, durable=True)[0]
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(first) + "\n")
        shutil.rmtree(chat.chat_dir())
        self.assertEqual(meld.replay_room(room, apply=True)["state"], "ok")
        shutil.rmtree(chat.chat_dir())
        bad = dict(first, transition="accepted")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(bad) + "\n")
        out = meld.replay_room(room, apply=True)
        self.assertEqual(out["state"], "UNKNOWN")
        self.assertIn("conflicting duplicate", out["reason"])

    def test_gap_malformed_and_unknown_version_are_unknown(self):
        room = self.complete_pair()
        chat.log_flush(rooms=[room])
        path = meld.lifecycle_path(room, durable=True)
        rows = self.events(room, durable=True)
        self.assertEqual(meld.replay_room(room, apply=False)["state"], "ok")
        cases = [rows[:1] + rows[2:],
                 [dict(rows[0], v=99)],
                 [dict(rows[0], projection={})]]
        for i, planted in enumerate(cases):
            with self.subTest(i=i):
                with open(path, "w", encoding="utf-8") as f:
                    for row in planted:
                        f.write(json.dumps(row) + "\n")
                shutil.rmtree(chat.chat_dir(), ignore_errors=True)
                out = meld.replay_room(room, apply=True)
                self.assertEqual(out["state"], "UNKNOWN")

    def test_legacy_snapshot_imports_once_on_first_flush(self):
        room = "meld-7-legacy"
        st = {"room": room, "epoch": 7, "role": "convener", "self": "seat-a",
              "peer": "seat-b", "peers": ["seat-b"], "idx": 9,
              "exchanges": 2, "cap": 5, "status": "active",
              "created": "2026-08-02T00:00:00Z", "done_peers": [],
              "spoke_peers": ["seat-b"]}
        meld._write_state(room, "seat-a", st)
        self.assertEqual(chat.log_flush(rooms=[room]), 0)
        rows = self.events(room, durable=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["transition"], "legacy-snapshot")
        self.assertNotIn("idx", rows[0]["projection"])
        self.assertEqual(chat.log_flush(rooms=[room]), 0)
        self.assertEqual(len(self.events(room, durable=True)), 1)

    def test_empty_status_is_unknown_without_a_durable_lifecycle_floor(self):
        out = "\n".join(meld.status(seat="seat-z"))
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("no live melds", out)


class ReplayedStatusRenderTest(MeldBase):
    """#126 / meld-replayed-reads-as-active (P0-misleading). A REPLAYED meld
    whose frozen status is active/invited must NOT render as awaiting a turn
    — the restore froze the pre-reboot status, and a conversation dead for
    hours reading status=active already cost two seats a live exchange
    co-designing inside a 35-hour-old ghost. durability=replayed was already
    in the row; the renderer is the consumer that field never had (the
    another-field-already-answered-this class). Every row also carries the
    AGE of the last real exchange."""

    def _replay_active_meld(self):
        """An active meld, flushed, wiped, restored -> durability=replayed,
        status frozen at active."""
        room, _ = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        meld.say(room, "YIELD", "mid-conversation", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        chat.log_flush(rooms=[room])
        shutil.rmtree(chat.chat_dir())
        chat.restore_journal(apply=True)
        return room

    def test_a_replayed_active_meld_never_reads_as_awaiting(self):
        room = self._replay_active_meld()
        st = meld.state(room, "seat-a")
        self.assertEqual(st["status"], "active")          # the FROZEN truth
        self.assertEqual(meld._durability(room, st), "replayed")
        out = "\n".join(meld.status(seat="seat-a"))
        # the EFFECT: no line presents this ghost as turn-owed
        self.assertNotIn("status=active ", out)
        self.assertIn("status=active-replayed", out)
        self.assertIn("durability=replayed", out)

    def test_every_row_carries_an_age(self):
        room = self._replay_active_meld()
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("age=", out)

    def test_a_LIVE_active_meld_still_reads_active(self):
        """The control: an unrestored active meld is genuinely turn-owed and
        must read exactly that — the replayed qualifier is for ghosts only."""
        room, _ = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        meld.say(room, "YIELD", "live", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("status=active ", out)
        self.assertNotIn("active-replayed", out)
        self.assertIn("age=", out)          # live rows carry an age too

    def test_answering_a_ghost_marks_it_REOPENED_never_plain_active(self):
        """codex r2 HIGH (meld-1785588958's exact shape): one live say into a
        replayed meld must NOT launder the archive back to turn-owed. The
        conversation stays a replay ORIGIN (durability=replayed-reopened) and
        the awaiting status keeps the replay qualifier — a mistaken reply
        cannot make the ghost look like it was always live."""
        room = self._replay_active_meld()
        meld.say(room, "YIELD", "a reply into the ghost", seat="seat-a")
        st = meld.state(room, "seat-a")
        # the reopened fact lives in the STREAM (a typed marker), so the
        # replay origin cannot be erased by a live event — both halves of
        # the mixed state are room-level provenance now.
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            kinds = [json.loads(line)["transition"] for line in f if line.strip()]
        self.assertIn("restored-from-journal", kinds)
        self.assertIn("reopened-after-replay", kinds)
        self.assertEqual(meld._durability(room, st), "replayed-reopened")
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertNotIn("status=active ", out)      # never plain turn-owed
        self.assertIn("status=active-reopened", out)  # the MIXED third status
        self.assertNotIn("status=active-replayed", out)  # not the frozen label
        self.assertIn("durability=replayed-reopened", out)

    def test_a_malformed_room_epoch_renders_no_age_not_55_years(self):
        """codex r2 MED: the room-name epoch is a CLAIM, and a garbage one is
        not a clock. meld-1-name must render silence on the age suffix, never
        age=20667d; a plausible epoch still resolves. r3: the validator moved
        to _room_name_epoch (migration-only; the stream is the age authority)."""
        self.assertIsNone(meld._room_name_epoch("meld-1-name"))
        self.assertIsNone(meld._room_name_epoch(
            "meld-99999999999-name"))                        # far future
        self.assertIsNone(meld._last_activity_epoch("meld-1-name"))
        # a plausible epoch (at/after founding, not future) resolves
        self.assertEqual(meld._room_name_epoch("meld-1785719199-topic"),
                         1785719199)
        self.assertEqual(meld._last_activity_epoch("meld-1785719199-topic"),
                         1785719199)

    def test_a_converged_replayed_meld_keeps_its_terminal_status(self):
        """done* is the truth whether live or replayed — the qualifier only
        fires on the awaiting statuses (active/invited), never on a sealed
        conversation."""
        room, _ = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        meld.say(room, "DONE", "a done", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        meld.say(room, "DONE", "b countersigns", seat="seat-b")
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        chat.log_flush(rooms=[room])
        shutil.rmtree(chat.chat_dir())
        chat.restore_journal(apply=True)
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("status=done-mutual", out)
        self.assertNotIn("done-mutual-replayed", out)
        self.assertIn("durability=replayed", out)
        self.assertIn("age=", out)


class RoomOwnedProvenanceTest(MeldBase):
    """r3 (codex HIGH+MED, fleet-converged at meld-1785730874): replay origin
    and post-replay-live are ROOM provenance derived from the lifecycle event
    stream — never actor-local snapshot bits. The r1/r2 model kept _replayed/
    _reopened on the actor snapshot, so a repair (delete the snapshot +
    _repair_from_ram) or a second wipe+restore silently LOST the mixed
    history, and the two actors could disagree about the same room. Age comes
    from the stream alone; the room-name epoch is a migration validator."""

    def _mixed_room(self):
        """An active meld, flushed, wiped, restored, then ANSWERED live —
        the replayed-then-reopened mixed history."""
        room, _ = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        meld.say(room, "YIELD", "mid-conversation", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        chat.log_flush(rooms=[room])
        shutil.rmtree(chat.chat_dir())
        chat.restore_journal(apply=True)
        meld.say(room, "YIELD", "a reply into the ghost", seat="seat-a")
        return room

    def _events(self, room, durable=False):
        path = meld.lifecycle_path(room, durable=durable)
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_both_actors_read_the_same_mixed_state(self):
        """Cross-actor consistency: provenance is derived from the room's
        stream, so seat-a and seat-b render the IDENTICAL durability — the
        r2 replying-actor-only _reopened bit made them disagree."""
        room = self._mixed_room()
        for actor in ("seat-a", "seat-b"):
            st = meld.state(room, actor)
            self.assertEqual(meld._durability(room, st), "replayed-reopened")
            out = "\n".join(meld.status(seat=actor))
            self.assertIn("status=active-reopened", out)
            self.assertIn("durability=replayed-reopened", out)
            self.assertNotIn("status=active ", out)

    def test_mixed_state_survives_a_snapshot_repair(self):
        """Repair: delete the snapshot, force _repair_from_ram — the mixed
        history is IN THE STREAM, so it cannot die with the snapshot (the
        exact r2 defect)."""
        room = self._mixed_room()
        os.unlink(meld.state_path(room, "seat-a"))
        st = meld.state(room, "seat-a")          # repaired from RAM
        self.assertEqual(meld._durability(room, st), "replayed-reopened")
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("durability=replayed-reopened", out)

    def test_mixed_state_survives_a_second_wipe_and_restore(self):
        """Second wipe+restore: the restored AND reopened markers both live
        in the durable journal, so the mixed state re-derives after the RAM
        is gone again (the r2 in-memory _REPLAYED set died at the process)."""
        room = self._mixed_room()
        chat.log_flush(rooms=[room])
        shutil.rmtree(chat.chat_dir())
        chat.restore_journal(apply=True)
        kinds = [e["transition"] for e in self._events(room)]
        self.assertIn("restored-from-journal", kinds)
        self.assertIn("reopened-after-replay", kinds)
        for actor in ("seat-a", "seat-b"):
            st = meld.state(room, actor)
            self.assertEqual(meld._durability(room, st), "replayed-reopened")
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("durability=replayed-reopened", out)
        self.assertIn("status=active-reopened", out)

    def test_the_three_render_states(self):
        """live / replayed / replayed-reopened — one room each, the rendered
        EFFECT asserted, never absence-of-complaint."""
        live, _ = meld.invite("seat-b", "live room", seat="seat-a")
        meld.join(live, seat="seat-b")
        meld.recv(live, timeout=0, seat="seat-a", poll=0.01)   # -> active
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn(live, out)
        self.assertIn("status=active ", out)
        self.assertNotIn("replayed", out.split(live)[1])

        room, _ = meld.invite("seat-b", "ghost room", seat="seat-a")
        meld.join(room, seat="seat-b")
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        meld.say(room, "YIELD", "mid", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        chat.log_flush(rooms=[room])
        shutil.rmtree(chat.chat_dir())
        chat.restore_journal(apply=True)
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("status=active-replayed", out)
        self.assertIn("durability=replayed age=", out)

        meld.say(room, "YIELD", "reopen it", seat="seat-a")
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("durability=replayed-reopened", out)
        self.assertIn("status=active-reopened", out)
        self.assertNotIn("status=active-replayed", out)

    def test_age_comes_from_the_event_stream_only(self):
        """The stream's last_activity is the ONE clock: journal a meld, then
        lie about both fallback clocks (room content rows + created) — the
        rendered age must still track the stream's epoch, and erasing the
        stream authority must erase the age even when the name epoch and the
        content rows BOTH disagree with it (the migration fallback is exactly
        that: a fallback, never an authority that can outvote the stream)."""
        room, _ = self.open_meld()
        # a content row stamped far in the past must NOT set the age — the
        # old content-row clock would have read it (codex r3 MED).
        chat._append({"ts": "2026-01-01T00:00:00Z", "from": "seat-b",
                      "text": "a restored-looking old row"}, room)
        ep = meld._last_activity_epoch(room)
        # the stream's own clock, not the row's — recomputed from the journal,
        # never pinned to a second wall-clock sample (invite and join may
        # straddle a second boundary; gate d351c1a4 read exactly that +1s)
        self.assertEqual(ep, max(meld._ts_epoch(e["ts"])
                                 for e in self._events(room)))
        delta = int(time.time()) - ep
        self.assertLess(delta, 120)              # fresh, not 200+ days
        out = "\n".join(meld.status(seat="seat-a"))
        m = re.search(r"age=(\d+)([smhd])", out)
        self.assertIsNotNone(m)                  # the age renders
        self.assertIn(m.group(2), "sm")          # fresh, never the row's 237d

    def test_the_stream_outvotes_the_name_epoch(self):
        """A journaled room whose NAME epoch lies (stamped a month back, but
        still VALID under the migration validator) must age from the STREAM,
        not the name: the name is a claim, the stream is the authority, and
        the migration fallback fires only when the stream cannot date the
        room at all (legacy-snapshot-only / unjournaled)."""
        room, epoch = self.open_meld()
        stale = max(1784663952, epoch - 30 * 86400)   # a valid-but-stale claim
        renamed = "meld-%d-%s" % (stale, room.split("-", 2)[2])
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        for ev in events:                        # the reducer keys on room+id:
            ev["room"] = renamed                 # restamp both honestly so the
            ev["id"] = meld._event_id(renamed, ev["seq"])  # stream still reads
        with open(meld.lifecycle_path(renamed), "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        self.assertTrue(meld._room_name_epoch(renamed))      # the claim validates
        got = meld._last_activity_epoch(renamed)
        self.assertEqual(got, max(meld._ts_epoch(e["ts"])
                                  for e in events))          # the stream WINS
        self.assertGreater(got, stale + 86400)               # the name claim lost

    def test_last_activity_is_the_event_CLOCK_not_the_room_epoch(self):
        """THE DISCRIMINATING ARM for the epoch-vs-ts clock class: a room
        created long ago (old epoch) with a FRESH exchange (recent event ts)
        must age from the EVENT TIME, not the creation epoch. Under the
        epoch-clock bug (last_activity = ev["epoch"], constant per room) this
        renders the creation age forever and hides the fresh exchange — the
        same wrong-clock class as the name-epoch fallback, one layer down."""
        room, epoch = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        meld.say(room, "YIELD", "fresh exchange", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        # stamp the room's epoch far into the past (the events' ts is NOW;
        # the epoch is the constant creation marker)
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        for ev in events:
            ev["epoch"] = 1784663952          # founding-era creation marker
        with open(meld.lifecycle_path(room), "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        ep = meld._last_activity_epoch(room)
        # the fresh exchange dominates: activity is ~now, NOT 1784663952
        self.assertGreater(ep, 1784663952 + 86400,
                           "last_activity followed the room epoch, not the "
                           "event clock — a fresh exchange was invisible")

    def test_an_invalid_stream_clock_is_UNKNOWN_never_age_0s(self):
        """codex r3 MED: a journal that exists but whose events carry an
        unparseable ts is UNKNOWN activity — the name-epoch fallback must NOT
        fire (it would render a confident age=0s off a claim)."""
        room, _ = self.open_meld()
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        for ev in events:
            ev["ts"] = "not-a-clock"
        with open(meld.lifecycle_path(room), "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        self.assertIsNone(meld._last_activity_epoch(room),
                          "an invalid stream clock fell back to the name epoch")

    def test_a_future_stream_clock_is_UNKNOWN_never_age_0s(self):
        """A future event ts is impossible; accepting it renders age=0s
        (max(0, delta)) as confident certainty. UNKNOWN/silent, hard reject."""
        room, _ = self.open_meld()
        import time as _t
        future = _t.strftime("%Y-%m-%dT%H:%M:%SZ",
                             _t.gmtime(_t.time() + 7200))
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        for ev in events:
            ev["ts"] = future
        with open(meld.lifecycle_path(room), "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        self.assertIsNone(meld._last_activity_epoch(room),
                          "a future stream clock rendered a confident age")

    def test_room_name_epoch_validator_boundaries(self):
        """before-founding -> no age; future -> no age (hard reject, no
        slack); valid -> age. The validator's only consumer is the migration
        fallback for rooms the stream cannot date."""
        now = int(time.time())
        self.assertIsNone(meld._room_name_epoch("meld-1784663951-x"))  # pre-founding
        self.assertIsNone(meld._room_name_epoch("meld-%d-x" % (now + 60)))
        self.assertEqual(meld._room_name_epoch("meld-1784663952-x"), 1784663952)
        self.assertEqual(meld._room_name_epoch("meld-%d-x" % now), now)
        # the age path honors the validator: a stream-less room dates from a
        # VALID name epoch and stays silent on an invalid one
        self.assertEqual(meld._last_activity_epoch("meld-%d-x" % now), now)
        self.assertIsNone(meld._last_activity_epoch("meld-%d-x" % (now + 60)))
        self.assertEqual(meld._age_suffix("meld-1-x", {}), "")


class ParentFormatMigrationTest(MeldBase):
    """codex r4 MIGRATION (the blocking class): the r2 parent format stored
    _replayed/_reopened on the ACTOR SNAPSHOTS and its stream has NO markers.
    A stream-only reader laundered every such room back to durable-live —
    the original turn-owed bug reintroduced for all existing data. The bits
    fold into typed room markers ONCE, fail-closed on actor disagreement,
    then are never consulted again."""

    def _parent_shaped_room(self, replayed=True, reopened=True,
                            actors=("seat-a", "seat-b"), disagree=False):
        """An active meld, RAM wiped, then hand-restored in the r2 PARENT
        shape: the markerless stream in RAM + actor snapshots carrying the
        bits, NO durable journal (the durable copy is the migration's
        output, not its input — the parent's replay wrote RAM-only)."""
        room, _ = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        meld.say(room, "YIELD", "mid-conversation", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            stream = [json.loads(line) for line in f if line.strip()]
        kinds = [e["transition"] for e in stream]
        self.assertNotIn("restored-from-journal", kinds)   # parent: no markers
        shutil.rmtree(chat.chat_dir())
        # hand-restore the parent shape: stream copied verbatim (no marker),
        # snapshots re-materialized WITH the actor bits, no journal_seq.
        os.makedirs(os.path.dirname(meld.lifecycle_path(room)))
        with open(meld.lifecycle_path(room), "w", encoding="utf-8") as f:
            for ev in stream:
                f.write(json.dumps(ev) + "\n")
        states, unique, why, _rp = meld._reduce(stream, room)
        self.assertIsNone(why)
        for actor in actors:
            folded = states[actor]
            st = meld._snapshot(room, actor, folded)
            st.pop("journal_seq", None)        # the parent had no journal seq
            st["idx"] = 3
            st["_replayed"] = replayed
            st["_reopened"] = reopened if not disagree else (actor == "seat-a")
            meld._write_state(room, actor, st)
        return room, kinds

    def test_parent_bits_migrate_into_room_markers_on_first_read(self):
        """A parent-shaped FROZEN room (bits on snapshots, markerless
        stream) must render the replayed state after migration — never
        plain live. The fold appends the restore marker honestly at the
        frozen tail, retires the bits from every snapshot, and fires only
        once."""
        room, _ = self._parent_shaped_room(replayed=True, reopened=False)
        st = meld.state(room, "seat-a")                  # the first read migrates
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            kinds = [json.loads(l)["transition"] for l in f if l.strip()]
        self.assertIn("restored-from-journal", kinds)    # folded into the stream
        self.assertEqual(meld._durability(room, st), "replayed")
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("status=active-replayed", out)     # the ghost reading
        self.assertNotIn("status=active ", out)          # never laundered live
        # the bits are retired — the stream is the sole authority now
        for actor in ("seat-a", "seat-b"):
            snap = pk.read_json(meld.state_path(room, actor))
            self.assertNotIn("_replayed", snap)
            self.assertNotIn("_reopened", snap)
        before = len(kinds)
        meld.state(room, "seat-a")                       # a second read folds nothing
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            self.assertEqual(len([l for l in f if l.strip()]), before)

    def test_parent_reopened_is_UNKNOWN_never_laundered_live(self):
        """A MIXED parent room (_replayed AND _reopened, markerless stream):
        the bits certify a resume but name neither the restore point nor the
        resume event, so NO honest marker splice exists — every candidate
        either restates the wrong projection (impossible transition) or
        places the markers adjacent (a reopen with no intervening content).
        FAIL-CLOSED: UNKNOWN with the reason named, no marker written, and
        NEVER laundered back to plain live (the original bug's shape)."""
        room, _ = self._parent_shaped_room(replayed=True, reopened=True)
        st = meld.state(room, "seat-a")
        self.assertEqual(st["status"], "UNKNOWN")
        self.assertIn("reopen", st.get("_unknown", ""))
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            kinds = [json.loads(l)["transition"] for l in f if l.strip()]
        self.assertNotIn("restored-from-journal", kinds)  # no guessed splice
        self.assertNotIn("reopened-after-replay", kinds)
        self.assertEqual(meld._durability(room, st), "UNKNOWN")
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("status=active ", out)          # never laundered live
        self.assertNotIn("durability=durable-live", out)

    def test_the_migration_receipt_reports_the_POST_fold_stream(self):
        """codex r6's exact probe: a parent-shaped room WITH a durable
        journal (markerless stream + actor bits) migrates through replay_room
        — and the receipt must report the POST-fold truth. Before the fix it
        returned the pre-fold count and replayed=false while RAM and durable
        each carried the marker, so restore-journal printed 'would replay'
        after --apply."""
        room, _epoch = self._parent_shaped_room(replayed=True, reopened=False)
        # give the room a DURABLE journal (replay_room is durable-primary:
        # it reads the durable stream first)
        import shutil as _sh
        os.makedirs(os.path.dirname(meld.lifecycle_path(room, True)),
                    exist_ok=True)
        _sh.copy(meld.lifecycle_path(room), meld.lifecycle_path(room, True))
        out = meld.replay_room(room, apply=True)
        self.assertEqual(out["state"], "ok", out)
        n_ram = len(meld._events(meld.lifecycle_path(room))[0])
        n_dur = len(meld._events(meld.lifecycle_path(room, True))[0])
        self.assertEqual(n_ram, n_dur)
        self.assertEqual(out["events"], n_ram,
                         "the receipt counted the PRE-fold stream")
        self.assertTrue(out["replayed"],
                        "a migration from durable bits did not read as a restore")

    def test_the_first_state_read_after_a_fold_is_not_UNKNOWN(self):
        """codex r6b exact review: the OTHER migration caller (_repair_from_ram)
        treated the helper's truthy "folded" return as a reason, so a first
        state() read appended the marker then returned status=UNKNOWN
        reason=folded. The state must be the replayed/mixed truth, not UNKNOWN."""
        room, _epoch = self._parent_shaped_room(replayed=True, reopened=False)
        st = meld.state(room, "seat-a")          # the first read folds via _repair_from_ram
        self.assertNotEqual(st.get("status"), "UNKNOWN",
                            "a successful fold read as UNKNOWN reason=folded")
        self.assertEqual(meld._durability(room, st), "replayed")

    def test_a_no_op_migration_never_reports_replayed(self):
        """codex r6 second probe: a normal LIVE room (no parent bits, nothing
        to fold) must NOT report replayed — restored=True was being set after
        EVERY migration call including no-ops, so restore-journal lied about
        live rooms. The helper reports whether it ACTUALLY folded."""
        room, _ = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        import shutil as _sh
        os.makedirs(os.path.dirname(meld.lifecycle_path(room, True)),
                    exist_ok=True)
        _sh.copy(meld.lifecycle_path(room), meld.lifecycle_path(room, True))
        out = meld.replay_room(room, apply=True)
        self.assertEqual(out["state"], "ok")
        self.assertFalse(out["replayed"],
                         "a live room with nothing to fold reported replayed")

    def test_parent_reopened_stays_UNKNOWN_across_a_status_replay(self):
        """The mixed UNKNOWN verdict must survive status()'s replay pass:
        replay_durable clears the _UNKNOWN ledger, so a durability reader
        that re-derived live from the still-markerless stream would launder
        the room back — the markerless SHAPE plus the bits must re-fire the
        verdict on every read."""
        room, _ = self._parent_shaped_room(replayed=True, reopened=True)
        meld.state(room, "seat-a")                        # fires the verdict
        meld._UNKNOWN.pop(meld._cache_key(room), None)    # replay clears it
        self.assertEqual(meld._durability(
            room, meld.pk.read_json(meld.state_path(room, "seat-a"))),
            "UNKNOWN")                                    # re-fired, not live
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("durability=durable-live", out)

    def test_parent_replayed_only_migrates_to_frozen_replayed(self):
        """_replayed=True, _reopened=False (the ghost was never answered)
        folds the restore marker ALONE and renders the frozen replayed
        state — a reopen marker would claim a resumption that never
        happened."""
        room, _ = self._parent_shaped_room(replayed=True, reopened=False)
        st = meld.state(room, "seat-a")
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            kinds = [json.loads(l)["transition"] for l in f if l.strip()]
        self.assertIn("restored-from-journal", kinds)
        self.assertNotIn("reopened-after-replay", kinds)
        self.assertEqual(meld._durability(room, st), "replayed")
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("status=active-replayed", out)
        self.assertNotIn("status=active ", out)

    def test_actor_disagreement_is_UNKNOWN_never_a_guess(self):
        """FAIL-CLOSED: two actors' snapshots disagreeing about the replay
        state is UNKNOWN — the migration never picks a side, appends no
        marker, and the room renders UNKNOWN with the disagreement named."""
        room, _ = self._parent_shaped_room(replayed=True, reopened=True,
                                           disagree=True)
        st = meld.state(room, "seat-a")
        self.assertEqual(st["status"], "UNKNOWN")
        self.assertIn("disagree", st.get("_unknown", ""))
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            kinds = [json.loads(l)["transition"] for l in f if l.strip()]
        self.assertNotIn("restored-from-journal", kinds)  # no guess appended
        out = "\n".join(meld.status(seat="seat-a"))
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("status=active ", out)
        self.assertNotIn("durability=durable-live", out)  # never laundered


class ReplayedLockTest(MeldBase):
    """codex r4 LOCK: replay_room used to run bare while _transition holds
    chat._room_lock — a live transition racing a restore could interleave
    after the durable copy but before the restore marker, both allocating
    the same seq, so the completed RAM stream reduces to 'conflicting
    duplicate sequence N' and the next read is UNKNOWN."""

    def _flushed_wiped_room(self):
        room, _ = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        meld.say(room, "YIELD", "mid", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        chat.log_flush(rooms=[room])
        shutil.rmtree(chat.chat_dir())
        return room

    def test_replay_room_holds_the_room_lock_for_its_whole_critical_section(self):
        """The inspect/copy/marker/snapshot sequence must serialize with live
        transitions: the lock is held from the durable inspect through the
        snapshot writes. The reduce seam is the probe: EVERY reduce
        replay_room runs must find the room lock held for THIS process —
        a bare replay_room would run them all unlocked."""
        room = self._flushed_wiped_room()
        reduce_saw_held = []
        real_reduce = meld._reduce

        def probe_reduce(events, r, **_kw):
            reduce_saw_held.append(meld._lock_held(r))
            return real_reduce(events, r)

        with mock.patch.object(meld, "_reduce", side_effect=probe_reduce):
            out = meld.replay_room(room, apply=True)
        self.assertEqual(out["state"], "ok")
        self.assertTrue(reduce_saw_held, "no reduce ran — the probe is vacuous")
        self.assertTrue(all(reduce_saw_held),
                        "a reduce ran outside the room lock")

    def test_replay_room_reduces_the_completed_stream_before_reporting_ok(self):
        """A verb that reports ok on a stream it has not reduced is claiming
        an outcome it did not measure: the restored RAM stream is re-reduced
        AFTER the marker append, so a successful report implies a coherent
        history. Plant a reduce that would fail on the completed stream and
        assert replay_room surfaces UNKNOWN, not ok."""
        room = self._flushed_wiped_room()
        real_reduce = meld._reduce
        calls = []

        def sabotage_completed(events, r, **_kw):
            out = real_reduce(events, r)
            calls.append([e["transition"] for e in events])
            # the POST-MARKER reduce (the completion check): lie UNKNOWN
            if "restored-from-journal" in calls[-1] and \
                    len(calls) >= 2 and "restored-from-journal" not in calls[-2]:
                return {}, [], "planted incoherent completed stream", \
                    meld._EMPTY_ROOMPROJ
            return out

        with mock.patch.object(meld, "_reduce", side_effect=sabotage_completed):
            out = meld.replay_room(room, apply=True)
        self.assertEqual(out["state"], "UNKNOWN")   # the lie was caught
        self.assertIn("planted incoherent", out["reason"])
        # control: without the sabotage the same room replays ok
        self.assertEqual(meld.replay_room(room, apply=True)["state"], "ok")


class ProvenanceCausalityTest(MeldBase):
    """codex r4 CAUSAL: _valid_transition checked only that a marker restates
    the actor projection, and _room_projection treated marker PRESENCE as
    truth — so three malformed histories reduced cleanly. The owner reducer
    now enforces room-level ordering/causality: a reopen needs a prior
    restore AND live content between; a malformed history is UNKNOWN."""

    def _mixed_fixture(self):
        """A REAL mixed room (active meld, flushed, wiped, restored, then
        answered live) -> (room, events) whose stream carries the honest
        restore/content/reopen shape. The malformed arms REORDER this real
        stream so only the causal shape is wrong — the per-event identities
        stay valid."""
        room, _ = self.open_meld()
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        meld.say(room, "YIELD", "mid-conversation", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        chat.log_flush(rooms=[room])
        shutil.rmtree(chat.chat_dir())
        chat.restore_journal(apply=True)
        meld.say(room, "YIELD", "a reply into the ghost", seat="seat-a")
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            events = [json.loads(l) for l in f if l.strip()]
        return room, events

    def _reduce_shape(self, room, events, drop=(), order=None):
        """Re-stamp the real stream with the named transition events dropped
        and/or the tail reordered, ids re-derived so the only wrongness is
        the CAUSAL SHAPE; return _reduce's (why, roomproj)."""
        kept = [e for e in events if e["transition"] not in drop]
        if order is not None:
            head = [e for e in kept if e["transition"] not in order]
            tail = {e["transition"]: e for e in kept if e["transition"] in order}
            kept = head + [tail[t] for t in order if t in tail]
        for i, ev in enumerate(kept, 1):
            ev["seq"] = i
            ev["id"] = meld._event_id(room, i)
        _states, _unique, why, roomproj = meld._reduce(kept, room)
        return why, roomproj

    def test_reopen_without_any_restore_is_UNKNOWN(self):
        room, events = self._mixed_fixture()
        why, _rp = self._reduce_shape(room, events,
                                      drop=("restored-from-journal",))
        self.assertIsNotNone(why)
        self.assertIn("without a prior restore", why)

    def test_reopen_ordered_before_restore_is_UNKNOWN(self):
        room, events = self._mixed_fixture()
        why, _rp = self._reduce_shape(
            room, events,
            order=("reopened-after-replay", "restored-from-journal"))
        self.assertIsNotNone(why)
        self.assertIn("without a prior restore", why)

    def test_restore_then_reopen_with_no_intervening_live_content_is_UNKNOWN(self):
        room, events = self._mixed_fixture()
        # pull the restore AFTER every live event so it sits immediately
        # before the reopen: restore, reopen, no content between
        head = [e for e in events
                if e["transition"] not in ("restored-from-journal",
                                           "reopened-after-replay")]
        rest = [e for e in events
                if e["transition"] == "restored-from-journal"][-1:]
        rop = [e for e in events
               if e["transition"] == "reopened-after-replay"][-1:]
        why, _rp = self._reduce_shape(room, head + rest + rop)
        self.assertIsNotNone(why)
        self.assertIn("no intervening live content", why)

    def test_the_valid_shape_reduces_to_the_mixed_state(self):
        """Control: the REAL mixed stream (restore, live content, reopen)
        is the ONE honest history and must reduce cleanly to the mixed
        projection."""
        room, events = self._mixed_fixture()
        why, roomproj = self._reduce_shape(room, events)
        self.assertIsNone(why)
        self.assertTrue(roomproj["restored"])
        self.assertTrue(roomproj["reopened"])

    def test_a_rerestore_before_the_reopen_re_arms_it(self):
        """A second restore landing BETWEEN the live content and the reopen
        (a wipe+replay racing the resumption) RESETS the intervening count:
        the earlier live event belongs to the PREVIOUS restore, so the
        pending reopen now has no intervening content and is UNKNOWN."""
        room, events = self._mixed_fixture()
        import copy
        rest = [e for e in events
                if e["transition"] == "restored-from-journal"][-1]
        rop_i = next(i for i, e in enumerate(events)
                     if e["transition"] == "reopened-after-replay")
        spliced = events[:rop_i] + [copy.deepcopy(rest)] + events[rop_i:]
        why, _rp = self._reduce_shape(room, spliced)
        self.assertIsNotNone(why)
        self.assertIn("no intervening live content", why)


class ClockInvariantPropertyTest(MeldBase):
    """codex r4 CLOCK, as a GENERATED property (not more examples). The
    contract: last_activity comes only from ev["ts"] of real content events;
    ANY future epoch is hard-rejected (no slack — now+30 used to render
    age=0s); and clock_unknown (an unparseable or future ts) POISONS the
    room clock, checked BEFORE any valid sibling (one valid + one invalid
    used to render a confident age). INVARIANT over every generated input:
    no output claims a certainty the inputs do not support — age is a
    defensible number or ABSENT, never age=0s as a stand-in for unknown."""

    def _room_with_ts(self, ts_specs, tag):
        """A fresh meld whose stream events get their ts rewritten to the
        given specs (epoch int -> ISO, or a raw string), read through the
        rendered status line. `tag` makes each generated case a distinct
        room (room names embed the second-epoch, so same-second cases would
        otherwise collide). Returns (room, rendered line)."""
        room, _ = meld.invite("seat-b", "clock probe %s" % tag, seat="seat-a")
        meld.join(room, seat="seat-b")
        with open(meld.lifecycle_path(room), encoding="utf-8") as f:
            events = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(events), len(ts_specs),
                         "clock spec must cover the whole fixture stream")
        for ev, spec in zip(events, ts_specs):
            ev["ts"] = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(spec))
                        if isinstance(spec, int) else spec)
            ev["id"] = meld._event_id(room, ev["seq"])
        with open(meld.lifecycle_path(room), "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        out = "\n".join(meld.status(seat="seat-a"))
        line = next(l for l in out.splitlines() if room in l)
        return room, line

    def test_clock_invariant_over_generated_inputs(self):
        now = int(time.time())
        good = now - 3600                     # a defensible past clock (~1h)
        # the fixture stream length (invite + join = 2 events); specs are
        # built per-case to this length
        nprobe, _line = self._room_with_ts([good, good], "len")
        del nprobe
        shutil.rmtree(chat.chat_dir())
        os.makedirs(chat.chat_dir())
        meld._UNKNOWN.clear()
        n = 2
        cases = {}                            # name -> (ts_specs, expect)
        # BOUNDARIES: one event's ts swept across the future line, the rest
        # valid and past. ANY future ts poisons the room -> no age.
        for delta in (-1, 0, 1, 59, 60, 61):
            specs = [good] * n
            specs[0] = now + delta
            cases["boundary now%+d" % delta] = (specs, "age" if delta <= 0
                                                else "none")
        # MIXED: one bad event both FIRST and LAST (older and newer than
        # the good one); unparseable and future flavors. Always poison.
        for bad, flavor in (("not-a-clock", "raw"), (now + 7200, "future")):
            for pos in (0, n - 1):
                specs = [good] * n
                specs[pos] = bad
                cases["bad@%d %s" % (pos, flavor)] = (specs, "none")
        # DEGENERATE: all-invalid, all-future, all-valid.
        cases["all invalid"] = (["not-a-clock"] * n, "none")
        cases["all future"] = ([now + 7200] * n, "none")
        cases["all valid past"] = ([good] * n, "age")
        # SINGLE-EVENT: a lone invite with a future/invalid/valid clock.
        for spec, expect in ((now + 7200, "none"), ("not-a-clock", "none"),
                             (good, "age")):
            cases["single %s" % expect] = ((spec,), expect)
        for idx, (name, (specs, expect)) in enumerate(sorted(cases.items())):
            tag = "c%02d" % idx           # room slugs truncate at 24 chars —
            with self.subTest(case=name):  # the short tag keeps cases distinct
                # pin time for BOTH the fixture's ts specs AND the reducer's
                # `now`: the boundary sweep crosses the future line, so the
                # two clocks must agree to the second or now+1 leaks.
                with mock.patch.object(meld.time, "time", return_value=now):
                    if len(specs) == 1:
                        room, _l = meld.invite("seat-b", "lone %s" % tag,
                                               seat="seat-a")
                        with open(meld.lifecycle_path(room),
                                  encoding="utf-8") as f:
                            events = [json.loads(l) for l in f if l.strip()]
                        for ev in events:
                            spec = specs[0]
                            ev["ts"] = (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                      time.gmtime(spec))
                                        if isinstance(spec, int) else spec)
                            ev["id"] = meld._event_id(room, ev["seq"])
                        with open(meld.lifecycle_path(room), "w",
                                  encoding="utf-8") as f:
                            for ev in events:
                                f.write(json.dumps(ev) + "\n")
                        out = "\n".join(meld.status(seat="seat-a"))
                        line = next(l for l in out.splitlines() if room in l)
                    else:
                        _room, line = self._room_with_ts(list(specs), tag)
                if expect == "none":
                    self.assertNotIn("age=", line,
                                     "%s: an uncertain clock rendered an age: %s"
                                     % (name, line))
                else:
                    m = re.search(r"age=(\d+)([smhd])", line)
                    self.assertIsNotNone(m,
                                         "%s: a valid clock rendered no age: %s"
                                         % (name, line))
                    # the DEFENSIBLE number: supported by a real past ts in
                    # the stream (<= ~1h), never a number the inputs do not
                    # support. age=0s is DEFENSIBLE here only for the now+0
                    # boundary (a ts AT now is real); the unknown-stand-in
                    # shape (invalid/future) is the "none" arms above.
                    secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600,
                                              "d": 86400}[m.group(2)]
                    self.assertLessEqual(secs, 3660,
                                         "%s: an age no input ts supports: %s"
                                         % (name, line))
                    if (m.group(1), m.group(2)) == ("0", "s"):
                        self.assertEqual(name, "boundary now+0",
                                         "%s: age=0s with no now-ts input: %s"
                                         % (name, line))
                shutil.rmtree(chat.chat_dir())
                os.makedirs(chat.chat_dir())
                meld._UNKNOWN.clear()


if __name__ == "__main__":
    unittest.main()


class CouncilJudgesANamedSubjectTest(unittest.TestCase):
    """A council's invariant was never "a tip" — it is that EVERY MEMBER RULES
    ON THE SAME NAMED THING, FIXED BEFORE ANYONE SIGNALS.

    A re-gate made the tip required and named the hazard exactly: an optional tip
    "fell back to first-signal selection, which lets the fastest member choose
    the question every other member is answering". A question supplied AT
    CONVENE is bound just as hard. What is still refused is a subject nobody
    named."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-council-subject-")
        self.prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self.tmp

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- convene: exactly one named subject ----------------------------

    def test_a_QUESTION_convenes_a_council_with_no_artifact(self):
        reg, err = council.convene("q1", ["a", "b", "c"], epoch=1,
                                   question="should we ship 0.2 now?")
        self.assertIsNone(err)
        self.assertTrue(reg["tip"].startswith(council.QUESTION_SUBJECT_PREFIX))
        self.assertEqual(reg["question"], "should we ship 0.2 now?")

    def test_NEITHER_subject_is_still_refused(self):
        # POSITIVE CONTROL FIRST, same call: with a question it convenes, so
        # the refusal below is about the absent subject and not a dead verb.
        ok, e0 = council.convene("ctl", ["a"], epoch=1, question="a real one")
        self.assertIsNone(e0)
        self.assertTrue(ok["tip"])
        reg, err = council.convene("q2", ["a"], epoch=1)
        self.assertIsNone(reg)
        self.assertIn("NAMED subject", err)

    def test_BOTH_subjects_is_refused_because_a_council_judges_ONE_thing(self):
        reg, err = council.convene("q3", ["a"], epoch=1, tip="c" * 40,
                                   question="and also this?")
        self.assertIsNone(reg)
        self.assertIn("ONE subject", err)

    def test_the_QUESTION_SUBJECT_is_stable_and_whitespace_normalised(self):
        a = council.question_subject("ship  0.2   now?")
        b = council.question_subject("  ship 0.2 now?  ")
        self.assertTrue(a)
        self.assertEqual(a, b)
        self.assertNotEqual(a, council.question_subject("ship 0.3 now?"))
        self.assertEqual(council.question_subject("   "), "")

    # ---- signal: adopts the bound subject, refuses a different one ------

    def test_a_member_signals_a_QUESTION_council_without_any_sha(self):
        reg, err = council.convene("q4", ["a", "b"], epoch=1,
                                   question="ship 0.2 now?")
        self.assertIsNone(err)
        subject = reg["tip"]
        out, e = council.signal("q4", "a", "YES", None, "because reasons")
        self.assertIsNone(e)
        self.assertEqual(out["signals"]["a"]["tip"], subject)

    def test_naming_a_DIFFERENT_subject_is_still_refused(self):
        reg, _e = council.convene("q5", ["a", "b"], epoch=1,
                                  question="ship 0.2 now?")
        # POSITIVE CONTROL FIRST: adopting the bound subject WORKS on this
        # very council, so the refusal below is about the mismatch.
        _ok, e0 = council.signal("q5", "a", "YES", None, "ev")
        self.assertIsNone(e0)
        out, e = council.signal("q5", "b", "YES", "b" * 40, "ev")
        self.assertIsNone(out)
        self.assertIn("bound to", e)

    def test_an_ARTIFACT_council_is_BYTE_IDENTICAL_to_before(self):
        """The must-miss control for the whole lane: widening the subject must
        not move the artifact path by one byte, because evidence_digest is an
        idempotency key and a moved key turns a retry into a second vote."""
        tip = "c" * 40
        reg, err = council.convene("a1", ["a"], epoch=1, tip=tip)
        self.assertIsNone(err)
        self.assertEqual(reg["tip"], tip)          # the raw sha, not wrapped
        out, e = council.signal("a1", "a", "YES", tip, "ev")
        self.assertIsNone(e)
        # the digest over an artifact council covers exactly what it always did
        self.assertEqual(
            council.evidence_digest(tip, "YES", "ev", room="a1", epoch=1,
                                    seat="a"),
            council.evidence_digest(tip, "YES", "ev", room="a1", epoch=1,
                                    seat="a"))
        self.assertNotEqual(
            council.evidence_digest(tip, "YES", "ev", room="a1", epoch=1,
                                    seat="a"),
            council.evidence_digest(tip, "YES", "ev", room="a1", epoch=1,
                                    seat="b"))

    # ---- the land-door separation, pinned rather than assumed -----------

    def test_a_council_VERDICT_reaches_NO_land_door(self):
        """Today this is true BY CONSTRUCTION — council persists to its own
        per-room event ledger and the land door reads the DISPATCH ledger — and
        nothing tested it. A structural accident nobody pins is one import away
        from becoming false, so this converts it into a guarded invariant."""
        import helm.council as C
        import helm.landreq as L
        import helm.dispatches as D
        src = inspect.getsource(C)
        # MUST-HIT: the land door really does read the dispatch ledger.
        self.assertIn("dispatches", inspect.getsource(L))
        # MUST-MISS: council never writes into it, in either direction.
        self.assertNotIn("dispatches.", src)
        self.assertNotIn("landreq", src)
        # and no land-path module reads council storage
        self.assertNotIn("council.registry_path", inspect.getsource(L))
        self.assertNotIn("council.registry_path", inspect.getsource(D))

    # ---- the SURFACE: computed, recorded, and actually SHOWN -----------

    def test_the_SUBJECT_appears_on_the_surface_a_member_reads(self):
        """A council printed room, bar and members and never WHAT IT JUDGED.
        Survivable while the subject was always a sha someone had just pasted
        into chat; fatal for a question council, where the subject exists
        nowhere else."""
        council.convene("s1", ["a", "b", "c"], epoch=1,
                        question="should we ship 0.2 now?")
        lines = council.status_lines("s1")
        self.assertTrue(any("should we ship 0.2 now?" in l for l in lines),
                        "the question is recorded but never shown: %s" % lines)
        # MUST-HIT control on the same surface: an ARTIFACT council names its
        # artifact, so the assertion above is about the question specifically.
        council.convene("s2", ["a", "b", "c"], epoch=1, tip="c" * 40)
        art = council.status_lines("s2")
        self.assertTrue(any("cccccccccccc" in l for l in art), art)

    def test_a_QUESTION_council_does_not_tell_members_to_pass_a_sha(self):
        """The sign line said --tip <sha> unconditionally. For a question
        council that is an instruction no member can follow, and it became
        wrong the moment tip-less councils existed."""
        council.convene("s3", ["a", "b"], epoch=1, question="ship now?")
        sign = [l for l in council.status_lines("s3") if "helm chat verdict" in l]
        self.assertTrue(sign)
        self.assertNotIn("--tip", sign[0])
        # MUST-HIT: an artifact council STILL offers --tip, so the absence
        # above is about the council kind and not a deleted flag.
        council.convene("s4", ["a", "b"], epoch=1, tip="d" * 40)
        sign2 = [l for l in council.status_lines("s4") if "helm chat verdict" in l]
        self.assertTrue(sign2)
        self.assertIn("--tip", sign2[0])


class SayTakesTheSafeRouteTest(MeldBase):
    """THE ROUTE THE ARGV GUARD ALREADY PRESCRIBES FOR THIS VERB.

    `chat meld say` is in `chat._BODY_VERBS`, so helm refuses a backticked
    argv body here and tells the author to use a quoted-delimiter heredoc.
    The door took text from argv only, so that prescription named a route
    that did not exist and the author's next move was the dangerous one.

    A REAL PIPE, NOT A StringIO. `resolve_one_body` asks whether an fd has
    DATA waiting, which needs a real fileno; a StringIO would answer the
    readiness question from the wrong object and these arms would pass over a
    door that never reads anything.
    """

    @contextlib.contextmanager
    def piped_stdin(self, text):
        r, w = os.pipe()
        os.write(w, text.encode("utf-8"))
        os.close(w)                      # EOF, so the body read terminates
        f = os.fdopen(r, "r")
        try:
            with mock.patch.object(sys, "stdin", f):
                yield
        finally:
            f.close()

    def posted(self, room):
        rows, _total = chat.read(room)
        return [r["text"] for r in rows]

    def test_a_heredoc_chunk_REACHES_THE_ROOM(self):
        room, _epoch = self.open_meld()
        before = len(self.posted(room))
        with self.piped_stdin("the chunk that came from stdin\n"):
            rc = meld.cmd(["say", room, "--marker", "YIELD",
                           "--seat", "seat-a"])
        self.assertEqual(rc, 0)
        texts = self.posted(room)
        self.assertEqual(len(texts), before + 1, "no row was posted")
        self.assertIn("the chunk that came from stdin", texts[-1])
        self.assertIn("[YIELD]", texts[-1])

    def test_the_chunk_ARRIVES_LITERAL(self):
        """THE WHOLE POINT, and it is why argv is not merely inconvenient.

        A body carrying backticks and $( ) is executed by the SHELL when it is
        an argv word — helm never sees what the author typed. Through stdin the
        payload is never a shell word, so the room gets the characters.
        """
        room, _epoch = self.open_meld()
        hazard = "the guard refuses `date` and $(whoami) in an argv body"
        with self.piped_stdin(hazard + "\n"):
            rc = meld.cmd(["say", room, "--marker", "HOLD",
                           "--seat", "seat-a"])
        self.assertEqual(rc, 0)
        self.assertIn(hazard, self.posted(room)[-1])

    def test_TWO_BODIES_IS_A_REFUSAL_AND_POSTS_NOTHING(self):
        """Choosing either one would report a chunk sent while the other never
        left the author's shell."""
        room, _epoch = self.open_meld()
        before = len(self.posted(room))
        with self.piped_stdin("the piped body\n"):
            rc = meld.cmd(["say", room, "--marker", "YIELD",
                           "the positional body", "--seat", "seat-a"])
        self.assertEqual(rc, 2)
        texts = self.posted(room)
        self.assertEqual(len(texts), before, "a refusal still posted a row")
        for body in ("the piped body", "the positional body"):
            self.assertNotIn(body, "\n".join(texts))

    def test_NO_BODY_ANYWHERE_STILL_REFUSES(self):  # noqa: VACUOUS_ASSERTION — the refusal rc and the unchanged row count are both unconditional positives on the observables this arm is about; the absence assertion is the point (a bare marker must post nothing)
        """MUST-MISS. Adding a stdin leg must not turn a bare marker into an
        accepted empty chunk, and must not hang waiting on an fd with nothing
        on it — the failure that reads to every observer as the author simply
        not typing.
        """
        room, _epoch = self.open_meld()
        before = len(self.posted(room))
        r, w = os.pipe()
        os.close(w)                      # EOF immediately: ready, but empty
        f = os.fdopen(r, "r")
        try:
            with mock.patch.object(sys, "stdin", f):
                rc = meld.cmd(["say", room, "--marker", "YIELD",
                               "--seat", "seat-a"])
        finally:
            f.close()
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.posted(room)), before)
