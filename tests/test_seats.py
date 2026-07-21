#!/usr/bin/env python3
"""helm seats — the delivery lane (meld-half port, codex-hardened round).
Hermetic: HELM_CHAT_DIR + HELM_HOME are tmp dirs, HELM_CHAT_NODE_URL
set-but-empty kills the signed transport, HELM_CHAT_OWNER_NAMES pinned, the
ambient CLAUDE/CODEX session ids scrubbed. Hook legs are fed synthetic hook
JSON and captured AT THE FD level — the one-write emit law (codex H7) writes
fd 1 directly, bypassing sys.stdout."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, home, pk, record, seats, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_DELIVER",
            "HELM_STOP_GUARD", "HELM_STOP_GUARD_INBOX",
            "HELM_STOP_GUARD_CLAIMS", "HELM_STOP_GUARD_INDEX",
            "HELM_STOP_GUARD_WHISPER",
            "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            # auto_name reads the ambient model/harness marks — scrub them or
            # a test run inside a live harness computes a different family
            # (CLAUDE_CODE_SESSION_ID is the REAL claude-code var — leaving it
            # unscrubbed let this very session's id leak into whoname/_family)
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL")


class SeatsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seats-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "david"
        # hermetic by law: the stop-guard's silent index-cap leg targets the
        # adopted claude memory dir — point it at tmp so no test can ever
        # touch the live MEMORY.md.
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cmd(self, verb, args=(), stdin=None, room="main"):
        """Plain-verb runner (print-based legs)."""
        out, err = io.StringIO(), io.StringIO()
        fake = types.SimpleNamespace(buffer=io.BytesIO(stdin)) if stdin is not None else None
        ctx = mock.patch.object(sys, "stdin", fake) if fake else contextlib.nullcontext()
        with ctx, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seats.cmd(verb, list(args), room)
        return rc, out.getvalue(), err.getvalue()

    def cmd_fd(self, verb, args=(), stdin=None, room="main"):
        """Hook-leg runner: captures FD 1 itself — the emit law writes there
        directly (one unbuffered os.write), invisible to redirect_stdout."""
        fake = types.SimpleNamespace(buffer=io.BytesIO(stdin or b"{}"))
        r, w = os.pipe()
        saved = os.dup(1)
        os.dup2(w, 1)
        os.close(w)
        try:
            with mock.patch.object(sys, "stdin", fake):
                rc = seats.cmd(verb, list(args), room)
            sys.stdout.flush()
        finally:
            os.dup2(saved, 1)
            os.close(saved)
        chunks = []
        while True:
            b = os.read(r, 65536)
            if not b:
                break
            chunks.append(b)
        os.close(r)
        return rc, b"".join(chunks).decode("utf-8")


class AddressingTest(SeatsBase):
    def test_mention_broadcast_and_noise(self):
        row = lambda frm, text, **kw: dict({"ts": "t", "from": frm, "text": text}, **kw)
        self.assertTrue(seats.deliverable(row("x", "hey @alice look"), "alice"))
        self.assertTrue(seats.deliverable(row("x", "@ALL standup"), "alice"))
        # noise law: agent chatter without a mention does NOT deliver
        self.assertFalse(seats.deliverable(row("bob", "about @alicein"), "alice"))
        self.assertFalse(seats.deliverable(row("alice", "@alice self"), "alice"))
        self.assertFalse(seats.deliverable({"ts": "t", "from": "david",
                                            "react": "🔥", "tts": "t", "tfrom": "x"},
                                           "alice"))
        self.assertFalse(seats.deliverable(row("x", "ping @alice-2"), "alice"))

    def test_owner_rail_post_no_longer_auto_wakes(self):
        """Owner steer 2026-07-21: owner-rail posts are no longer a wake class.
        A server-stamped owner post with no @mention does NOT reach a seat (was:
        the 'owner rule' delivered a web/tui-stamped row); an owner post wakes a
        seat only via an @mention or the seat's home room. OWNER_RAILS/owner_names
        still gate owner IDENTITY for the unread rail + console, just not the
        beacon."""
        row = lambda **kw: dict({"ts": "t", "from": "david",
                                 "text": "no mention"}, **kw)
        self.assertFalse(seats.deliverable(row(), "alice"))              # spoofable CLI
        self.assertFalse(seats.deliverable(row(origin="web"), "alice"))  # owner rail: no wake now
        self.assertFalse(seats.deliverable(row(origin="tui"), "alice"))
        self.assertFalse(seats.deliverable(row(origin="cli"), "alice"))
        # an owner post that @mentions the seat still wakes it
        self.assertTrue(seats.deliverable(
            {"ts": "t", "from": "david", "text": "@alice go", "origin": "web"},
            "alice"))

    def test_owner_names_env_override(self):
        os.environ["HELM_CHAT_OWNER_NAMES"] = "boss, Chief"
        self.assertEqual(seats.owner_names(), {"boss", "chief"})


class JoinTest(SeatsBase):
    def test_join_writes_roster_and_baselines_cursor_at_join(self):
        chat.post("history @alice", who="old")
        seat, line = seats.join(session="sess-1234", cwd="/tmp/projx", seat="alice")
        self.assertEqual(seat, "alice")
        self.assertIn("seat 'alice'", line)
        self.assertIn("Monitor", line)   # the idle-beacon RSH pointer (M11)
        row = seats.roster()["alice"]
        self.assertEqual(row["session"], "sess-1234")
        self.assertEqual(row["project"], "projx")
        # pre-join backlog never floods…
        self.assertIsNone(seats.deliver(seat="alice"))
        # …but a message between JOIN and the FIRST boundary delivers (H5.5)
        chat.post("@alice early word", who="bob")
        self.assertIn("early word", seats.deliver(seat="alice"))

    def test_join_hook_json_leg(self):
        payload = json.dumps({"session_id": "s-77", "cwd": "/tmp/p"}).encode()
        rc, out = self.cmd_fd("join", ["--hook-json", "--seat", "zed"], stdin=payload)
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("seat 'zed'", d["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(seats.seat_for_session("s-77"), "zed")

    def test_join_fail_open_on_garbage_stdin(self):
        rc, out = self.cmd_fd("join", ["--hook-json"], stdin=b"not json{{")
        self.assertEqual(rc, 0)   # never raises, never shapes a session start

    def test_join_line_mandates_arming_the_idle_wake_beacon(self):
        _seat, line = seats.join(seat="codex", cwd="/tmp/p")
        # a mandatory FIRST action, not a suggestion
        self.assertIn("MANDATORY", line)
        # the exact beacon-arm command carries the RESOLVED seat name + --follow
        self.assertIn("helm chat wait --seat codex --follow", line)
        self.assertIn("Monitor(", line)
        self.assertIn("persistent: true", line)
        # the honest enforcement note: nothing external can wake a PTY agent
        self.assertIn("native-wake-only-agent-armed", line)

    def test_seat_joins_roster_under_its_family_name(self):
        # launch_line exports HELM_CHAT_NAME=<family>; the join hook keys the
        # roster on it (derive_seat) — so @codex reaches the seat, not agent-xxxx
        os.environ["HELM_CHAT_NAME"] = "codex"
        os.environ["CLAUDE_SESSION_ID"] = "sess-abcdef12"
        seat, _line = seats.join(cwd="/tmp/p")     # no explicit --seat
        self.assertEqual(seat, "codex")
        self.assertIn("codex", seats.roster())
        self.assertNotIn("agent-sess-abc", " ".join(seats.roster()))


class DeliverTest(SeatsBase):
    def seat_up(self, seat="alice"):
        seats.join(session="s-" + seat, cwd="/tmp/p", seat=seat)

    def test_one_per_boundary_with_waiting_count(self):
        self.seat_up()
        chat.post("@alice first", who="bob")
        chat.post("@alice second", who="bob")
        line = seats.deliver(seat="alice")
        self.assertIn("bob: @alice first", line)
        self.assertIn("(+1 waiting", line)
        line2 = seats.deliver(seat="alice")
        self.assertIn("@alice second", line2)
        self.assertNotIn("waiting", line2)
        self.assertIsNone(seats.deliver(seat="alice"))

    def test_owner_rail_post_does_not_wake_without_mention(self):
        """Owner steer 2026-07-21: an owner-rail post with no @mention no longer
        wakes an un-homed seat; only a mention (or the home room) does."""
        self.seat_up()
        chat.post("course correction", who="david", origin="web")
        self.assertIsNone(seats.deliver(seat="alice"))
        chat.post("@alice course correction", who="david", origin="web")
        self.assertIn("course correction", seats.deliver(seat="alice"))

    def test_cli_owner_name_does_not_owner_deliver(self):
        self.seat_up()
        chat.post("i am totally the owner", who="david")   # no rail stamp
        self.assertIsNone(seats.deliver(seat="alice"))

    def test_chatter_advances_cursor_to_eof(self):
        self.seat_up()
        chat.post("agent noise", who="bob")
        chat.post("more noise", who="carol")
        self.assertIsNone(seats.deliver(seat="alice"))
        cur = seats._cursor("main", "alice")
        self.assertEqual(cur["off"], os.path.getsize(chat.room_path("main")))
        st = os.stat(chat.room_path("main"))
        self.assertEqual((cur["dev"], cur["ino"]), (st.st_dev, st.st_ino))

    def test_scrub_and_clip(self):
        self.seat_up()
        hostile = "@alice \x1b[31mred\x1b[0m line1\nline2 sep " + "x" * 400
        chat.post(hostile, who="bob")
        line = seats.deliver(seat="alice")
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\n", line)
        self.assertNotIn(" ", line)
        payload = line.split("bob: ", 1)[1]
        self.assertLessEqual(len(payload.encode("utf-8")), seats.MAX_BYTES + 8)
        self.assertIn("…", line)

    def test_kill_switch(self):
        self.seat_up()
        chat.post("@alice ping", who="bob")
        os.environ["HELM_CHAT_DELIVER"] = "0"
        self.assertIsNone(seats.deliver(seat="alice"))

    def test_marker_untouched_by_delivery(self):
        self.seat_up()
        chat.post("@alice steer", who="david", origin="web")  # a mention delivers
        chat.mark_owner_unread("main")
        self.assertIsNotNone(seats.deliver(seat="alice"))
        self.assertTrue(os.path.exists(chat.marker_path("main")))

    def test_inode_change_resets_and_suppresses_replayed_rows(self):
        """Rotation/replacement (inode change) resets to 0; rows up to and
        including the cursor's last row id are suppressed, later ones
        deliver — duplicates acceptable, loss is not (H5)."""
        self.seat_up()
        chat.post("@alice one", who="bob")
        self.assertIn("one", seats.deliver(seat="alice"))
        p = chat.room_path("main")
        with open(p, encoding="utf-8") as f:
            content = f.read()
        os.remove(p)                       # new inode, SAME byte content
        pk.atomic_write(p, content)
        self.assertIsNone(seats.deliver(seat="alice"))   # rid suppression
        chat.post("@alice two", who="bob")
        self.assertIn("two", seats.deliver(seat="alice"))

    def test_same_size_replacement_detected(self):
        """A replacement of EQUAL size must not hide behind a size check —
        the inode is the identity (H5.1)."""
        self.seat_up()
        chat.post("agent noise", who="bob")
        self.assertIsNone(seats.deliver(seat="alice"))   # cursor at EOF
        p = chat.room_path("main")
        with open(p, encoding="utf-8") as f:
            old = f.read()
        row = json.loads(old.splitlines()[0])
        row["text"] = "@alice YO!"
        row["from"] = "bob"
        row["id"] = "f" * 12                # a REAL writer mints a fresh id
        new_line = json.dumps(row, ensure_ascii=False)
        new_line = new_line + " " * (len(old) - len(new_line) - 1)  # pad = same size
        os.remove(p)
        pk.atomic_write(p, new_line + "\n")
        self.assertEqual(os.path.getsize(p), len(old.encode()))
        line = seats.deliver(seat="alice")
        self.assertIn("YO!", line)

    def test_partial_trailing_line_never_consumed(self):
        self.seat_up()
        chat.post("@alice whole", who="bob")
        with open(chat.room_path("main"), "a", encoding="utf-8") as f:
            f.write('{"ts": "x", "from": "bob", "text": "@alice torn')  # no \n
        line = seats.deliver(seat="alice")
        self.assertIn("whole", line)
        self.assertNotIn("torn", line)
        self.assertIsNone(seats.deliver(seat="alice"))   # partial still parked
        with open(chat.room_path("main"), "a", encoding="utf-8") as f:
            f.write('"}\n')                              # writer finishes the row
        self.assertIn("torn", seats.deliver(seat="alice"))

    def test_emit_before_commit_at_least_once(self):
        """H7: the cursor must NOT advance if emit never completed — a kill
        between select and output re-delivers next boundary."""
        self.seat_up()
        chat.post("@alice precious", who="bob")
        with self.assertRaises(RuntimeError):
            seats.deliver(seat="alice",
                          emit=mock.Mock(side_effect=RuntimeError("killed")))
        self.assertIn("precious", seats.deliver(seat="alice"))   # re-delivered

    def test_slug_colliding_seats_never_share_state(self):
        """Codex B1's exact reproduction: pk.slug('api.a') == pk.slug('api-a')
        yet they are distinct address tokens — each must keep its own
        cursor/seen state, neither may consume the other's rows."""
        self.assertNotEqual(seats.cursor_path("main", "api.a"),
                            seats.cursor_path("main", "api-a"))
        self.assertNotEqual(seats.seen_path("api.a"), seats.seen_path("api-a"))
        seats.join(seat="api.a", cwd="/tmp/p")
        seats.join(seat="api-a", cwd="/tmp/p")
        chat.post("@api.a first", who="owner")
        chat.post("@api-a second", who="owner")
        self.assertIn("@api.a first", seats.deliver(seat="api.a"))
        self.assertIsNone(seats.deliver(seat="api.a"))   # advances ITS cursor only
        self.assertIn("@api-a second", seats.deliver(seat="api-a"))

    def test_conamed_sessions_both_receive_the_mention(self):
        """G-cursor-persession: two live sessions sharing one HELM_CHAT_NAME
        must BOTH see an @mention (fan-out) — the seat-only cursor let
        whichever boundary fired first race-consume it for the sibling."""
        seats.join(session="s-one", seat="fable", cwd="/tmp/p")
        seats.join(session="s-two", seat="fable", cwd="/tmp/p")
        chat.post("@fable ship it", who="bob")
        self.assertIn("ship it", seats.deliver(session="s-one", seat="fable"))
        self.assertIn("ship it", seats.deliver(session="s-two", seat="fable"))
        # each consumed its OWN cursor — no re-nudge, no cross-consume
        self.assertIsNone(seats.deliver(session="s-one", seat="fable"))
        self.assertIsNone(seats.deliver(session="s-two", seat="fable"))
        # the roster row stays seat-keyed (one row) and resolves BOTH sessions
        self.assertEqual(len([s for s in seats.roster() if s == "fable"]), 1)
        self.assertEqual(seats.seat_for_session("s-one"), "fable")
        self.assertEqual(seats.seat_for_session("s-two"), "fable")

    def test_conamed_join_order_independent_of_hook_seat_resolution(self):
        """The hook passes only session_id — the OLDER co-named session must
        still resolve to the shared seat after a newer join overwrote
        row['session'] (the sessions list is the addressing memory)."""
        seats.join(session="s-old", seat="fable", cwd="/tmp/p")
        seats.join(session="s-new", seat="fable", cwd="/tmp/p")
        chat.post("@fable hello", who="bob")
        # no --seat: exactly what the PostToolUse hook can supply
        self.assertIn("hello", seats.deliver(session="s-old"))

    def test_session_cursor_inherits_seat_baseline_on_upgrade(self):
        """A pre-split install tracked the seat-level cursor; the first
        session-keyed boundary must deliver from THAT baseline, not skip to
        EOF (loss is the one forbidden outcome)."""
        seats.join(seat="alice", cwd="/tmp/p")              # seat-level cursor
        chat.post("@alice queued before upgrade", who="bob")
        line = seats.deliver(session="s-later", seat="alice")
        self.assertIn("queued before upgrade", line)

    def test_wait_shares_the_sessions_cursor(self):
        """An ambient-session wait must consume the SAME cursor as that
        session's boundary hook — no double-nudge for one session."""
        seats.join(session="s-w", seat="alice", cwd="/tmp/p")
        chat.post("@alice once", who="bob")
        line = seats.wait(seat="alice", session="s-w", timeout=1, poll=0.01)
        self.assertIn("once", line)
        self.assertIsNone(seats.deliver(session="s-w", seat="alice"))

    def test_unknown_session_self_heals_roster_with_meaningful_name(self):
        chat.post("noise", who="bob")
        self.assertIsNone(seats.deliver(session="brand-new-session",
                                        cwd="/tmp/projx"))
        # G-stable-names: the self-heal binds a project+family name, not hex
        self.assertIn("projx-agent", seats.roster())
        self.assertNotIn("agent-brand-ne", seats.roster())
        chat.post("@projx-agent go", who="bob")
        # a later cwd-less boundary still resolves the SAME seat (roster-bound)
        self.assertIn("go", seats.deliver(session="brand-new-session"))

    def test_deliver_hook_json_one_write_shape_and_fail_open(self):
        self.seat_up()
        chat.post("@alice hi", who="bob")
        sid = seats.roster()["alice"]["session"]
        payload = json.dumps({"session_id": sid}).encode()
        rc, out = self.cmd_fd("deliver", ["--hook-json"], stdin=payload)
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("bob: @alice hi", d["hookSpecificOutput"]["additionalContext"])
        with mock.patch.object(seats, "deliver", side_effect=RuntimeError("boom")):
            rc, out = self.cmd_fd("deliver", ["--hook-json"], stdin=b"{}")
        self.assertEqual((rc, out), (0, ""))

    def test_unchanged_room_never_rewrites_shared_state(self):
        """Freeze bar 6: the fast path touches only the seat's own .seen."""
        self.seat_up()
        chat.post("noise", who="bob")
        self.assertIsNone(seats.deliver(seat="alice"))
        before = os.stat(seats.roster_path()).st_mtime_ns
        for _ in range(3):
            self.assertIsNone(seats.deliver(seat="alice"))
        self.assertEqual(os.stat(seats.roster_path()).st_mtime_ns, before)


