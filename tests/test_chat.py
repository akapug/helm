#!/usr/bin/env python3
"""helm chat — RAM-room groupchat tests. Hermetic: HELM_CHAT_DIR + HELM_HOME
are tmp dirs (read through home.env at call time); the real /dev/shm/helm-chat
and ~/.helm are never touched. --follow itself is interactive and untested;
the polling read primitive it loops on (chat.read) is pinned here."""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, home, reflex  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE")


class ChatBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-chat-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        # SET-BUT-EMPTY disables the signed transport — v1 behavior, hermetic
        # even when a real room node is live on this machine
        os.environ["HELM_CHAT_NODE_URL"] = ""
        # cwd hermeticity: the default room resolves through seats.
        # resolve_homing, which derives a project room from a git cwd — run
        # from tmp (not the helm checkout) so defaults stay 'main'
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, args, stdin_text=""):
        out, err = io.StringIO(), io.StringIO()
        stdin_prior = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = chat.cmd_chat(list(args))
        finally:
            sys.stdin = stdin_prior
        return rc, out.getvalue(), err.getvalue()


class RoomTest(ChatBase):
    def test_post_read_roundtrip_and_since(self):
        chat.post("first", who="a1")
        chat.post("second", who="a2")
        msgs, total = chat.read()
        self.assertEqual(total, 2)
        self.assertEqual([m["text"] for m in msgs], ["first", "second"])
        for m in msgs:
            self.assertEqual(sorted(m), ["from", "id", "text", "ts"])
            self.assertEqual(len(m["id"]), 12)   # the stable per-row id (H5)
        tail, total = chat.read(since=1)
        self.assertEqual(total, 2)
        self.assertEqual([m["text"] for m in tail], ["second"])
        self.assertEqual(chat.read(since=2), ([], 2))  # caught up

    def test_since_past_the_end_resets(self):
        # a rotation shrank the room under a poller — it re-syncs, never starves
        chat.post("only", who="a1")
        msgs, total = chat.read(since=99)
        self.assertEqual((len(msgs), total), (1, 1))

    def test_env_dir_and_room_layout(self):
        chat.post("hi", who="a1")
        self.assertEqual(chat.chat_dir(), os.environ["HELM_CHAT_DIR"])
        path = chat.room_path("main")
        self.assertTrue(path.startswith(os.environ["HELM_CHAT_DIR"]))
        with open(path) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "hi")  # one JSON obj/line
        mode = stat.S_IMODE(os.stat(chat.chat_dir()).st_mode)
        self.assertEqual(mode, 0o700)  # the room dir is the operator's

    def test_from_env_name_wins(self):
        os.environ["HELM_CHAT_NAME"] = "pilot"
        self.assertEqual(chat.post("x")["from"], "pilot")

    def test_unparseable_lines_skipped(self):
        chat.post("good", who="a1")
        with open(chat.room_path("main"), "a") as f:
            f.write("not json\n[1,2]\n")
        msgs, total = chat.read()
        self.assertEqual(total, 1)
        self.assertEqual(msgs[0]["text"], "good")

    def test_rotation_keeps_the_newest_half(self):
        with mock.patch.object(chat, "SIZE_CAP", 400):
            for i in range(20):
                chat.post("msg-%02d" % i, who="a1")
        msgs, total = chat.read()
        self.assertLess(total, 20)                       # oldest half rotated out
        self.assertEqual(msgs[-1]["text"], "msg-19")     # newest kept
        self.assertNotIn("msg-00", [m["text"] for m in msgs])
        self.assertLessEqual(os.path.getsize(chat.room_path("main")), 400)


class MarkerTest(ChatBase):
    def test_mark_and_consume_semantics(self):
        chat.post("agents?", who="david")
        chat.mark_owner_unread()
        mp = chat.marker_path("main")
        with open(mp) as f:
            self.assertEqual(f.read(), "1")  # the count at owner-post time
        self.assertFalse(chat.consume(total=0))   # not consumed past the post
        self.assertTrue(os.path.exists(mp))
        self.assertTrue(chat.consume(total=1))    # seen it -> cleared
        self.assertFalse(os.path.exists(mp))
        self.assertFalse(chat.consume(total=9))   # no marker -> nothing to clear

    def test_cli_read_clears_the_marker(self):
        chat.post("fleet, look alive", who="david")
        chat.mark_owner_unread()
        rc, out, _ = self.run_cmd(["read"])
        self.assertEqual(rc, 0)
        self.assertIn("david: fleet, look alive", out)
        self.assertFalse(os.path.exists(chat.marker_path("main")))

    def test_reflex_fires_while_marker_exists_and_not_after(self):
        home.scaffold_global()  # seeds the pack; marker path resolves to tmp
        self.assertEqual(reflex.fire("any turn text"), [])
        chat.post("ship it", who="david")
        chat.mark_owner_unread()
        fired = [e["id"] for e in reflex.fire("any turn text")]
        self.assertEqual(fired, ["owner-chat-unread"])
        self.run_cmd(["read"])  # the read consumes past the owner's post
        self.assertEqual(reflex.fire("any turn text"), [])


