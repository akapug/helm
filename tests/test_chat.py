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
import subprocess
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
            self.assertEqual(len(m["id"]), 12)   # the stable per-row id
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
        chat.post("agents?", who="owner")
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
        chat.post("fleet, look alive", who="owner")
        chat.mark_owner_unread()
        rc, out, _ = self.run_cmd(["read"])
        self.assertEqual(rc, 0)
        self.assertIn("owner: fleet, look alive", out)
        self.assertFalse(os.path.exists(chat.marker_path("main")))

    def test_reflex_fires_while_marker_exists_and_not_after(self):
        home.scaffold_global()  # seeds the pack; marker path resolves to tmp
        self.assertEqual(reflex.fire("any turn text"), [])
        chat.post("ship it", who="owner")
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
        (owner-requested alias)."""
        rc, _, err = self.run_cmd(["roster"])
        self.assertEqual(rc, 0)
        self.assertNotIn("unknown subcommand", err)


class RowIntegrityTest(ChatBase):
    def test_unicode_line_separator_never_tears_the_row(self):
        """U+2028/U+2029 inside a message (a voice paste can carry them) must
        not split the JSON row for readers — read() splits on exactly \\n,
        never str.splitlines() (found in delivery testing)."""
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
    All three findings on the first cut are pinned here: whole-body
    scanning made flag-prose unsendable, single-dash flags still broadcast,
    and the tests sat after the __main__ guard where direct unittest
    execution never discovered them (this class now precedes it)."""

    def setUp(self):
        super().setUp()
        # Ambient actor for this class: the `--seat tester` calls below ASSERT
        # this session identity (the post-actor-binding contract) — they do not
        # SELECT another seat. Identity is incidental here: the class tests
        # leading-flag PARSING, not who signs.
        os.environ["HELM_CHAT_NAME"] = "tester"

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
        # Review note: startswith("--") left `-x` broadcasting (-h is now a help
        # ask, answered rc 0 by the dispatcher gate — see HelpBeforeWorkTest)
        rc, _ = self._post("-x")
        self.assertEqual(rc, 2)
        self.assertEqual(self._rows(), [])

    def test_help_gets_usage_not_a_broadcast(self):
        # upgraded from refusal (rc 2) to an ANSWER (rc 0, usage on stdout)
        # by the dispatcher help gate; still never a broadcast
        for flag in ("--help", "-h"):
            err, out = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                rc = chat.cmd_chat(["post", flag])
            self.assertEqual(rc, 0, flag)
            self.assertIn("usage:", out.getvalue())
        self.assertEqual(self._rows(), [])

    def test_prose_about_flags_is_sendable(self):
        # Review note: the first cut scanned the WHOLE body, so ordinary dev chat
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
    hooks BEFORE their fail-open guards could catch it (a composition review found this; main handled it, the lane regressed it). seats.safe_cwd
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