class WaitTest(SeatsBase):
    def test_wait_returns_pending_and_advances_cursor(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice now", who="bob")
        line = seats.wait(seat="alice", timeout=1, poll=0.01)
        self.assertIn("@alice now", line)
        self.assertIsNone(seats.deliver(seat="alice"))  # no re-nudge

    def test_wait_timeout_rc1(self):
        seats.join(seat="alice", cwd="/tmp/p")
        rc, _out, _err = self.cmd("wait", ["--seat", "alice", "--timeout", "0.05"])
        self.assertEqual(rc, 1)

    def test_wait_follow_streams_every_match_and_never_exits_on_first(self):
        seats.join(seat="alice", cwd="/tmp/p")     # baselines the cursor at join
        chat.post("@alice one", who="bob")
        chat.post("just agent noise", who="bob")   # non-matching — must be skipped
        chat.post("@alice two", who="bob")
        captured = []
        line = seats.wait(seat="alice", follow=True, timeout=0.15, poll=0.01,
                          emit=captured.append)
        self.assertIsNone(line)                    # --follow returns only on timeout
        self.assertEqual(len(captured), 2)         # BOTH matches — not just the first
        self.assertIn("@alice one", captured[0])
        self.assertIn("@alice two", captured[1])
        self.assertFalse(any("agent noise" in c for c in captured))  # non-match not emitted

    def test_wait_follow_non_matching_row_emits_nothing(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("chatter with no mention", who="bob")
        captured = []
        line = seats.wait(seat="alice", follow=True, timeout=0.1, poll=0.01,
                          emit=captured.append)
        self.assertIsNone(line)
        self.assertEqual(captured, [])

    def test_wait_any_sees_only_rows_after_arming(self):
        import threading
        chat.post("pre-existing", who="bob")
        line = seats.wait(any_row=True, timeout=0.2, poll=0.01)
        self.assertIsNone(line)
        t = threading.Timer(0.05, chat.post, args=("newest",),
                            kwargs={"who": "carol"})
        t.start()
        try:
            line = seats.wait(any_row=True, timeout=2, poll=0.01)
        finally:
            t.join()
        self.assertIn("newest", line)


class MultiRoomTest(SeatsBase):
    """Slice 5 (multi-room deliver) + its beacon half: an @mention in ANY room
    must reach the seat — the owner's live helm-dogfood '@opus-integrator …'
    post woke nothing because both the beacon and the boundary lane were
    main-scoped (2026-07-21). (Owner-rail posts are no longer a wake class as of
    the same day's owner steer — mentions + home room only.)"""

    def test_mention_in_never_joined_room_wakes_the_beacon(self):
        """THE bug's reproduction: seat x's only activity is in team-x, a
        room it never joined — its `wait --follow` beacon must still stream
        the mention, and the same session's boundary must not re-nudge."""
        seats.join(session="s-x", seat="x", cwd="/tmp/p")
        chat.post("@x cross-room ping", who="bob", room="team-x")
        captured = []
        line = seats.wait(seat="x", session="s-x", follow=True,
                          timeout=0.15, poll=0.01, emit=captured.append)
        self.assertIsNone(line)                # --follow returns only on timeout
        self.assertEqual(len(captured), 1)
        self.assertIn("cross-room ping", captured[0])
        self.assertIn("#team-x", captured[0])  # the wake names the channel
        # consumed on THIS session's per-room cursor — no double delivery
        self.assertIsNone(seats.deliver_any(session="s-x", seat="x"))

    def test_boundary_hook_delivers_cross_room(self):
        seats.join(session="s-h", seat="hx", cwd="/tmp/p")
        chat.post("@hx in the side channel", who="bob", room="side")
        payload = json.dumps({"session_id": "s-h"}).encode()
        rc, out = self.cmd_fd("deliver", ["--hook-json"], stdin=payload)
        self.assertEqual(rc, 0)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("in the side channel", ctx)
        self.assertIn("#side", ctx)
        rc, out = self.cmd_fd("deliver", ["--hook-json"], stdin=payload)
        self.assertEqual(out, "")              # nothing left — no re-nudge

    def test_owner_post_wakes_only_via_home_or_mention(self):
        """Owner steer 2026-07-21: owner-rail posts no longer auto-wake — not in
        a side room, and no longer in main either. An un-homed seat hears an
        owner post ONLY if @mentioned; a seat HOMED to a room hears owner posts
        there (home = full surface). (Was: owner reach was {home, main}.)"""
        seats.join(session="s-o", seat="oz", cwd="/tmp/p")
        chat.post("side-room note", who="david", origin="web", room="announce")
        self.assertIsNone(seats.deliver_any(session="s-o", seat="oz"))
        chat.post("all hands", who="david", origin="web")   # main: no longer wakes un-homed
        self.assertIsNone(seats.deliver_any(session="s-o", seat="oz"))
        chat.post("@oz ping", who="david", origin="web")    # …but a mention does
        self.assertIn("ping", seats.deliver_any(session="s-o", seat="oz"))
        # …and a seat HOMED to the side room hears the owner there
        seats.join(session="s-an", seat="anna", cwd="/tmp/p", room="announce")
        chat.post("announce word", who="david", origin="web", room="announce")
        self.assertIn("announce word",
                      seats.deliver_any(session="s-an", seat="anna"))

    def test_primary_room_first_one_nudge_per_boundary_no_loss(self):
        """Main outranks the side rooms, one row per boundary, and nothing
        double-delivers or vanishes across the scan order."""
        seats.join(session="s-p", seat="p", cwd="/tmp/p")
        chat.post("@p in team", who="bob", room="team-x")
        chat.post("@p in main", who="bob")
        self.assertIn("in main", seats.deliver_any(session="s-p", seat="p"))
        self.assertIn("in team", seats.deliver_any(session="s-p", seat="p"))
        self.assertIsNone(seats.deliver_any(session="s-p", seat="p"))

    def test_homed_seat_lives_in_its_room_and_still_hears_main(self):
        """Slice 3 (team-room homing) composes with the multi-room deliver:
        HELM_CHAT_ROOM homes the no---room verbs the hooks call — join +
        deliver run in the team room — while deliver_any still wakes the
        homed seat on an @mention back in main."""
        os.environ["HELM_CHAT_ROOM"] = "team-x"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(["join", "--seat", "tm"]), 0)
        self.assertIn("in room team-x", out.getvalue())
        chat.post("@tm team word", who="bob", room="team-x")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(["deliver", "--seat", "tm"]), 0)
        self.assertIn("team word", out.getvalue())
        self.assertIn("#team-x", out.getvalue())   # delivered IN the home room
        chat.post("@tm back in main", who="bob")   # cross-room mention
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(["deliver", "--seat", "tm"]), 0)
        self.assertIn("back in main", out.getvalue())

    def test_cross_room_waiting_pointer_names_the_room(self):
        seats.join(session="s-w2", seat="w2", cwd="/tmp/p")
        chat.post("@w2 one", who="bob", room="dog")
        chat.post("@w2 two", who="bob", room="dog")
        line = seats.deliver_any(session="s-w2", seat="w2")
        self.assertIn("(+1 waiting", line)
        self.assertIn("helm chat read --room dog", line)

    def test_join_baselines_existing_rooms_pre_join_backlog_never_floods(self):
        chat.post("@z ancient word", who="bob", room="dust")   # before z joins
        seats.join(session="s-z", seat="z", cwd="/tmp/p")
        self.assertIsNone(seats.deliver_any(session="s-z", seat="z"))
        chat.post("@z fresh word", who="bob", room="dust")     # post-join news
        self.assertIn("fresh word", seats.deliver_any(session="s-z", seat="z"))

    def test_untracked_seat_never_backfills_foreign_history(self):
        """A seat with no cursor anywhere (reaped / pre-install self-heal)
        EOF-baselines every room — the backfill law is for TRACKED seats
        meeting a room born after their join, never a backlog flood."""
        chat.post("@ghost old word", who="bob", room="attic")
        self.assertIsNone(seats.deliver_any(session="s-g", seat="ghost"))
        chat.post("@ghost new word", who="bob", room="attic")
        self.assertIn("new word", seats.deliver_any(session="s-g", seat="ghost"))

    def test_follow_streams_matches_from_multiple_rooms(self):
        seats.join(session="s-m", seat="m", cwd="/tmp/p")
        chat.post("@m alpha", who="bob")                       # main
        chat.post("@m beta", who="bob", room="team-x")
        chat.post("chatter, no mention", who="bob", room="team-x")
        captured = []
        seats.wait(seat="m", session="s-m", follow=True, timeout=0.15,
                   poll=0.01, emit=captured.append)
        self.assertEqual(len(captured), 2)
        both = " || ".join(captured)
        self.assertIn("alpha", both)
        self.assertIn("beta", both)
        self.assertNotIn("chatter", both)                      # noise law holds

    def test_conamed_sessions_fan_out_cross_room(self):
        """Per (seat, room, session) cursors: BOTH co-named sessions see the
        side-room mention, each exactly once."""
        seats.join(session="s-one", seat="fab", cwd="/tmp/p")
        seats.join(session="s-two", seat="fab", cwd="/tmp/p")
        chat.post("@fab ship it", who="bob", room="team-fab")
        self.assertIn("ship it", seats.deliver_any(session="s-one", seat="fab"))
        self.assertIn("ship it", seats.deliver_any(session="s-two", seat="fab"))
        self.assertIsNone(seats.deliver_any(session="s-one", seat="fab"))
        self.assertIsNone(seats.deliver_any(session="s-two", seat="fab"))

    def test_stop_guard_blocks_on_cross_room_pending(self):
        seats.join(session="s-sg", seat="sg", cwd="/tmp/p")
        chat.post("@sg review the team-x branch", who="bob", room="team-x")
        rc, _o, err = self.cmd(
            "stop-guard", ["--hook-json", "--seat", "sg"],
            stdin=json.dumps({"session_id": "s-sg"}).encode())
        self.assertEqual(rc, 2)
        self.assertIn("undelivered", err)
        self.assertIn("[#team-x]", err)               # the row names its room
        self.assertIn("review the team-x branch", err)
        # the gate never consumed it — the lane still delivers afterwards
        self.assertIn("review the team-x branch",
                      seats.deliver_any(session="s-sg", seat="sg"))

    def test_roster_report_counts_cross_room_pending(self):
        seats.join(session="s-rr", seat="rr", cwd="/tmp/p")
        chat.post("@rr main one", who="bob")
        chat.post("@rr dogfood two", who="bob", room="helm-dogfood")
        rep = seats.roster_report("main")
        s = [x for x in rep["seats"] if x["seat"] == "rr"][0]
        self.assertEqual(s["pending"], 2)
        # the report moved nothing — both rows still deliver, in scan order
        self.assertIn("main one", seats.deliver_any(session="s-rr", seat="rr"))
        self.assertIn("dogfood two", seats.deliver_any(session="s-rr", seat="rr"))

    def test_scan_rooms_bounded_primary_first_newest_win(self):
        chat.post("seed", who="bob")                           # main exists
        n = seats.ROOM_SCAN_CAP + 4
        now = time.time()
        for i in range(n):
            chat.post("x", who="bob", room="r%02d" % i)
            p = chat.room_path("r%02d" % i)
            os.utime(p, (now - 1000 + i, now - 1000 + i))      # r00 oldest
        rooms = seats._scan_rooms("main")
        self.assertEqual(rooms[0], "main")
        self.assertEqual(len(rooms), seats.ROOM_SCAN_CAP)      # the bound
        self.assertIn("r%02d" % (n - 1), rooms)                # newest kept
        self.assertNotIn("r00", rooms)                         # oldest dropped