class CmdTest(ChatBase):
    def test_post_and_read_cycle(self):
        rc, out, _ = self.run_cmd(["post", "hello", "crew"])
        self.assertEqual(rc, 0)
        self.assertIn("hello crew", out)
        rc, out, _ = self.run_cmd(["read"])
        self.assertEqual(rc, 0)
        self.assertIn("hello crew", out)

    def test_read_since_flag(self):
        chat.post("one", who="a1")
        chat.post("two", who="a1")
        rc, out, _ = self.run_cmd(["read", "--since", "1"])
        self.assertEqual(rc, 0)
        self.assertNotIn("one", out)
        self.assertIn("two", out)

    def test_rooms_listing(self):
        chat.post("hi", who="a1")
        chat.post("ops talk", room="ops", who="a2")
        chat.mark_owner_unread("ops")
        rc, out, _ = self.run_cmd(["rooms"])
        self.assertEqual(rc, 0)
        self.assertIn("main  1 msg", out)
        self.assertIn("ops  1 msg [owner-unread]", out)

    def test_list_rooms_is_the_one_sorted_source(self):
        self.assertEqual(chat.list_rooms(), [])          # none until a post
        chat.post("hi", who="a1")                        # -> main
        chat.post("ops", room="ops", who="a2")
        chat.post("zeta", room="zeta", who="a3")
        self.assertEqual(chat.list_rooms(), ["main", "ops", "zeta"])  # sorted

    def test_room_flag_scopes_post_and_read(self):
        self.assertEqual(self.run_cmd(["post", "sidebar", "--room", "ops"])[0], 0)
        self.assertEqual(chat.read("main"), ([], 0))
        rc, out, _ = self.run_cmd(["read", "--room", "ops"])
        self.assertIn("sidebar", out)

    def test_default_io_consumes_the_one_homing_resolver(self):
        """The roster-scatter hole codex probed: the SessionStart join homed
        the seat to its project room (seats.resolve_homing, cwd-derived), but
        a no---room `helm chat post` privately defaulted to env-or-'main' and
        wrote ZERO rows to that home. Default chat I/O now consumes the SAME
        resolver: post and read land in the derived project room; --room
        still wins; a project-less cwd (the base-class chdir) keeps main."""
        import subprocess
        from helm import seats
        repo = os.path.join(self.tmp, "proj-a")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True,
                       capture_output=True)
        home_room = seats.derive_home_room(repo)
        self.assertIsNotNone(home_room)         # the join's answer, one truth
        os.chdir(repo)
        rc, out, _ = self.run_cmd(["post", "to", "my", "home"])
        self.assertEqual(rc, 0)
        self.assertIn("[%s]" % home_room, out)
        self.assertEqual(chat.read("main"), ([], 0))    # zero rows to main
        self.assertEqual(chat.read(home_room)[1], 1)    # the home room got it
        rc, out, _ = self.run_cmd(["read"])             # default read: home too
        self.assertIn("to my home", out)
        self.assertEqual(
            self.run_cmd(["post", "aside", "--room", "main"])[0], 0)
        self.assertEqual(chat.read("main")[1], 1)       # --room still beats

    def test_env_room_homes_the_default(self):
        """Team-room homing (slice 3): HELM_CHAT_ROOM re-homes every no---room
        verb — exactly what the hooks call — while an explicit --room still
        wins; un-homed sessions keep main (test_post_and_read_cycle)."""
        os.environ["HELM_CHAT_ROOM"] = "team-x"
        rc, out, _ = self.run_cmd(["post", "homed", "hello"])
        self.assertEqual(rc, 0)
        self.assertIn("[team-x]", out)
        self.assertEqual(chat.read("main"), ([], 0))     # nothing leaked to main
        self.assertEqual(chat.read("team-x")[1], 1)
        rc, out, _ = self.run_cmd(["read"])              # default read: homed too
        self.assertIn("homed hello", out)
        self.assertEqual(self.run_cmd(["post", "aside", "--room", "main"])[0], 0)
        self.assertEqual(chat.read("main")[1], 1)        # --room beats the env
        chat.mark_owner_unread("team-x")                 # a homed read consumes
        self.assertEqual(self.run_cmd(["read"])[0], 0)   # its OWN room's marker
        self.assertFalse(os.path.exists(chat.marker_path("team-x")))

    def test_join_dispatch_preserves_explicit_room_provenance(self):
        os.environ["HELM_CHAT_ROOM"] = "project-a"
        with mock.patch("helm.seats.cmd", return_value=0) as cmd:
            self.assertEqual(chat.cmd_chat(["join", "--room", "main"]), 0)
        cmd.assert_called_once_with(
            "join", [], "main", room_explicit=True, room_source=None)

    def test_join_dispatch_preserves_derived_environment_provenance(self):
        os.environ["HELM_CHAT_ROOM"] = "project-a"
        os.environ["HELM_CHAT_ROOM_SOURCE"] = "derived"
        with mock.patch("helm.seats.cmd", return_value=0) as cmd:
            self.assertEqual(chat.cmd_chat(["join"]), 0)
        cmd.assert_called_once_with(
            "join", [], "project-a", room_explicit=False,
            room_source="derived")

    def test_preferred_room_does_not_inherit_legacy_source(self):
        os.environ["HELM_CHAT_ROOM"] = "explicit-new"
        os.environ["MELD_CHAT_ROOM_SOURCE"] = "derived"
        with mock.patch("helm.seats.cmd", return_value=0) as cmd:
            self.assertEqual(chat.cmd_chat(["join"]), 0)
        cmd.assert_called_once_with(
            "join", [], "explicit-new", room_explicit=False,
            room_source=None)

    def test_empty_read_and_bad_args(self):
        rc, out, _ = self.run_cmd(["read"])
        self.assertEqual(rc, 0)
        self.assertIn("no messages", out)
        self.assertEqual(self.run_cmd(["post"])[0], 2)            # nothing to say
        self.assertEqual(self.run_cmd(["read", "--since", "x"])[0], 2)
        self.assertEqual(self.run_cmd(["post", "x", "--room"])[0], 2)
        self.assertEqual(self.run_cmd(["bogus"])[0], 2)

    def test_rooms_empty(self):
        rc, out, _ = self.run_cmd(["rooms"])
        self.assertEqual(rc, 0)
        self.assertIn("no rooms yet", out)

    def test_roster_aliases_seats(self):
        """`helm chat roster` is a friendlier spelling of `seats` — it must
        reach the same dispatch (rc 0), never the unknown-subcommand path
        (owner asked for the alias 2026-07-21)."""
        rc, _, err = self.run_cmd(["roster"])
        self.assertEqual(rc, 0)
        self.assertNotIn("unknown subcommand", err)


