#!/usr/bin/env python3
"""helm meld — the mindmeld preset. Hermetic: HELM_CHAT_DIR + HELM_HOME are
tmp dirs, HELM_CHAT_NODE_URL set-but-empty kills the signed transport,
ambient session ids scrubbed (test_seats.py's exact envelope). Pins the meld
preset scars (epoch fence, F1 control echoes, F2 fail-closed self-skip,
room×actor state, clip-proof invite head) + the helm-native laws (act-moment
mentions only, latency-pure sign=False, bounds as behavior)."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
        os.environ["HELM_CHAT_OWNER_NAMES"] = "owner"

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
        # clip-proof head (inverted from a live incident): the join command must sit
        # inside the first 200 BYTES — the delivery clip can never eat it.
        head = seats._clip(seats._scrub(inv))
        self.assertIn("helm chat meld join %s" % room, head)
        self.assertTrue(inv.startswith("@seat-b "))
        st = meld.state(room, "seat-a")
        self.assertEqual((st["role"], st["status"], st["exchanges"]),
                         ("convener", "invited", 0))
        self.assertIn(room, chat.list_rooms())          # owner surface: the
        # room shows in rooms/web channel list the moment it exists

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
        # a READY that lands silently strands GO forever (a live incident)
        st = meld.state(room, "seat-b")
        self.assertEqual((st["role"], st["status"], st["peer"]),
                         ("joiner", "active", "seat-a"))

    def test_join_without_seed_refuses(self):
        chat.post("just chatter", room="meld-1-x", who="someone")
        with self.assertRaises(SystemExit):
            meld.join("meld-1-x", seat="seat-b")

    def test_state_is_room_x_actor(self):
        """The meld live-dogfood REFUTE: both actors share one chat dir;
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

    def test_done_status_transitions(self):
        """After your own DONE, recv is the COUNTERSIGN WATCH (live-fire
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
        double-command an agent replays after compaction/resume (a dual-gate review finding)."""
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
        self.assertIn("non-peer", "\n".join(lines))   # noted, never silent
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
        rc = chat.cmd_chat(["council", "invite", "seat-b", "wire", "format"])
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
        # the empty slate answers in the TYPED voice too (the one meld
        # surface that used to hardcode "helm meld:")
        self.assertIn("helm standup: no live melds for seat seat-z",
                      out.getvalue())

    def test_invite_wait_collapses_invite_and_first_recv(self):
        """--wait = invite + recv in one call (every convener's literal next
        command, live-fire want)."""
        os.environ["HELM_MELD_RECV_TIMEOUT_S"] = "0"
        rc = meld.cmd(["invite", "seat-b", "wire", "--wait", "--seat", "seat-a"])
        self.assertEqual(rc, meld.EXIT_BOUND)         # blocked, bounded out
        room = next(r for r in chat.list_rooms() if r.startswith("meld-"))
        self.assertEqual(meld.state(room, "seat-a")["status"], "invited")


class TestSelfSeat(MeldBase):
    def test_self_seat_survives_a_deleted_cwd(self):
        """A review finding (probe A7): _self_seat's bare
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


if __name__ == "__main__":
    unittest.main()