class RoomAllowlistTest(SeatsBase):
    """Homing under the BEACON-SCOPE law (premise beacon-scope-mentions-
    plus-home-room-owner-posts-not-all, superseding the G1-G3 allowlist —
    the allowlist starved codex-2 of an @codex-2 mention in #helm-dogfood):
    a foreign team's @all / owner-post still never drafts a homed seat, but
    a direct @mention crosses EVERY room, always."""

    def test_home_room_recorded_at_join(self):
        os.environ["HELM_CHAT_ROOM"] = "team-a"
        seats.join(session="s-a", seat="ta", cwd="/tmp/p")
        self.assertEqual(seats.roster()["ta"]["home_room"], "team-a")
        # a sessionless auto roster write (deliver's path) never strips it
        seats.write_roster("ta", session="s-a2")
        self.assertEqual(seats.roster()["ta"]["home_room"], "team-a")

    def test_explicit_join_room_is_homed_and_slugged(self):
        seats.join(session="s-exp", seat="ex", cwd="/tmp/p", room="Team A")
        self.assertEqual(seats.roster()["ex"]["home_room"], "team-a")
        self.assertIn("home room team-a", seats.join(
            session="s-exp2", seat="ex", cwd="/tmp/p", room="Team A")[1])

    def test_rejoin_with_new_room_rehomes(self):
        os.environ["HELM_CHAT_ROOM"] = "team-a"
        seats.join(session="s-a", seat="mv", cwd="/tmp/p")
        os.environ["HELM_CHAT_ROOM"] = "team-b"     # the deliberate move
        seats.join(session="s-a2", seat="mv", cwd="/tmp/p")
        self.assertEqual(seats.roster()["mv"]["home_room"], "team-b")

    def test_homed_seat_skips_foreign_all_and_owner_but_hears_mentions(self):
        os.environ["HELM_CHAT_ROOM"] = "team-a"
        seats.join(session="s-ta", seat="ta", cwd="/tmp/p")
        del os.environ["HELM_CHAT_ROOM"]
        # foreign broadcast + owner noise never drafts the homed seat…
        chat.post("@all standup", who="bob", room="team-b")
        chat.post("owner direction for team b", who="david", origin="web",
                  room="team-b")
        chat.post("team b chatter", who="bob", room="team-b")
        self.assertIsNone(seats.deliver_any(session="s-ta", seat="ta"))
        # …but a DIRECT @mention crosses any room, always (THE codex-2 bug:
        # a homed seat never saw '@codex-2 …' posted in #helm-dogfood)
        chat.post("@ta foreign mention", who="bob", room="team-b")
        self.assertIn("foreign mention",
                      seats.deliver_any(session="s-ta", seat="ta"))
        # home room + main + owner-in-home DO land (primary room first:
        # main's mention outranks team-a's, one nudge per boundary)
        chat.post("@ta home word", who="bob", room="team-a")
        chat.post("@ta main word", who="bob")
        chat.post("owner in team a", who="david", origin="web", room="team-a")
        got = [seats.deliver_any(session="s-ta", seat="ta") for _ in range(3)]
        text = "\n".join(g for g in got if g)
        self.assertIn("main word", got[0])
        self.assertIn("home word", text)
        self.assertIn("owner in team a", text)
        self.assertIsNone(seats.deliver_any(session="s-ta", seat="ta"))

    def test_unhomed_seat_keeps_every_room(self):
        seats.join(session="s-u", seat="un", cwd="/tmp/p")   # no HELM_CHAT_ROOM
        chat.post("@un foreign ping", who="bob", room="team-b")
        line = seats.deliver_any(session="s-u", seat="un")
        self.assertIn("foreign ping", line)
        self.assertIn("#team-b", line)

    def test_stop_guard_scope_matches_the_lane_for_homed_seat(self):
        os.environ["HELM_CHAT_ROOM"] = "team-a"
        seats.join(session="s-g", seat="ga", cwd="/tmp/p")
        del os.environ["HELM_CHAT_ROOM"]
        chat.post("@all team b standup", who="bob", room="team-b")
        chat.post("team b owner note", who="david", origin="web", room="team-b")
        blocks, _warns = seats.stop_guard(session="s-g", seat="ga")
        self.assertEqual(blocks, [])          # foreign @all/owner must NOT gate
        chat.post("@ga home call", who="bob", room="team-a")
        blocks, _warns = seats.stop_guard(session="s-g", seat="ga")
        self.assertTrue(blocks)               # a home-room mention still gates

    def test_scan_rooms_covers_every_room_scope_lives_in_deliverable(self):
        """The scan is scope-BLIND under the beacon-scope premise — an
        @mention anywhere must surface, so homing filters per ROW, never
        per room."""
        os.environ["HELM_CHAT_ROOM"] = "team-a"
        seats.join(session="s-s", seat="sc", cwd="/tmp/p")
        del os.environ["HELM_CHAT_ROOM"]
        chat.post("x", who="bob", room="team-b")
        chat.post("x", who="bob", room="team-c")
        rooms = seats._scan_rooms("team-a", seat="sc")
        self.assertEqual(rooms[0], "team-a")   # primary first
        self.assertIn("team-b", rooms)
        self.assertIn("team-c", rooms)
        # the scope filter is deliverable()'s: foreign chatter/broadcast no,
        # home-room anything yes
        sc = seats.seat_scope("sc")
        self.assertEqual(sc["home"], "team-a")
        row = {"ts": "t", "from": "bob", "text": "no mention"}
        self.assertFalse(seats.deliverable(row, "sc", "team-b", sc))
        self.assertTrue(seats.deliverable(row, "sc", "team-a", sc))

    def test_foreign_room_volume_cannot_starve_the_home_room(self):
        os.environ["HELM_CHAT_ROOM"] = "team-a"
        seats.join(session="s-cap", seat="cap", cwd="/tmp/p")
        del os.environ["HELM_CHAT_ROOM"]
        chat.post("home", who="bob", room="team-a")
        chat.post("all hands", who="bob")
        # Newer foreign rooms fill the global scan cap. The scope-blind scan
        # ADMITS them (mentions must cross rooms) but PINS home + main — the
        # seat's own channel is never evicted by foreign volume.
        for i in range(seats.ROOM_SCAN_CAP + 5):
            chat.post("noise", who="bob", room="foreign-%02d" % i)
        rooms = seats._scan_rooms("main", seat="cap")
        self.assertIn("team-a", rooms)
        self.assertIn("main", rooms)
        self.assertLessEqual(len(rooms), seats.ROOM_SCAN_CAP + 3)  # bounded
        # …and home-room traffic still DELIVERS through the flood
        chat.post("word for the team", who="carol", room="team-a")
        got = [seats.deliver_any(session="s-cap", seat="cap") for _ in range(3)]
        self.assertIn("word for the team", "\n".join(g for g in got if g))


