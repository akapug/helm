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

from helm import chat, pk, seats, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_DELIVER",
            "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


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

    def test_owner_rule_trusts_only_server_rails(self):
        """Codex C1-lite: a CLI post claiming an owner name is an ordinary
        message — only rows the web/TUI rails stamped get the owner rule."""
        row = lambda **kw: dict({"ts": "t", "from": "david",
                                 "text": "no mention"}, **kw)
        self.assertFalse(seats.deliverable(row(), "alice"))              # spoofable CLI
        self.assertTrue(seats.deliverable(row(origin="web"), "alice"))   # owner rail
        self.assertTrue(seats.deliverable(row(origin="tui"), "alice"))
        self.assertFalse(seats.deliverable(row(origin="cli"), "alice"))
        # an owner-named CLI row still reaches a seat it @mentions
        self.assertTrue(seats.deliverable(
            {"ts": "t", "from": "david", "text": "@alice go"}, "alice"))

    def test_owner_names_env_override(self):
        os.environ["HELM_CHAT_OWNER_NAMES"] = "boss, Chief"
        self.assertEqual(seats.owner_names(), {"boss", "chief"})
        self.assertTrue(seats.deliverable(
            {"ts": "t", "from": "Boss", "text": "go", "origin": "web"}, "alice"))


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

    def test_owner_rail_post_delivers_without_mention(self):
        self.seat_up()
        chat.post("course correction", who="david", origin="web")
        self.assertIn("david: course correction", seats.deliver(seat="alice"))

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
        chat.post("steer", who="david", origin="web")
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

    def test_unknown_session_self_heals_roster(self):
        chat.post("noise", who="bob")
        self.assertIsNone(seats.deliver(session="brand-new-session"))
        self.assertIn("agent-brand-ne", seats.roster())
        chat.post("@agent-brand-ne go", who="bob")
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
        old = time.time() - 10_000
        os.utime(seats.seen_path("old-seat"), (old, old))
        rep = seats.roster_report("main")
        self.assertEqual(rep["seats"][0]["presence"], "absent")

    def test_cli_seats_table(self):
        seats.join(seat="alice", cwd="/tmp/p")
        rc, out, _ = self.cmd("seats")
        self.assertEqual(rc, 0)
        self.assertIn("alice", out)
        self.assertIn("fresh", out)


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


class ChatDispatchTest(SeatsBase):
    def test_chat_verbs_reach_seats(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = chat.cmd_chat(["claim", "res-1", "--seat", "alice"])
        self.assertEqual(rc, 0)
        self.assertEqual(seats.claims_list()[0]["holder"], "alice")


if __name__ == "__main__":
    unittest.main()