class RowIntegrityTest(ChatBase):
    def test_unicode_line_separator_never_tears_the_row(self):
        """U+2028/U+2029 inside a message (a voice paste can carry them) must
        not split the JSON row for readers — read() splits on exactly \\n,
        never str.splitlines() (found by the delivery lane 2026-07-20)."""
        chat.post("voice paste second visual line third", who="bob")
        rows, total = chat.read("main")
        self.assertEqual(total, 1)
        self.assertIn(" ", rows[0]["text"])
        chat.post("padding", who="bob")
        p = chat.room_path("main")
        chat._rotate(p, cap=1)   # force rotation through the same split law
        rows, total = chat.read("main")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["text"], "padding")


class PostUnknownFlagTest(ChatBase):
    """post REFUSES an unrecognised LEADING flag instead of publishing it —
    and ONLY leading flags: the body is prose and may talk about flags freely.
    All three xrev findings on the first cut are pinned here: whole-body
    scanning made flag-prose unsendable, single-dash flags still broadcast,
    and the tests sat after the __main__ guard where direct unittest
    execution never discovered them (this class now precedes it)."""

    def _post(self, *args):
        import contextlib
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = chat.cmd_chat(["post"] + list(args))
        return rc, err.getvalue()

    def _rows(self, room="main"):
        return chat.read(room)[0]

    def test_a_misremembered_addressing_flag_is_refused_not_posted(self):
        rc, err = self._post("--to", "codex-orch", "hello")
        self.assertEqual(rc, 2)
        self.assertIn("--to", err)
        self.assertEqual(self._rows(), [])

    def test_single_dash_flags_are_refused_too(self):
        # xrev: startswith("--") left `-h` and `-x` broadcasting
        for flag in ("-h", "-x"):
            rc, _ = self._post(flag)
            self.assertEqual(rc, 2, flag)
        self.assertEqual(self._rows(), [])

    def test_help_gets_usage_not_a_broadcast(self):
        rc, err = self._post("--help")
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)
        self.assertEqual(self._rows(), [])

    def test_prose_about_flags_is_sendable(self):
        # xrev: the first cut scanned the WHOLE body, so ordinary dev chat
        # about CLI flags was unsendable outside stdin
        rc, _ = self._post("--seat", "tester", "please", "use", "--force", "carefully")
        self.assertEqual(rc, 0)
        self.assertIn("--force", self._rows()[0]["text"])

    def test_double_dash_delimiter_sends_a_flag_shaped_body(self):
        rc, _ = self._post("--seat", "tester", "--", "--to", "is", "not", "a", "flag")
        self.assertEqual(rc, 0)
        self.assertTrue(self._rows()[0]["text"].startswith("--to"))

    def test_prose_dash_starters_are_body_not_flags(self):
        # "-" bullets and "->" arrows are not flag-shaped
        rc, _ = self._post("--seat", "tester", "->", "see", "the", "board")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._rows()), 1)

    def test_the_refusal_names_the_real_addressing_flag(self):
        _, err = self._post("--to", "someone", "hi")
        self.assertIn("--dm", err)

    def test_a_plain_message_still_posts(self):
        rc, _ = self._post("--seat", "tester", "an ordinary message")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._rows()), 1)