class BeaconScopeTest(SeatsBase):
    """Premise beacon-scope-mentions-plus-home-room-owner-posts-not-all:
    (a) @mention any room ALWAYS; (b) ANYTHING in the home room; (c) owner
    posts/@all never fleet-wide; (d) mute tunes (b)/(c), never (a)."""

    def test_codex2_repro_foreign_room_mention_wakes_main_homed_beacon(self):
        """THE live bug: codex-2 homed to #main never saw '@codex-2 …'
        posted in #helm-dogfood — the homing allowlist starved the beacon."""
        os.environ["HELM_CHAT_ROOM"] = "main"
        seats.join(session="s-c2", seat="codex-2", cwd="/tmp/p")
        del os.environ["HELM_CHAT_ROOM"]
        self.assertEqual(seats.roster()["codex-2"]["home_room"], "main")
        chat.post("@codex-2 please pick this up", who="david",
                  room="helm-dogfood")
        captured = []
        seats.wait(seat="codex-2", session="s-c2", follow=True, timeout=0.15,
                   poll=0.01, emit=captured.append)
        self.assertEqual(len(captured), 1)
        self.assertIn("please pick this up", captured[0])
        self.assertIn("#helm-dogfood", captured[0])

    def test_home_room_surfaces_everything(self):
        seats.join(session="s-h", seat="hm", cwd="/tmp/p", room="team-a")
        chat.post("plain team chatter, no mention", who="bob", room="team-a")
        line = seats.deliver_any(session="s-h", seat="hm")
        self.assertIn("plain team chatter", line)
        self.assertIn("#team-a", line)
        # …and a seat homed to MAIN gets everything in main (codex-2's home)
        os.environ["HELM_CHAT_ROOM"] = "main"
        seats.join(session="s-h2", seat="hm2", cwd="/tmp/p")
        del os.environ["HELM_CHAT_ROOM"]
        chat.post("main chatter", who="bob")
        self.assertIn("main chatter",
                      seats.deliver_any(session="s-h2", seat="hm2"))

    def test_non_home_non_mention_never_delivers(self):
        seats.join(session="s-n", seat="nn", cwd="/tmp/p", room="team-a")
        chat.post("other team chatter", who="bob", room="team-b")
        chat.post("@all other team standup", who="bob", room="team-b")
        chat.post("owner steering team b", who="david", origin="web",
                  room="team-b")
        chat.post("un-homed main chatter", who="bob")   # main ≠ home either
        self.assertIsNone(seats.deliver_any(session="s-n", seat="nn"))

    def test_mute_suppresses_noise_but_never_mentions_or_dms(self):
        seats.join(session="s-m", seat="mu", cwd="/tmp/p", room="team-a")
        rc, out, _err = self.cmd("seat", ["mute", "team-a", "--seat", "mu"])
        self.assertEqual(rc, 0)
        self.assertIn("muted", out)
        seats.set_mute("mu", "main")
        # home-room chatter + owner post in main: both muted away
        chat.post("home chatter", who="bob", room="team-a")
        chat.post("owner note", who="david", origin="web")
        self.assertIsNone(seats.deliver_any(session="s-m", seat="mu"))
        # a direct @mention in the MUTED room still surfaces (mute tunes
        # noise, never direct address — premise (a) says ALWAYS)
        chat.post("@mu direct word", who="bob", room="team-a")
        self.assertIn("direct word", seats.deliver_any(session="s-m", seat="mu"))
        # a DM still surfaces
        seats.dm("mu", "psst", who="ada")
        self.assertIn("psst", seats.deliver_any(session="s-m", seat="mu"))
        # unmute restores the flow WITHOUT flooding the muted backlog
        # (cursors advanced past it), and `mutes` reports the live set
        self.assertEqual(seats.mutes("mu"), ["main", "team-a"])
        rc, out, _err = self.cmd("seat", ["unmute", "team-a", "--seat", "mu"])
        self.assertEqual(rc, 0)
        self.assertEqual(seats.mutes("mu"), ["main"])
        self.assertIsNone(seats.deliver_any(session="s-m", seat="mu"))
        chat.post("after unmute", who="bob", room="team-a")
        self.assertIn("after unmute",
                      seats.deliver_any(session="s-m", seat="mu"))

    def test_mute_gates_stop_guard_too(self):
        seats.join(session="s-sg", seat="mg", cwd="/tmp/p", room="team-a")
        seats.set_mute("mg", "team-a")
        chat.post("noise while muted", who="bob", room="team-a")
        blocks, _w = seats.stop_guard(session="s-sg", seat="mg")
        self.assertEqual(blocks, [])          # muted noise never gates a stop
        chat.post("@mg but answer this", who="bob", room="team-a")
        blocks, _w = seats.stop_guard(session="s-sg", seat="mg")
        self.assertTrue(blocks)               # the direct address still does


class DMTest(SeatsBase):
    """The 1:1 lane (premise exact-token-addressee-match): session/seat-keyed,
    exactly one recipient, zero room fanout, renders as a DM, signs like a
    post."""

    def test_dm_reaches_exactly_one_seat_no_room_fanout(self):
        seats.join(session="s-a", seat="ada", cwd="/tmp/p")
        seats.join(session="s-b", seat="ben", cwd="/tmp/p")
        seats.join(session="s-c", seat="cyd", cwd="/tmp/p")
        row, err = seats.dm("ben", "secret handshake", who="ada")
        self.assertIsNone(err)
        self.assertEqual(row["dm"], "ben")
        # NO room fanout: no channel appears, #main got nothing
        self.assertEqual(chat.list_rooms(), [])
        self.assertEqual(chat.read("main")[1], 0)
        # exactly ONE recipient, delivered as a DM (not a room row)
        line = seats.deliver_any(session="s-b", seat="ben")
        self.assertIn("secret handshake", line)
        self.assertIn("[helm chat dm → ben]", line)
        self.assertIsNone(seats.deliver_any(session="s-b", seat="ben"))
        self.assertIsNone(seats.deliver_any(session="s-a", seat="ada"))
        self.assertIsNone(seats.deliver_any(session="s-c", seat="cyd"))

    def test_dm_exact_token_never_substring_or_slug_fold(self):
        """team.a and team-a slug-collide but are DIFFERENT addressees —
        a DM to one must never reach the other."""
        seats.join(session="s-p", seat="team.a", cwd="/tmp/p")
        seats.join(session="s-q", seat="team-a", cwd="/tmp/p")
        _row, err = seats.dm("team.a", "for the dot team only", who="ada")
        self.assertIsNone(err)
        self.assertIsNone(seats.deliver_any(session="s-q", seat="team-a"))
        self.assertIn("for the dot team only",
                      seats.deliver_any(session="s-p", seat="team.a"))

    def test_dm_signed_like_a_post_and_renders_as_dm(self):
        sent = {"sent": True, "turn_hash": "t" * 64, "receipt_hash": "r" * 64,
                "chain_index": 9}
        with mock.patch.object(chat, "_sign_send", return_value=(sent, None)) as ss:
            row, err = seats.dm("zoe", "signed word", who="ada", sign=True)
        self.assertIsNone(err)
        self.assertEqual(row["chain"], 9)
        ss.assert_called_once_with(chat.digest_payload("signed word"), mock.ANY)
        rendered = chat._fmt(row)
        self.assertNotIn("[unsigned]", rendered)
        self.assertIn("-> @zoe (dm):", rendered)
        # unsigned still lands, loudly tagged (fallback law)
        row2, _err = seats.dm("zoe", "plain word", who="ada")
        self.assertIn("[unsigned]", chat._fmt(row2))

    def test_dm_beacon_wakes_the_recipient(self):
        seats.join(session="s-r", seat="rio", cwd="/tmp/p")
        seats.dm("rio", "wake up rio", who="ada")
        captured = []
        seats.wait(seat="rio", session="s-r", follow=True, timeout=0.15,
                   poll=0.01, emit=captured.append)
        self.assertEqual(len(captured), 1)
        self.assertIn("wake up rio", captured[0])
        self.assertIn("dm", captured[0])

    def test_dm_before_join_delivers_after_join(self):
        seats.dm("late", "waiting for you", who="ada")
        seats.join(session="s-l", seat="late", cwd="/tmp/p")
        self.assertIn("waiting for you",
                      seats.deliver_any(session="s-l", seat="late"))

    def test_dm_to_untracked_seat_still_delivers(self):
        """The lane always backfills from 0 — even a seat with no cursor
        anywhere (reaped / never joined) gets the DM that created it."""
        seats.dm("ghost", "boo", who="ada")
        self.assertIn("boo", seats.deliver_any(session="s-gh", seat="ghost"))

    def test_dm_case_snaps_to_the_live_roster_seat(self):
        seats.join(session="s-k", seat="Kimi", cwd="/tmp/p")
        row, _err = seats.dm("kimi", "case snap", who="ada")
        self.assertEqual(row["dm"], "Kimi")
        self.assertIn("case snap", seats.deliver_any(session="s-k", seat="Kimi"))

    def test_dm_refuses_self_and_bad_tokens(self):
        row, err = seats.dm("ada", "hi me", who="ada")
        self.assertIsNone(row)
        self.assertIn("yourself", err)
        row, err = seats.dm("bad name!", "x", who="ada")
        self.assertIsNone(row)
        self.assertIn("exact seat token", err)

    def test_dm_gates_the_stop_and_counts_pending(self):
        seats.join(session="s-g", seat="gee", cwd="/tmp/p")
        seats.dm("gee", "answer me first", who="ada")
        blocks, _w = seats.stop_guard(session="s-g", seat="gee")
        self.assertTrue(blocks)
        self.assertIn("[dm]", blocks[0])
        rep = seats.roster_report("main")
        s = [x for x in rep["seats"] if x["seat"] == "gee"][0]
        self.assertEqual(s["pending"], 1)

    def test_dm_lane_survives_a_rename(self):
        seats.join(session="s-rn", seat="oldname", cwd="/tmp/p")
        seats.dm("oldname", "pre-rename word", who="ada")
        ok, _msg = seats.rename_seat("oldname", "newname")
        self.assertTrue(ok)
        self.assertIn("pre-rename word",
                      seats.deliver_any(session="s-rn", seat="newname"))
        seats.dm("newname", "post-rename word", who="ada")
        self.assertIn("post-rename word",
                      seats.deliver_any(session="s-rn", seat="newname"))

    def test_dm_cli_verbs(self):
        seats.join(session="s-v", seat="vic", cwd="/tmp/p")
        # helm chat dm <seat> <text...> [--seat S]
        rc, out, _err = self.cmd("dm", ["vic", "hello", "there", "--seat", "ada"])
        self.assertEqual(rc, 0)
        self.assertIn("hello there", out)
        self.assertIn("(dm)", out)
        # helm chat post --dm SEAT (the flag-shaped route)
        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            rc = chat.cmd_chat(["post", "hi", "again",
                                "--dm", "vic", "--seat", "ada"])
        self.assertEqual(rc, 0, err2.getvalue())
        self.assertIn("(dm)", out2.getvalue())
        # the recipient reads its lane: helm chat read --dm --seat vic
        out3 = io.StringIO()
        with contextlib.redirect_stdout(out3):
            rc = chat.cmd_chat(["read", "--dm", "--seat", "vic"])
        self.assertEqual(rc, 0)
        self.assertIn("hello there", out3.getvalue())
        self.assertIn("hi again", out3.getvalue())
        self.assertIn("(dm)", out3.getvalue())
        # nothing fanned out to any room
        self.assertEqual(chat.list_rooms(), [])