class HelpBeforeWorkTest(ChatBase):
    """--help is answered at the dispatcher, BEFORE any verb runs (the
    block-before-help class, live-probed): `wait --help` entered
    the wait loop and blocked forever — the mandatory-first-action verb every
    new seat probes — and join/deliver/claim/log-flush DID WORK under --help
    (`claim --help` leased a resource named "--help"). The seats/node/meld
    legs are patched to raise, so a regression fails FAST instead of hanging
    the suite."""

    def setUp(self):
        super().setUp()
        # `--seat t` in the body tests below ASSERTS this ambient identity
        # (post-actor-binding contract); the class tests the help gate, not
        # signing, so the actor is incidental.
        os.environ["HELM_CHAT_NAME"] = "t"

    def _no_dispatch(self, args):
        """cmd_chat(args) with every downstream leg booby-trapped: reaching
        one means the help gate did not fire. chat._follow is trapped too —
        the read --follow leg dispatches inline in cmd_chat, and without the
        trap a gate regression HANGS the suite instead of failing fast."""
        from helm import seats, chatnode, meld
        boom = mock.Mock(side_effect=AssertionError(
            "dispatched past the --help gate"))
        with mock.patch.object(seats, "cmd", boom), \
                mock.patch.object(chatnode, "cmd_node", boom), \
                mock.patch.object(meld, "cmd", boom), \
                mock.patch.object(chat, "_follow", boom):
            return self.run_cmd(args)

    def test_wait_help_answers_before_the_loop(self):
        # THE bug: rc 124/143, zero output, harness timeout. Pin: rc 0 +
        # usage naming every flag's semantics, loop never entered.
        for argv in (["wait", "--help"], ["wait", "-h"],
                     ["wait", "--seat", "s", "--help"]):
            rc, out, err = self._no_dispatch(argv)
            self.assertEqual(rc, 0, argv)
            for token in ("--seat", "--room", "--follow", "--timeout",
                          "--any", "timeout"):
                self.assertIn(token, out, argv)
            self.assertEqual(err, "")

    def test_every_seat_verb_answers_help_without_running(self):
        for verb in ("join", "deliver", "stop-guard", "seats", "seat", "dm",
                     "claim", "release", "claims"):
            rc, out, _ = self._no_dispatch([verb, "--help"])
            self.assertEqual(rc, 0, verb)
            self.assertIn("usage: helm chat %s" % verb, out)

    def test_node_and_meld_answer_help_without_dispatch(self):
        # meld's species spellings (council/standup) answer in their own
        # voice — each usage line names the typed verb, not the genus
        for verb in ("node", "meld", "council", "standup"):
            rc, out, _ = self._no_dispatch([verb, "--help"])
            self.assertEqual(rc, 0, verb)
            self.assertIn("usage: helm chat %s" % verb, out)

    def test_read_follow_help_returns(self):
        # read --follow --help blocked forever too (same class, chat-local);
        # _no_dispatch traps _follow so a regression fails, never hangs
        rc, out, _ = self._no_dispatch(["read", "--follow", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("usage: helm chat read", out)

    def test_verdict_and_reveal_answer_help_with_the_deferral(self):
        # the only dispatchable chat verbs deferred to 0.3: --help answers
        # honestly (rc 0 + the deferral) instead of the verb's bare rc 2
        for verb in ("verdict", "reveal"):
            rc, out, _ = self._no_dispatch([verb, "--help"])
            self.assertEqual(rc, 0, verb)
            self.assertIn("usage: helm chat %s" % verb, out)
            self.assertIn("0.3", out)

    def test_room_flag_refuses_a_flag_shaped_value(self):
        # THE residual: the --room pop ran BEFORE the help gate and consumed
        # '--help' as the room name — `wait --room --help` blocked forever
        # (rc 124, zero output) and `post --room --help x` posted into a
        # room literally named "--help". A flag-shaped value is never a
        # room name: refuse fast, before any dispatch.
        for argv in (["wait", "--room", "--help"],
                     ["read", "--room", "--help", "--follow"],
                     ["post", "--room", "--help", "hello"],
                     ["read", "--room"]):
            rc, _, err = self._no_dispatch(argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("--room wants a room name", err, argv)
        self.assertEqual(chat.read("--help")[1], 0)   # no room "--help"

    def test_seat_flag_refuses_a_flag_shaped_value(self):
        # same class, the identity flag: `post --seat --help hi` posted AS a
        # seat named "--help". Non-text verbs never get here — the gate's
        # whole-tail scan answers `wait --seat --help` as help first.
        for argv in (["post", "--seat", "--help", "hi"],
                     ["dm", "--seat", "--help", "codex", "hi"],
                     ["post", "--seat"]):
            rc, _, err = self._no_dispatch(argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("--seat wants a seat name", err, argv)
        self.assertEqual(chat.read()[1], 0)
        rc, out, _ = self._no_dispatch(["wait", "--seat", "--help"])
        self.assertEqual(rc, 0)                       # help wins at the gate
        self.assertIn("usage: helm chat wait", out)

    def test_dm_flag_refuses_a_flag_shaped_value(self):
        rc, _, err = self._no_dispatch(["post", "--dm", "--help", "hi"])
        self.assertEqual(rc, 2)
        self.assertIn("--dm wants a seat name", err)
        self.assertEqual(chat.read()[1], 0)

    def test_leading_help_with_a_body_refuses_loudly(self):
        # the judged contract: bare leading --help = usage rc 0 (pinned in
        # test_inert_verbs...); leading --help WITH a body = rc 2, usage on
        # stderr, NOTHING sent — never success-code a dropped message
        for argv in (["post", "--help", "me", "with", "this"],
                     ["reply", "--help", "1", "still", "broken"],
                     ["dm", "--help", "codex", "hi"]):
            rc, out, err = self._no_dispatch(argv)
            self.assertEqual(rc, 2, argv)
            self.assertEqual(out, "", argv)
            self.assertIn("usage: helm chat %s" % argv[0], err, argv)
            self.assertIn("NOTHING was sent", err, argv)
        self.assertEqual(chat.read()[1], 0)

    def test_dashdash_sends_a_body_that_starts_with_help(self):
        # the deliberate path the refusal points at stays open
        rc, _, _ = self.run_cmd(["post", "--seat", "t", "--",
                                 "--help", "me", "with", "this"])
        self.assertEqual(rc, 0)
        self.assertEqual(chat.read()[0][-1]["text"], "--help me with this")

    def test_help_asks_never_block_end_to_end(self):
        # the class's live symptom was rc 124 under harness timeout: pin the
        # never-blocks property through the real entrypoint, hard timeout
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for argv, want in ((["wait", "--help"], 0),
                           (["wait", "--room", "--help"], 2),
                           (["read", "--room", "--help", "--follow"], 2)):
            p = subprocess.run(
                [sys.executable, "-m", "helm", "chat"] + argv, cwd=root,
                capture_output=True, text=True, timeout=15)
            self.assertEqual(p.returncode, want, (argv, p.stderr))

    def test_inert_verbs_answer_help_without_side_effects(self):
        for verb in ("react", "rooms", "verify", "log-flush", "post",
                     "reply"):
            rc, out, _ = self.run_cmd([verb, "--help"])
            self.assertEqual(rc, 0, verb)
            self.assertIn("usage: helm chat %s" % verb, out)
        self.assertEqual(chat.read()[1], 0)   # nothing posted, nothing leased

    def test_claim_help_creates_no_lease(self):
        rc, out, _ = self._no_dispatch(["claim", "--help"])
        self.assertEqual(rc, 0)
        from helm import seats
        self.assertEqual(seats.claims_list(), [])

    def test_prose_about_help_still_sends(self):
        # free-text verbs scan only the LEADING position: a body ABOUT
        # --help must stay sendable (post's flag-refusal scope law)
        rc, out, _ = self.run_cmd(["post", "--seat", "t", "run", "it",
                                   "with", "--help", "first"])
        self.assertEqual(rc, 0)
        self.assertEqual(chat.read()[0][-1]["text"],
                         "run it with --help first")

    def test_roster_alias_shares_the_seats_help(self):
        rc, out, _ = self._no_dispatch(["roster", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("usage: helm chat seats", out)


class SeatActorBindingTest(ChatBase):
    """Outcome controls (cross-family review): --seat is an ASSERTION, not
    a signer selector. An actor (ambient HELM_CHAT_NAME) that ASSERTS a
    DIFFERENT --seat produces NO effect at all — no row, no DM spool change,
    no ACK transition, no signer call — refused BEFORE any of them. Actor
    with omitted/equal --seat succeeds, and the signer sees the AMBIENT actor,
    never the claim. Real dm/ack verbs, not `post --dm`.
    (Bug it closes: `helm chat dm X --seat kimi` used to sign the DM AS kimi;
    `helm chat ack --seat kimi` used to forge kimi's signed ACK.)"""

    def setUp(self):
        super().setUp()
        os.environ["HELM_CHAT_NAME"] = "seat-a"     # the ambient actor
        self._ss = mock.patch.object(chat, "_sign_send",
                                     return_value=(None, chat._diag(
                                         "send_failed", "off")))
        self.ss = self._ss.start()
        self.addCleanup(self._ss.stop)

    def _rows(self, room="main"):
        return chat.read(room)[0]

    @contextlib.contextmanager
    def _signer_configured(self):
        """Genuinely turn the signed transport ON so _sign_send is REACHABLE on
        a success path. ChatBase runs transport-off, where _sign_send is never
        called on ANY path — which alone makes a mismatch's assert_not_called
        VACUOUS (it passes whether or not the refusal fired). Under this, the
        equal/omitted path DOES call _sign_send, so a mismatch's no-call is a
        real, discriminating control (cross-family review note)."""
        from helm import cell as cellmod
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        ready = {"configured": True, "usable": True, "state": "ready",
                 "reason": None}
        with mock.patch.object(cellmod, "bin_status", return_value=ready), \
                mock.patch.object(chat, "node_head",
                                  return_value={"chain_index": 1}):
            yield

    # ---- POST ------------------------------------------------------------
    def test_post_seat_mismatch_refuses_before_any_row(self):
        rc, _out, err = self.run_cmd(["post", "hi", "--seat", "seat-b"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot act as another seat", err)
        self.assertEqual(self._rows(), [])          # NO row appended
        self.ss.assert_not_called()                 # NO signer call

    def test_post_ambient_signs_as_the_actor_not_the_claim(self):
        # This lane's exact contract: the CLI threads NO claimed profile into
        # the signing owner. post() passes profile=None -> _signed_row falls
        # back to the AMBIENT cell profile (cell.profile_name()), never the
        # --seat claim. Re-adding profile=seat (the bug this closes) makes the
        # profile arg non-None on the equal path and fails here. Mismatch never
        # reaches _signed_row at all — the refusal tests prove that.
        with mock.patch.object(chat, "_signed_row",
                               wraps=chat._signed_row) as sr:
            rc, _o, _e = self.run_cmd(["post", "hi", "--seat", "seat-a"])  # equal
        self.assertEqual(rc, 0)
        self.assertEqual(self._rows()[-1]["from"], "seat-a")   # ambient identity
        self.assertIsNone(sr.call_args.args[2])                # profile: no claim

    def test_post_no_seat_uses_ambient(self):
        rc, _o, _e = self.run_cmd(["post", "hi"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._rows()[-1]["from"], "seat-a")

    # ---- REPLY (post --reply-to) ----------------------------------------
    def test_reply_seat_mismatch_refuses(self):
        self.run_cmd(["post", "parent"])
        before = len(self._rows())
        rc, _o, err = self.run_cmd(
            ["post", "child", "--reply-to", "1", "--seat", "seat-b"])
        self.assertEqual(rc, 2)
        self.assertEqual(len(self._rows()), before)   # no child row
        self.assertIn("cannot act as another seat", err)

    # ---- REACT -----------------------------------------------------------
    def test_react_seat_mismatch_refuses_the_toggle(self):
        self.run_cmd(["post", "target"])
        self.ss.reset_mock()
        rc, _o, err = self.run_cmd(["react", "1", ":fire:", "--seat", "seat-b"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot act as another seat", err)
        self.ss.assert_not_called()
        # no reaction landed on the row
        self.assertFalse(any(r.get("react") for r in self._rows()))

    # ---- DM verb (the confirmed forgery) --------------------------------
    def test_dm_verb_seat_mismatch_signs_nothing_as_another(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = chat.cmd_chat(["dm", "codex", "secret", "--seat", "seat-b"])
        self.assertEqual(code, 2)
        self.assertIn("cannot act as another seat", err.getvalue())
        self.ss.assert_not_called()                 # no DM signed as seat-b
        # DIRECT spool control (cross-family review): the recipient's private lane
        # never grew — the refused DM produced no row anywhere, not just no sig
        self.assertEqual(chat.read(chat.dm_room("codex"))[1], 0)

    def test_dm_verb_ambient_delivers_as_the_actor(self):
        rc, _o, _e = self.run_cmd(["dm", "codex", "hello"])
        self.assertEqual(rc, 0)
        lane = chat.read(chat.dm_room("codex"))[0]
        self.assertTrue(lane and lane[-1]["from"] == "seat-a")

    # ---- ACK verb (the forgeable obligation clear) ----------------------
    def test_ack_verb_seat_mismatch_never_transitions(self):
        from helm import seats
        # seat-a posts a row addressed to seat-c so there IS an ackable row
        self.run_cmd(["post", "@seat-c please ack"])
        rid = self._rows()[-1]["id"]
        self.ss.reset_mock()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = chat.cmd_chat(["ack", rid, "done", "--seat", "seat-c"])
        self.assertEqual(code, 2)
        self.assertIn("cannot act as another seat", err.getvalue())
        self.ss.assert_not_called()                 # no forged signed ACK
        self.assertFalse(any(r.get("ack") == rid for r in self._rows()))

    # ---- NON-VACUOUS controls: prove the no-signer assertions discriminate --
    # cross-family review note: the mismatch tests above run transport-off,
    # where _sign_send is never called on ANY path, so their assert_not_called
    # alone is vacuous. These run with the signer GENUINELY configured, so the
    # signer IS reached on success and the refusal's no-call is a real control.
    def test_post_mismatch_reaches_no_signer_with_transport_on(self):
        with self._signer_configured():
            # positive control: the equal-actor path DOES reach _sign_send
            rc, _o, _e = self.run_cmd(["post", "one", "--seat", "seat-a"])
            self.assertEqual(rc, 0)
            self.assertGreaterEqual(self.ss.call_count, 1)   # signer WAS reached
            self.ss.reset_mock()
            # the refusal: a mismatch reaches the (now-live) signer ZERO times
            rc, _o, err = self.run_cmd(["post", "two", "--seat", "seat-b"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot act as another seat", err)
        self.ss.assert_not_called()                          # NOW discriminating
        self.assertFalse(any(r["text"] == "two" for r in self._rows()))

    def test_dm_and_ack_mismatch_reach_no_signer_with_transport_on(self):
        # dm + ack are THE confirmed forgeries — prove the refusal stops the
        # signer even when signing is genuinely reachable (non-vacuous).
        with self._signer_configured():
            self.ss.reset_mock()
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                dm_rc = chat.cmd_chat(["dm", "codex", "x", "--seat", "seat-b"])
            self.assertEqual(dm_rc, 2)
            self.ss.assert_not_called()                      # no DM signed
            self.assertEqual(chat.read(chat.dm_room("codex"))[1], 0)  # no spool
            # a real ackable row (seat-a's own post signs; reset before the ack)
            self.run_cmd(["post", "@seat-c please ack"])
            rid = self._rows()[-1]["id"]
            self.ss.reset_mock()
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                ack_rc = chat.cmd_chat(["ack", rid, "done", "--seat", "seat-c"])
            self.assertEqual(ack_rc, 2)
            self.ss.assert_not_called()                      # no forged signed ACK
            self.assertFalse(any(r.get("ack") == rid for r in self._rows()))


if __name__ == "__main__":
    unittest.main()