class DeletedCwdTest(ChatBase):
    """A session whose process cwd was DELETED (a pruned lane worktree — a
    ROUTINE lifecycle state here) must keep chatting. The homing prologue's
    eager os.getcwd() crashed every default chat verb AND all three delivery
    hooks BEFORE their fail-open guards could catch it (fable composition
    HIGH @ 8313d9f; main handled this, the lane regressed it). seats.safe_cwd
    fails open to None -> un-homed -> #main; the session lives."""

    def _delete_cwd(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        os.chdir(d)
        os.rmdir(d)
        self.assertRaises(OSError, os.getcwd)   # the probe's precondition

    def _hook(self, args, payload=b"{}"):
        """chat.cmd_chat with hook-JSON stdin + FD-1 capture (the hook emit
        writes fd 1 directly — invisible to redirect_stdout)."""
        import types
        fake = types.SimpleNamespace(buffer=io.BytesIO(payload))
        r, w = os.pipe()
        saved = os.dup(1)
        os.dup2(w, 1)
        os.close(w)
        try:
            with mock.patch.object(sys, "stdin", fake), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = chat.cmd_chat(list(args))
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

    def test_default_chat_io_survives_a_deleted_cwd(self):
        self._delete_cwd()
        rc, _, err = self.run_cmd(["post", "still", "alive"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cmd(["read"])
        self.assertEqual(rc, 0, err)
        self.assertIn("still alive", out)

    def test_all_three_delivery_hooks_survive_a_deleted_cwd(self):
        payload = json.dumps({"session_id": "s-del-cwd"}).encode("utf-8")
        self._delete_cwd()
        rc, out = self._hook(["join", "--hook-json"], payload)
        self.assertEqual(rc, 0)
        # the join RAN and emitted its identity line — a crash swallowed by
        # a fail-open guard would also rc 0, but emit nothing
        self.assertIn("SessionStart", out)
        rc, _ = self._hook(["deliver", "--hook-json"], payload)
        self.assertEqual(rc, 0)
        rc, _ = self._hook(["stop-guard", "--hook-json"], payload)
        self.assertEqual(rc, 0)

    def test_seat_add_homing_resolves_from_a_deleted_cwd(self):
        """The same eager-getcwd class at seat.py's _resolve_homing: `helm
        seat add --room X` from a deleted cwd must resolve, not crash."""
        from helm import seat as seat_mod
        from helm import seats
        self._delete_cwd()
        self.assertIsNone(seats.safe_cwd())
        self.assertEqual(seat_mod._resolve_homing("ops"), ("ops", None))
        self.assertEqual(seat_mod._resolve_homing(None), (None, None))


if __name__ == "__main__":
    unittest.main()