class ProjectHomingTest(SeatsBase):
    """Multi-PROJECT homing (owner canon main-room-topology): a seat with no
    explicit HELM_CHAT_ROOM/--room derives its home from the join cwd's git
    project (common-dir parent basename — worktree-agnostic); explicit wins;
    a project-less cwd stays un-homed; rehome_seat is the deliberate move."""

    def _repo(self, name="proj-alpha"):
        import subprocess
        repo = os.path.join(self.tmp, name)
        os.makedirs(repo, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True,
                       capture_output=True)
        return repo

    def test_derives_home_from_git_project(self):
        repo = self._repo()
        seats.join(session="s-1", seat="pa", cwd=repo)
        self.assertEqual(seats.roster()["pa"]["home_room"], "proj-alpha")

    def test_subdir_derives_the_repo_project_not_the_subdir(self):
        repo = self._repo()
        sub = os.path.join(repo, "apps", "web")
        os.makedirs(sub)
        seats.join(session="s-2", seat="pb", cwd=sub)
        # project identity is the repo, never the cwd basename ('web')
        self.assertEqual(seats.roster()["pb"]["home_room"], "proj-alpha")

    def test_worktree_derives_the_main_repo_project(self):
        repo = self._repo()
        import subprocess
        wt = os.path.join(self.tmp, "proj-alpha-wt-lane")
        subprocess.run(["git", "-C", repo, "worktree", "add", "-q", wt],
                       check=True, capture_output=True)
        seats.join(session="s-3", seat="pc", cwd=wt)
        self.assertEqual(seats.roster()["pc"]["home_room"], "proj-alpha")

    def test_project_less_cwd_stays_unhomed(self):
        seats.join(session="s-4", seat="pd", cwd=self.tmp)  # tmp not a repo
        self.assertIsNone(seats.roster()["pd"].get("home_room"))

    def test_explicit_room_beats_derivation(self):
        repo = self._repo()
        seats.join(session="s-5", seat="pe", cwd=repo, room="team-x")
        self.assertEqual(seats.roster()["pe"]["home_room"], "team-x")
        # HELM_CHAT_ROOM (the launch seam) also wins
        os.environ["HELM_CHAT_ROOM"] = "team-y"
        seats.join(session="s-6", seat="pf", cwd=repo)
        self.assertEqual(seats.roster()["pf"]["home_room"], "team-y")

    def test_derived_home_does_not_rehome_on_later_join(self):
        repo = self._repo()
        seats.join(session="s-7", seat="pg", cwd=repo)
        self.assertEqual(seats.roster()["pg"]["home_room"], "proj-alpha")
        # a project-less re-join (derive → None) never strips the home
        seats.join(session="s-7b", seat="pg", cwd=self.tmp)
        self.assertEqual(seats.roster()["pg"]["home_room"], "proj-alpha")

    def test_repo_named_main_stays_unhomed(self):
        repo = self._repo(name="main")
        seats.join(session="s-8", seat="ph", cwd=repo)
        self.assertIsNone(seats.roster()["ph"].get("home_room"))

    def test_homed_project_seat_isolated_from_sibling_project(self):
        ra, rb = self._repo("proj-a"), self._repo("proj-b")
        seats.join(session="s-a", seat="sea", cwd=ra)   # homed #proj-a
        chat.post("@sea proj-b mention", who="bob", room="proj-b")
        chat.post("owner in proj-b", who="david", origin="web", room="proj-b")
        self.assertIsNone(seats.deliver_any(session="s-a", seat="sea"))
        chat.post("@sea proj-a word", who="bob", room="proj-a")
        self.assertIn("proj-a word", seats.deliver_any(session="s-a", seat="sea"))

    def test_rehome_seat_deliberate_move_and_clear(self):
        repo = self._repo()
        seats.join(session="s-9", seat="pi", cwd=repo)
        self.assertEqual(seats.roster()["pi"]["home_room"], "proj-alpha")
        ok, msg = seats.rehome_seat("pi", "team-z")
        self.assertTrue(ok, msg)
        self.assertEqual(seats.roster()["pi"]["home_room"], "team-z")
        # isolation follows the new home immediately (no relaunch)
        chat.post("@pi proj-a traffic", who="bob", room="proj-a")
        self.assertIsNone(seats.deliver_any(session="s-9", seat="pi"))
        chat.post("@pi team-z word", who="bob", room="team-z")
        self.assertIn("team-z word", seats.deliver_any(session="s-9", seat="pi"))
        # clear back to un-homed
        ok, msg = seats.rehome_seat("pi", "main")
        self.assertTrue(ok, msg)
        self.assertIsNone(seats.roster()["pi"].get("home_room"))

    def test_rehome_unknown_seat_refused(self):
        ok, msg = seats.rehome_seat("ghost", "team-z")
        self.assertFalse(ok)
        self.assertIn("no roster row", msg)


class AutoNameTest(SeatsBase):
    """G-stable-names: an un-named join gets a MEANINGFUL stable auto-name
    (project+family, deduped) instead of opaque agent-<sid8> hex."""

    def test_unnamed_join_gets_meaningful_stable_deduped_name(self):
        with mock.patch.dict(os.environ,
                             {"CLAUDE_CODE_SUBAGENT_MODEL": "claude-fable-5"}):
            seat, _ = seats.join(session="s-a1", cwd="/tmp/helm")
            self.assertEqual(seat, "helm-fable")
            again, _ = seats.join(session="s-a1", cwd="/tmp/helm")  # stable
            self.assertEqual(again, "helm-fable")
            other, _ = seats.join(session="s-a2", cwd="/tmp/helm")  # deduped
            self.assertEqual(other, "helm-fable-2")
        # both are ADDRESSABLE apart — no shared cursor, no cross-consume
        chat.post("@helm-fable-2 only you", who="bob")
        self.assertIsNone(seats.deliver(session="s-a1"))
        self.assertIn("only you", seats.deliver(session="s-a2"))

    def test_family_fallbacks(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SUBAGENT_MODEL": "kimi-k3"}):
            self.assertEqual(seats._family(), "kimi")
        with mock.patch.dict(os.environ, {"CODEX_SESSION_ID": "x"}):
            self.assertEqual(seats._family(), "codex")
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.assertEqual(seats._family(), "claude")
        self.assertEqual(seats._family(), "agent")   # everything scrubbed

    def test_explicit_chat_name_still_wins(self):
        os.environ["HELM_CHAT_NAME"] = "codex"
        self.assertEqual(seats.derive_seat("s-x", "/tmp/helm"), "codex")

    def test_whoname_speaks_the_roster_seat(self):
        """A joined session POSTS under its seat name — deliveries and posts
        speak one name, and a rename rebinds both."""
        seats.join(session="sess-w1", seat="wren", cwd="/tmp/p")
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sess-w1"}):
            self.assertEqual(chat.whoname(), "wren")

    def test_whoname_resolves_the_real_claude_code_session_var(self):
        """REGRESSION (owner-caught 2026-07-21): Claude Code exports
        CLAUDE_CODE_SESSION_ID, NOT CLAUDE_SESSION_ID — a bare CLI post fell
        through to the anon 'agent' floor and the per-session cursor no-op'd.
        home.session_id() must resolve the real var so whoname() speaks the
        seat and the co-named cursor keys correctly."""
        seats.join(session="sess-cc1", seat="opus-integrator", cwd="/tmp/p")
        with mock.patch.dict(os.environ,
                             {"CLAUDE_CODE_SESSION_ID": "sess-cc1"}):
            self.assertEqual(home.session_id(), "sess-cc1")
            self.assertEqual(chat.whoname(), "opus-integrator")

    def test_session_id_resolution_order(self):
        """CLAUDE_CODE_SESSION_ID wins over the legacy alias and codex var;
        None when every harness var is scrubbed (the base's ENV_KEYS scrub
        leaves them absent, so callers fall to their floor)."""
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "cc",
                                          "CLAUDE_SESSION_ID": "legacy",
                                          "CODEX_SESSION_ID": "cx"}):
            self.assertEqual(home.session_id(), "cc")
        self.assertIsNone(home.session_id())


class RenameTest(SeatsBase):
    """helm chat seat rename — bind a live agent to a memorable @name."""

    def test_rename_rebinds_delivery_and_keeps_tracked_ground(self):
        seats.join(session="s-r1", seat="agent-3f2a", cwd="/tmp/p")
        chat.post("noise", who="bob")
        self.assertIsNone(seats.deliver(session="s-r1"))
        off = seats._cursor("main", "agent-3f2a", "s-r1")["off"]
        ok, msg = seats.rename_seat("agent-3f2a", "art3mis")
        self.assertTrue(ok, msg)
        self.assertIn("re-arm", msg)                       # beacon note
        self.assertNotIn("agent-3f2a", seats.roster())
        self.assertIn("art3mis", seats.roster())
        # the cursor moved WITH the seat — no EOF re-baseline, no loss
        self.assertEqual(seats._cursor("main", "art3mis", "s-r1")["off"], off)
        chat.post("@art3mis go", who="bob")
        self.assertIn("go", seats.deliver(session="s-r1"))  # hook path rebound
        self.assertIsNone(seats.deliver(session="s-r1"))

    def test_rename_by_session_prefix(self):
        seats.join(session="sess-abcdef1234", seat="agent-xyz", cwd="/tmp/p")
        ok, _msg = seats.rename_seat("sess-abc", "nice")
        self.assertTrue(ok)
        self.assertEqual(seats.seat_for_session("sess-abcdef1234"), "nice")

    def test_rename_refusals(self):
        seats.join(session="s-1", seat="a", cwd="/tmp/p")
        seats.join(session="s-2", seat="b", cwd="/tmp/p")
        for bad, why in (("b", "taken"), ("david", "reserved"),
                         ("all", "reserved"), ("sp ace", "chars"),
                         ("", "chars")):
            ok, msg = seats.rename_seat("a", bad)
            self.assertFalse(ok, "%s should refuse (%s): %s" % (bad, why, msg))
        ok, msg = seats.rename_seat("ghost", "x")
        self.assertFalse(ok)
        self.assertIn("no roster row", msg)
        ok, msg = seats.rename_seat("a", "a")          # no-op, not an error
        self.assertTrue(ok)

    def test_rename_refuses_case_collision(self):
        """A case-variant of a live seat is the SAME address + keyed state
        downstream (casefold keys, re.I mentions) — renaming INTO one must be
        refused, else the two rows alias mentions/presence and the reaper
        cross-fires onto the live seat's state (kimi cross-family review,
        live-probed 2026-07-21). A pure self-case-change is still allowed."""
        seats.join(session="s-k", seat="kimi", cwd="/tmp/p")
        seats.join(session="s-a", seat="alpha", cwd="/tmp/p")
        ok, msg = seats.rename_seat("alpha", "KIMI")
        self.assertFalse(ok, msg)
        self.assertIn("taken", msg)
        self.assertNotIn("KIMI", seats.roster())         # no aliased row minted
        self.assertIn("kimi", seats.roster())
        ok, _ = seats.rename_seat("kimi", "Kimi")         # self-case-change ok
        self.assertTrue(ok)

    def test_cli_and_web_rename(self):
        seats.join(session="s-9", seat="blob", cwd="/tmp/p")
        rc, _out, err = self.cmd("seat", ["rename", "blob", "buddy"])
        self.assertEqual(rc, 0, err)
        self.assertIn("buddy", seats.roster())
        obj, code = web._api_chat_seat({"action": "rename",
                                        "seat": "buddy", "new": "pal"})
        self.assertEqual(code, 200, obj)
        self.assertIn("pal", seats.roster())
        obj, code = web._api_chat_seat({"action": "rename",
                                        "seat": "ghost", "new": "x"})
        self.assertEqual(code, 400)
        obj, code = web._api_chat_seat({"action": "nuke", "seat": "pal"})
        self.assertEqual(code, 400)
        rc, _out, err = self.cmd("seat", ["rename"])   # usage
        self.assertEqual(rc, 2)


class RosterTruthTest(SeatsBase):
    """roster-truth (owner-caught 2026-07-21: 'keep all live agents straight on
    the roster'). A live-but-idle agent must not vanish — presence stays fresh
    when it speaks, and even when its delivery is muted."""

    def test_post_refreshes_poster_presence(self):
        seats.join(session="s-rt1", seat="rt-agent", cwd="/tmp/p")
        os.remove(seats.seen_path("rt-agent"))          # prove post re-touches
        chat.post("hello fleet", who="rt-agent")
        self.assertTrue(os.path.exists(seats.seen_path("rt-agent")))

    def test_owner_post_mints_no_presence(self):
        chat.post("owner speaks", who="david")          # owner is not a seat
        self.assertFalse(os.path.exists(seats.seen_path("david")))
        self.assertNotIn("david", seats.roster())

    def test_muted_deliver_still_refreshes_presence(self):
        seats.join(session="s-rt2", seat="rt-muted", cwd="/tmp/p")
        os.remove(seats.seen_path("rt-muted"))
        os.environ["HELM_CHAT_DELIVER"] = "0"            # delivery muted…
        self.assertIsNone(seats.deliver(session="s-rt2"))   # …so no nudge…
        self.assertTrue(os.path.exists(seats.seen_path("rt-muted")))  # …still alive


class StopGuardTest(SeatsBase):
    """The idle gate (buildr/mc arbiter port). Hermetic: room + claims in tmp,
    HELM_ADOPTED_DIR in tmp so the silent index-cap leg can never touch a live
    MEMORY.md."""

    def guard(self, payload=None, args=()):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        return self.cmd("stop-guard", ["--hook-json", *args], stdin=stdin or b"{}")

    def test_pending_mention_blocks_once_listing_the_row(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice review the branch", who="bob")
        rc, _out, err = self.guard({"session_id": "s-1"}, args=["--seat", "alice"])
        self.assertEqual(rc, 2)
        self.assertIn("undelivered", err)
        self.assertIn("bob: @alice review the branch", err)   # listed compactly
        self.assertIn("address these before stopping", err)
        self.assertIn("helm chat read", err)
        # the guard is a gate, not a delivery: the cursor never moved, the
        # tool-boundary lane still delivers the row afterwards
        self.assertIn("review the branch", seats.deliver(seat="alice"))

    def test_same_fingerprint_second_stop_passes(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice go", who="bob")
        rc, _o, _e = self.guard(args=["--seat", "alice"])
        self.assertEqual(rc, 2)                    # first stop on this set blocks
        rc, _o, err = self.guard(args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)               # same rows: pass — never a loop
        self.assertNotIn("inbox clean", err)       # latched ≠ clean — no false warn

    def test_new_row_after_a_passed_stop_blocks_again(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice one", who="bob")
        self.assertEqual(self.guard(args=["--seat", "alice"])[0], 2)
        self.assertEqual(self.guard(args=["--seat", "alice"])[0], 0)  # latched
        chat.post("@alice two", who="bob")         # NEW pending set
        rc, _o, err = self.guard(args=["--seat", "alice"])
        self.assertEqual(rc, 2)
        self.assertIn("@alice two", err)

    def test_held_lease_blocks_naming_the_resource(self):
        ok, _m, _l = seats.claim("worktree-main", "alice", ttl=60, session="s-9")
        self.assertTrue(ok)
        rc, _o, err = self.guard({"session_id": "s-9"})
        self.assertEqual(rc, 2)
        self.assertIn("worktree-main", err)
        self.assertIn("release", err)
        # a DIFFERENT session holds nothing — no claims block
        rc, _o, err = self.guard({"session_id": "s-other"})
        self.assertEqual(rc, 0, err)

    def test_both_blockers_surface_in_one_exit_2(self):
        seats.join(session="s-9", seat="alice", cwd="/tmp/p")
        chat.post("@alice pending", who="bob")
        seats.claim("worktree-main", "alice", ttl=60, session="s-9")
        rc, _o, err = self.guard({"session_id": "s-9"}, args=["--seat", "alice"])
        self.assertEqual(rc, 2)
        self.assertIn("undelivered", err)          # the whole picture in ONE shot
        self.assertIn("worktree-main", err)

    def test_clean_stop_passes_with_beacon_warn(self):
        seats.join(seat="bob", cwd="/tmp/p")
        rc, _o, err = self.guard(args=["--seat", "bob"])
        self.assertEqual(rc, 0)
        self.assertIn("helm chat wait --seat bob --follow", err)  # the arm line
        self.assertIn("Monitor", err)

    def test_kill_switches(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice pending", who="bob")
        os.environ["HELM_STOP_GUARD"] = "0"
        rc, _o, err = self.guard(args=["--seat", "alice"])
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")                  # off = silent, always
        os.environ.pop("HELM_STOP_GUARD")
        os.environ["HELM_STOP_GUARD_INBOX"] = "0"  # per-check off
        rc, _o, _e = self.guard(args=["--seat", "alice"])
        self.assertEqual(rc, 0)
        os.environ.pop("HELM_STOP_GUARD_INBOX")
        seats.claim("worktree-x", "alice", ttl=60, session="s-9")
        os.environ["HELM_STOP_GUARD_CLAIMS"] = "0"
        rc, _o, _e = self.guard({"session_id": "s-9"})
        self.assertEqual(rc, 0)

    def test_stop_hook_active_never_reblocks(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice pending", who="bob")
        rc, _o, err = self.guard({"stop_hook_active": True}, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)               # the harness is already continuing

    def test_garbage_stdin_fails_open(self):
        rc, _o, _e = self.guard(b"not json{{")
        self.assertEqual(rc, 0)


class StopWhisperTest(SeatsBase):
    """The contextual continuation lane (stop-whisper slice 1): one budgeted
    nudge at turn-stop off live signals (record.py counters + the latched
    pending set), once per (signal, level) fingerprint, fail-closed to
    nothing. Hermetic: counters planted in the tmp HELM_HOME's reflex-state."""

    def guard(self, payload=None, args=()):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        return self.cmd("stop-guard", ["--hook-json", *args], stdin=stdin or b"{}")

    def plant(self, sid, **counters):
        pk.write_json(os.path.join(record.session_dir(sid), "counters.json"),
                      counters)

    def test_stuck_session_soft_holds_once_with_pull_pointer(self):
        seats.join(session="s-w1", seat="wisp", cwd="/tmp/p")
        self.plant("s-w1", **{"stuck-streak": 3})
        rc, _o, err = self.guard({"session_id": "s-w1"}, args=["--seat", "wisp"])
        self.assertEqual(rc, 2)
        self.assertIn("[helm stop-whisper]", err)
        self.assertIn("wedged", err)
        self.assertIn("helm reflex smoke --session s-w1", err)  # pull-depth pointer
        # same state on the next stop: latched — passes (never an infinite hold)
        rc, _o, err = self.guard({"session_id": "s-w1"}, args=["--seat", "wisp"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stop-whisper", err)

    def test_worsening_streak_refires_next_bucket(self):
        seats.join(session="s-w2", seat="wisp", cwd="/tmp/p")
        self.plant("s-w2", **{"stuck-streak": 3})
        self.assertEqual(self.guard({"session_id": "s-w2"})[0], 2)
        self.assertEqual(self.guard({"session_id": "s-w2"})[0], 0)  # latched
        self.plant("s-w2", **{"stuck-streak": 6})   # next escalate bucket
        rc, _o, err = self.guard({"session_id": "s-w2"})
        self.assertEqual(rc, 2)
        self.assertIn("6 repeated", err)

    def test_dirty_streak_banks_the_slice(self):
        seats.join(session="s-w3", seat="wisp", cwd="/tmp/p")
        self.plant("s-w3", **{"dirty-streak": 8})
        rc, _o, err = self.guard({"session_id": "s-w3"})
        self.assertEqual(rc, 2)
        self.assertIn("bank the green slice", err)
        self.assertIn("git status", err)            # pull-depth pointer

    def age_room_rows(self, room="main", secs=3600):
        """Rewrite a room log's row timestamps `secs` into the past — the
        stale-unlanded precondition (a fresh set never whispers: echo≠context)."""
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - secs))
        p = os.path.join(chat.chat_dir(), room + ".jsonl")
        with open(p) as f:
            rows = [json.loads(l) for l in f]
        with open(p, "w") as f:
            for r in rows:
                r["ts"] = old
                f.write(json.dumps(r) + "\n")

    def test_unlanded_pending_reminds_once_after_inbox_latch(self):
        seats.join(seat="wisp", cwd="/tmp/p")
        chat.post("@wisp land the fix", who="david")
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 2)                      # stop 1: the inbox block
        self.assertIn("undelivered", err)
        self.assertNotIn("stop-whisper", err)        # never doubled on one stop
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 0, err)                 # stop 2, rows FRESH: latched
        self.age_room_rows()                         # …the set sits unlanded >10m
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 2)                      # stop 3: the unlanded whisper
        self.assertIn("unlanded >10m", err)
        self.assertIn("helm chat read", err)
        rc, _o, err = self.guard(args=["--seat", "wisp"])
        self.assertEqual(rc, 0, err)                 # stop 4: silence

    def test_one_whisper_even_with_multiple_live_signals(self):
        seats.join(session="s-w5", seat="wisp", cwd="/tmp/p")
        self.plant("s-w5", **{"stuck-streak": 3, "dirty-streak": 9})
        rc, _o, err = self.guard({"session_id": "s-w5"})
        self.assertEqual(rc, 2)
        self.assertEqual(err.count("[helm stop-whisper]"), 1)
        self.assertIn("wedged", err)                 # salience: stuck wins
        self.assertNotIn("bank the green", err)
        # next stop: stuck latched, dirty (still live) takes the slot
        rc, _o, err = self.guard({"session_id": "s-w5"})
        self.assertEqual(rc, 2)
        self.assertIn("bank the green", err)

    def test_budget_cap_holds(self):
        seats.join(session="s-w6" + "x" * 40, seat="wisp", cwd="/tmp/p")
        self.plant("s-w6" + "x" * 40, **{"stuck-streak": 3})
        rc, _o, err = self.guard({"session_id": "s-w6" + "x" * 40})
        self.assertEqual(rc, 2)
        line = next(l for l in err.splitlines() if "stop-whisper" in l)
        self.assertLessEqual(len(line), seats.STOP_WHISPER_CAP)

    def test_kill_switch_and_fail_closed(self):
        seats.join(session="s-w7", seat="wisp", cwd="/tmp/p")
        self.plant("s-w7", **{"stuck-streak": 5})
        os.environ["HELM_STOP_GUARD_WHISPER"] = "0"
        rc, _o, err = self.guard({"session_id": "s-w7"})
        self.assertEqual(rc, 0, err)                 # off = silent
        os.environ.pop("HELM_STOP_GUARD_WHISPER")
        # garbled counters: fail-closed to nothing, never a raise
        p = os.path.join(record.session_dir("s-w8"), "counters.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("not json{{")
        rc, _o, err = self.guard({"session_id": "s-w8"})
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stop-whisper", err)

    def test_ledger_row_ids_only(self):
        seats.join(session="s-w9", seat="wisp", cwd="/tmp/p")
        self.plant("s-w9", **{"dirty-streak": 8})
        self.assertEqual(self.guard({"session_id": "s-w9"})[0], 2)
        lp = os.path.join(home.global_dir(), ".state", "stop-whisper-ledger.jsonl")
        with open(lp) as f:
            rows = [json.loads(l) for l in f]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "dirty:1")
        self.assertEqual(rows[0]["session"], "s-w9")
        self.assertNotIn("text", rows[0])            # ids only, never content


class StopWhisperVerifyRungsTest(SeatsBase):
    """Slice 2 — the verify-grounding rungs: red-gate (RUN-but-not-GREEN),
    unverified (code edits, no gate ever ran), unbanked-green (gate green,
    tree dirty — commit is the clear next step). Hermetic: record.py's
    command-log/edit-targets planted in the tmp HELM_HOME's reflex-state.
    Each fires ONLY on its real condition, once per fingerprint, inside the
    byte cap, fail-closed, kill-switched."""

    def guard(self, sid, args=()):
        return self.cmd("stop-guard", ["--hook-json", *args],
                        stdin=json.dumps({"session_id": sid}).encode())

    def plant(self, sid, **counters):
        pk.write_json(os.path.join(record.session_dir(sid), "counters.json"),
                      counters)

    def plant_runs(self, sid, *rows, mode="w"):
        p = os.path.join(record.session_dir(sid), "command-log.jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode) as f:
            for i, r in enumerate(rows):
                f.write(json.dumps({"ts": int(time.time()) + i, **r}) + "\n")

    def plant_edits(self, sid, *names):
        p = os.path.join(record.session_dir(sid), "edit-targets.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("".join(n + "\n" for n in names))

    def test_red_gate_fires_once_then_rearms_on_a_new_red_run(self):
        seats.join(session="s-r1", seat="wisp", cwd="/tmp/p")
        self.plant_runs("s-r1", {"token": "pytest", "exit": 1, "digest": "aaa"})
        rc, _o, err = self.guard("s-r1")
        self.assertEqual(rc, 2)
        self.assertIn("gate ran RED", err)
        self.assertIn("pytest", err)
        self.assertIn("exited 1", err)
        rc, _o, err = self.guard("s-r1")
        self.assertEqual(rc, 0, err)                 # same red state: latched
        self.plant_runs("s-r1", {"token": "pytest", "exit": 1, "digest": "bbb"},
                        mode="a")                    # a NEW red run re-arms once
        rc, _o, err = self.guard("s-r1")
        self.assertEqual(rc, 2)
        self.assertIn("gate ran RED", err)

    def test_green_rerun_silences_the_red_gate(self):
        seats.join(session="s-r2", seat="wisp", cwd="/tmp/p")
        self.plant_runs("s-r2", {"token": "pytest", "exit": 1, "digest": "aaa"},
                        {"token": "pytest", "exit": 0, "digest": "ccc"})
        rc, _o, err = self.guard("s-r2")
        self.assertEqual(rc, 0, err)                 # latest per token is green
        self.assertNotIn("stop-whisper", err)

    def test_interrupted_run_is_no_verdict_never_red(self):
        seats.join(session="s-r3", seat="wisp", cwd="/tmp/p")
        self.plant_runs("s-r3", {"token": "pytest", "exit": -1, "digest": "aaa"})
        rc, _o, err = self.guard("s-r3")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stop-whisper", err)

    def test_unverified_code_edits_fire_docs_only_never(self):
        seats.join(session="s-u1", seat="wisp", cwd="/tmp/p")
        self.plant("s-u1", **{"last-dirty": 1})
        self.plant_edits("s-u1", "seats.py", "record.py")
        rc, _o, err = self.guard("s-u1")
        self.assertEqual(rc, 2)
        self.assertIn("NO test/gate run", err)
        self.assertIn("git diff --stat", err)        # pull-depth pointer
        rc, _o, err = self.guard("s-u1")
        self.assertEqual(rc, 0, err)                 # latched per edit bucket
        # docs-only session: the verify rungs stay silent (specificity law)
        seats.join(session="s-u2", seat="wisp2", cwd="/tmp/p")
        self.plant("s-u2", **{"last-dirty": 1})
        self.plant_edits("s-u2", "MEMORY.md", "NOTES.txt")
        rc, _o, err = self.guard("s-u2")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stop-whisper", err)

    def test_unverified_needs_a_dirty_tree(self):
        seats.join(session="s-u3", seat="wisp", cwd="/tmp/p")
        self.plant_edits("s-u3", "seats.py")         # edits banked, tree clean
        rc, _o, err = self.guard("s-u3")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stop-whisper", err)

    def test_unbanked_green_names_the_commit_step(self):
        seats.join(session="s-b1", seat="wisp", cwd="/tmp/p")
        self.plant("s-b1", **{"last-dirty": 1})
        self.plant_edits("s-b1", "seats.py")
        self.plant_runs("s-b1", {"token": "pytest", "exit": 0, "digest": "ggg"})
        rc, _o, err = self.guard("s-b1")
        self.assertEqual(rc, 2)
        self.assertIn("gate GREEN", err)
        self.assertIn("git add -A && git commit", err)
        rc, _o, err = self.guard("s-b1")
        self.assertEqual(rc, 0, err)                 # latched per green state
        self.plant_runs("s-b1", {"token": "pytest", "exit": 0, "digest": "hhh"},
                        mode="a")                    # a NEW proven-green re-arms
        self.assertEqual(self.guard("s-b1")[0], 2)

    def test_red_gate_owns_the_stop_over_unbanked(self):
        seats.join(session="s-b2", seat="wisp", cwd="/tmp/p")
        self.plant("s-b2", **{"last-dirty": 1})
        self.plant_edits("s-b2", "seats.py")
        self.plant_runs("s-b2", {"token": "vitest", "exit": 0, "digest": "ggg"},
                        {"token": "pytest", "exit": 2, "digest": "rrr"})
        rc, _o, err = self.guard("s-b2")
        self.assertEqual(rc, 2)
        self.assertEqual(err.count("[helm stop-whisper]"), 1)  # one line per stop
        self.assertIn("gate ran RED", err)           # salience: red wins
        self.assertNotIn("gate GREEN", err)
        rc, _o, err = self.guard("s-b2")
        self.assertEqual(rc, 0, err)                 # unbanked NEVER claims green
        self.assertNotIn("gate GREEN", err)          # while a red gate stands

    def test_byte_cap_holds_with_a_maximal_token(self):
        seats.join(session="s-c1", seat="wisp", cwd="/tmp/p")
        self.plant_runs("s-c1", {"token": "t" * 80, "exit": 1, "digest": "aaa"})
        rc, _o, err = self.guard("s-c1")
        self.assertEqual(rc, 2)
        line = next(l for l in err.splitlines() if "stop-whisper" in l)
        self.assertLessEqual(len(line.encode()), seats.STOP_WHISPER_CAP)

    def test_kill_switch_and_fail_closed_on_garbled_logs(self):
        seats.join(session="s-k1", seat="wisp", cwd="/tmp/p")
        self.plant_runs("s-k1", {"token": "pytest", "exit": 1, "digest": "aaa"})
        os.environ["HELM_STOP_GUARD_WHISPER"] = "0"
        rc, _o, err = self.guard("s-k1")
        self.assertEqual(rc, 0, err)                 # off = silent
        os.environ.pop("HELM_STOP_GUARD_WHISPER")
        # garbled command-log + edit-targets: silence, never a raise
        seats.join(session="s-k2", seat="wisp2", cwd="/tmp/p")
        self.plant("s-k2", **{"last-dirty": 1})
        sd = record.session_dir("s-k2")
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, "command-log.jsonl"), "w") as f:
            f.write("not json{{\n")
        with open(os.path.join(sd, "edit-targets.log"), "wb") as f:
            f.write(b"\xff\xfe broken\n")
        rc, _o, err = self.guard("s-k2")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stop-whisper", err)

    def test_ledger_rows_stay_ids_only(self):
        seats.join(session="s-l1", seat="wisp", cwd="/tmp/p")
        self.plant_runs("s-l1", {"token": "pytest", "exit": 1, "digest": "abc"})
        self.assertEqual(self.guard("s-l1")[0], 2)
        lp = os.path.join(home.global_dir(), ".state", "stop-whisper-ledger.jsonl")
        with open(lp) as f:
            rows = [json.loads(l) for l in f]
        self.assertEqual(rows[0]["id"], "redgate:abc:1")
        self.assertNotIn("text", rows[0])


class ClaimsTest(SeatsBase):
    def test_lease_is_the_capability_composite_binding(self):
        ok, msg, lease = seats.claim("worktree-main", "alice", ttl=60, session="sA")
        self.assertTrue(ok)
        self.assertIn("fence 1", msg)
        # another party — even under the SAME display name/session — refused
        ok, _m, _l = seats.claim("worktree-main", "alice", ttl=60, session="sB")
        self.assertFalse(ok)
        # extend needs the FULL binding: lease + holder seat (+ session match)
        ok, _m, _l = seats.claim("worktree-main", "alice", ttl=120, session="sA")
        self.assertFalse(ok)                     # no lease — name+session ≠ enough
        ok, _m, lease2 = seats.claim("worktree-main", "alice", ttl=120,
                                     lease=lease, session="sA")
        self.assertTrue(ok)
        self.assertEqual(lease, lease2)          # stable across the extend
        # release: validated TOGETHER, never lease-OR-session
        ok, msg = seats.release("worktree-main", "alice")             # bare name
        self.assertFalse(ok)
        self.assertIn("capability", msg)
        ok, _m = seats.release("worktree-main", "eve", lease=lease)   # wrong seat
        self.assertFalse(ok)
        ok, _m = seats.release("worktree-main", "alice", lease=lease,
                               session="sB")                          # wrong session
        self.assertFalse(ok)
        ok, _m = seats.release("worktree-main", "alice", lease=lease, session="sA")
        self.assertTrue(ok)
        self.assertEqual(seats.claims_list(), [])

    def test_cli_cannot_assert_a_copied_session(self):
        """Codex B2's exact reproduction, CLI-level: caller B copies A's
        roster-visible SID; --session no longer exists and the ambient env
        session opens nothing without the lease capability."""
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sA"}):
            rc, out, _ = self.cmd("claim", ["port:1", "--seat", "alice"])
        self.assertEqual(rc, 0)
        # B knows sA (roster/API) and even sets it as their ambient session
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sA"}):
            rc, _out, err = self.cmd("release", ["port:1", "--seat", "alice"])
        self.assertEqual(rc, 1)
        self.assertIn("capability", err)
        # the old flag is dead: passing it changes nothing
        rc, _out, err = self.cmd("release", ["port:1", "--seat", "alice",
                                             "--session", "sA"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(seats.claims_list()), 1)   # still held
        # the printed lease IS the capability
        lease = out.split("lease ")[1].split(",")[0]
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sA"}):
            rc, _out, _e = self.cmd("release", ["port:1", "--seat", "alice",
                                                "--lease", lease])
        self.assertEqual(rc, 0)

    def test_aba_stale_holder_cannot_release_regrant(self):
        """H9 ABA: A's lease expires, B claims; stale A (same name, old
        lease) must not drop B's lease."""
        _ok, _m, lease_a = seats.claim("port-8900", "alice", ttl=0, session="sA")
        ok, _m, lease_b = seats.claim("port-8900", "alice", ttl=60, session="sB")
        self.assertTrue(ok)                     # expired A swept, B granted
        ok, _m = seats.release("port-8900", "alice", lease=lease_a, session="sA")
        self.assertFalse(ok)                    # stale nonce refused
        ok, _m = seats.release("port-8900", "alice", lease=lease_b, session="sB")
        self.assertTrue(ok)

    def test_fence_increments_and_list_hides_binding_material(self):
        _ok, m1, _l = seats.claim("r", "a", ttl=0, session="s1")
        _ok, m2, _l = seats.claim("r", "b", ttl=60, session="s2")
        self.assertIn("fence 1", m1)
        self.assertIn("fence 2", m2)
        row = seats.claims_list()[0]
        self.assertNotIn("lease", row)       # the capability is never listed
        self.assertNotIn("session", row)

    def test_release_unclaimed(self):
        ok, msg = seats.release("ghost", "alice", lease="deadbeef")
        self.assertFalse(ok)
        self.assertIn("not claimed", msg)

    def test_list_poll_with_live_claims_never_writes(self):
        """Day-review #4: the roster GET polls claims_list every 3s — a
        read with every claim live must leave .claims.json byte-for-byte
        alone (same inode, same mtime), not rewrite it under the lock."""
        seats.claim("db-migrate", "alice", ttl=60, session="sA")
        p = seats.claims_path()
        before = os.stat(p)
        for _ in range(3):
            self.assertEqual(len(seats.claims_list()), 1)
        after = os.stat(p)
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))

    def test_list_persists_only_an_actual_expiry_sweep(self):
        """The GC leg still works: a row that really expired is dropped
        from the listing AND from disk — one write, then reads go quiet."""
        seats.claim("keep", "bob", ttl=60, session="sB")
        seats.claim("gone", "alice", ttl=0, session="sA")   # expired at birth
        rows = seats.claims_list()
        self.assertEqual([r["resource"] for r in rows], ["keep"])
        on_disk = pk.read_json(seats.claims_path(), {})
        self.assertNotIn("gone", on_disk)                   # sweep persisted
        before = os.stat(seats.claims_path())
        seats.claims_list()                                 # next poll: pure read
        self.assertEqual(os.stat(seats.claims_path()).st_mtime_ns,
                         before.st_mtime_ns)


class CouncilDeferredTest(SeatsBase):
    def test_council_verbs_point_at_the_deferral(self):
        rc, _out, err = self.cmd("verdict", ["topic", "text"])
        self.assertEqual(rc, 2)
        self.assertIn("deferred to 0.3", err)
        rc, _out, err = self.cmd("reveal", ["topic"])
        self.assertEqual(rc, 2)


class RosterReportTest(SeatsBase):
    def test_report_presence_pending_preview_claims(self):
        seats.join(seat="alice", cwd="/tmp/projx", session="s-a")
        chat.post("@alice one", who="bob")
        chat.post("@alice two", who="bob")
        seats.claim("db-migrate", "alice", ttl=60, session="s-a")
        rep = seats.roster_report("main")
        s = rep["seats"][0]
        self.assertEqual(s["seat"], "alice")
        self.assertEqual(s["presence"], "fresh")
        self.assertEqual(s["pending"], 2)
        self.assertIn("two", s["preview"])
        self.assertEqual(rep["claims"][0]["resource"], "db-migrate")
        # the report never moves the cursor — both rows still deliver
        self.assertIn("one", seats.deliver(seat="alice"))

    def test_presence_tiers_from_seen_file(self):
        seats.write_roster("old-seat")
        old = time.time() - 1000        # absent (>QUIET_S) but under REAP_S
        os.utime(seats.seen_path("old-seat"), (old, old))
        rep = seats.roster_report("main")
        self.assertEqual(rep["seats"][0]["presence"], "absent")

    def test_cli_seats_table(self):
        seats.join(seat="alice", cwd="/tmp/p")
        rc, out, _ = self.cmd("seats")
        self.assertEqual(rc, 0)
        self.assertIn("alice", out)
        self.assertIn("fresh", out)


class RosterReaperTest(SeatsBase):
    """G-roster-reaper: the roster only ever grew — permanently-absent /tmp
    throwaways piled up with orphan cursor/seen/latch files."""

    def test_stale_row_and_orphan_state_reaped_fresh_survives(self):
        seats.join(seat="fresh", session="s-f", cwd="/tmp/p")
        seats.join(seat="stale", session="s-s", cwd="/tmp/p")
        with open(seats._stop_fp_path("main", "stale", "s-s"), "w") as f:
            f.write("fp")                       # a stop latch orphan too
        old = time.time() - 2 * seats.REAP_S
        os.utime(seats.seen_path("stale"), (old, old))
        reaped = seats.reap_roster()
        self.assertEqual(reaped, ["stale"])
        self.assertNotIn("stale", seats.roster())
        self.assertIn("fresh", seats.roster())          # fresh row survives
        names = os.listdir(chat.chat_dir())
        self.assertFalse([n for n in names
                          if seats._seat_key("stale") in n])   # whole tail gone
        self.assertTrue([n for n in names
                         if seats._seat_key("fresh") in n])    # fresh state kept

    def test_report_reaps_and_cli_hides_absent_behind_all(self):
        seats.join(seat="live", session="s-l", cwd="/tmp/p")
        seats.join(seat="gone", session="s-g", cwd="/tmp/p")
        old = time.time() - 2 * seats.REAP_S
        os.utime(seats.seen_path("gone"), (old, old))
        rep = seats.roster_report("main")               # the report's GC leg
        self.assertEqual([s["seat"] for s in rep["seats"]], ["live"])
        # absent-but-not-yet-reap-age rows hide behind --all in the CLI
        seats.write_roster("napping")
        nap = time.time() - seats.QUIET_S - 60
        os.utime(seats.seen_path("napping"), (nap, nap))
        rc, out, _ = self.cmd("seats")
        self.assertEqual(rc, 0)
        self.assertIn("live", out)
        self.assertNotIn("napping", out)
        self.assertIn("hidden", out)
        rc, out, _ = self.cmd("seats", ["--all"])
        self.assertIn("napping", out)

    def test_fresh_roster_poll_never_writes(self):
        """The web panel polls the report every 3s — an all-fresh roster must
        stay byte-identical (same inode, same mtime), never churn."""
        seats.join(seat="alice", session="s-a", cwd="/tmp/p")
        p = seats.roster_path()
        before = os.stat(p)
        for _ in range(3):
            seats.roster_report("main")
        after = os.stat(p)
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))


class RoomLockTest(SeatsBase):
    def test_concurrent_appenders_under_tiny_rotation_cap(self):
        """Codex C4's demanded test: concurrent writers crossing the
        rotation cap must never tear/interleave a JSON row, and the newest
        rows must survive rotation."""
        import threading
        with mock.patch.object(chat, "SIZE_CAP", 1500):
            def blast(name):
                for i in range(20):
                    chat.post("msg %02d from %s" % (i, name), who=name)
            threads = [threading.Thread(target=blast, args=("w%d" % k,))
                       for k in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            chat.post("sentinel-after-storm", who="final")
        with open(chat.room_path("main"), encoding="utf-8") as f:
            raw = [x for x in f.read().split("\n") if x]
        rows = [json.loads(x) for x in raw]          # every line parses — no tears
        self.assertTrue(all(isinstance(r, dict) for r in rows))
        self.assertEqual(rows[-1]["text"], "sentinel-after-storm")


class WebRosterTest(SeatsBase):
    def test_endpoint_shape_and_fail_open(self):
        seats.join(seat="alice", cwd="/tmp/p")
        obj, code = web._api_chat_roster({})
        self.assertEqual(code, 200)
        self.assertEqual(obj["seats"][0]["seat"], "alice")
        with mock.patch.object(seats, "roster_report", side_effect=RuntimeError):
            obj, code = web._api_chat_roster({})
        self.assertEqual(code, 200)
        self.assertEqual(obj, {"seats": [], "claims": [], "unavailable": True})


class RoomsSummarySeatsTest(SeatsBase):
    """The sidebar's per-channel roster: web._rooms_summary folds each room's
    present seats (recent posters + cursor-holders, presence-tagged) so the
    owner sees WHO is in a channel, not just its name."""

    def test_rooms_summary_lists_recent_posters_with_presence(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("hello from alice", who="alice")
        rooms = {r["room"]: r for r in web._rooms_summary()}
        names = [s["seat"] for s in rooms["main"]["seats"]]
        self.assertIn("alice", names)
        st = next(s for s in rooms["main"]["seats"] if s["seat"] == "alice")
        self.assertEqual(st["presence"], "fresh")

    def test_rooms_summary_scopes_seats_to_their_room(self):
        seats.join(seat="alice", cwd="/tmp/p")
        seats.join(seat="bob", cwd="/tmp/p")
        chat.post("alice in main", who="alice")
        chat.post("bob in side", who="bob", room="side")
        rooms = {r["room"]: r for r in web._rooms_summary()}
        self.assertEqual([s["seat"] for s in rooms["main"]["seats"]], ["alice"])
        self.assertEqual([s["seat"] for s in rooms["side"]["seats"]], ["bob"])

    def test_rooms_summary_includes_consumer_who_never_posted(self):
        seats.join(seat="quiet-seat", cwd="/tmp/p")
        chat.post("@quiet-seat ping", who="someone-else")
        seats.deliver(seat="quiet-seat")          # consumes -> cursor off > 0
        rooms = {r["room"]: r for r in web._rooms_summary()}
        names = [s["seat"] for s in rooms["main"]["seats"]]
        self.assertIn("quiet-seat", names)

    def test_rooms_summary_bare_baseline_is_not_presence(self):
        # join baselines a cursor in EVERY room; a seat that never posted or
        # consumed in a room must NOT show as present there (bob joined while
        # only main existed -> holds a main baseline at off 0).
        seats.join(seat="bob", cwd="/tmp/p")
        chat.post("noise", who="someone-else")
        rooms = {r["room"]: r for r in web._rooms_summary()}
        names = [s["seat"] for s in rooms["main"]["seats"]]
        self.assertNotIn("bob", names)

    def test_rooms_summary_owner_rows_are_not_seats(self):
        chat.post("owner words", who="david")   # owner rail, not a roster seat
        rooms = {r["room"]: r for r in web._rooms_summary()}
        names = [s["seat"] for s in rooms.get("main", {}).get("seats", [])]
        self.assertNotIn("david", names)

    def test_rooms_summary_fail_open_when_roster_breaks(self):
        chat.post("x", who="alice")
        with mock.patch.object(seats, "roster", side_effect=RuntimeError):
            rooms = {r["room"]: r for r in web._rooms_summary()}
        self.assertEqual(rooms["main"]["seats"], [])   # degraded, never fatal


class ChatDispatchTest(SeatsBase):
    def test_chat_verbs_reach_seats(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = chat.cmd_chat(["claim", "res-1", "--seat", "alice"])
        self.assertEqual(rc, 0)
        self.assertEqual(seats.claims_list()[0]["holder"], "alice")


if __name__ == "__main__":
    unittest.main()
