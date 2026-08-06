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
import subprocess
import sys
import tempfile
import time
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, dispatches, home, pk, proxywatch, record, seats, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_DELIVER",
            "HELM_STOP_GUARD", "HELM_STOP_GUARD_INBOX",
            "HELM_STOP_GUARD_CLAIMS", "HELM_STOP_GUARD_INDEX",
            "HELM_STOP_GUARD_WHISPER", "HELM_STOP_GUARD_BEACON",
            "HELM_STOP_GUARD_CLAIME", "HELM_STOP_GUARD_DELEGATION",
            "HELM_STOP_GUARD_WIRING", "HELM_STOP_GUARD_LEASE_TTL",
            "HELM_STOP_GUARD_NDP", "HELM_STOP_GUARD_SPIRAL",
            "HELM_STOP_GUARD_PUNT",
            "HELM_SCRATCH_GC", "HELM_SCRATCH_DIR", "HELM_CACHE_DIR",
            "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR", "HELM_PROC", "MELD_PROC",
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
        # PROJECT-LESS BY CONSTRUCTION, not by env plant. chat.post's room
        # default now DERIVES (env seam, then cwd project); this file's tests
        # exercise that very seam through seats.join homing, so planting
        # HELM_CHAT_ROOM=main here would promote every join to the explicit
        # tier and change what they test (a homed-at-main seat hears owner
        # posts a un-homed one must not). Instead the process runs from the
        # tmp fixture — a genuinely project-less helm — so the default
        # honestly fail-opens to #main (the old behavior), the sanitize
        # still proves no ambient env leaks, and homing tests keep passing
        # their rooms/cwds explicitly.
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "owner"
        # hermetic by law: the stop-guard's silent index-cap leg targets the
        # adopted claude memory dir — point it at tmp so no test can ever
        # touch the live MEMORY.md.
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        # SAME LAW for the stop-guard's other silent leg: the scratch reaper
        # (scratch.auto_gc) DELETES dead-session scratch under the real
        # /tmp/claude-* harness estate. A test must never mutate a harness
        # store, so the reaper is off here; test_scratch.py drives it against
        # a fixture tree, and a tripwire there pins this very setting.
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        # Session retention consults same-UID process cmdlines/environments. A
        # short synthetic SID can otherwise collide with an unrelated live
        # process, so cap tests read an empty fixture proc tree; dedicated
        # liveness tests populate their own HELM_PROC fixtures.
        proc = os.path.join(self.tmp, "proc")
        os.makedirs(proc)
        os.environ["HELM_PROC"] = proc

    def tearDown(self):
        os.chdir(self.cwd_prior)
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
        sys.stdout.flush()  # do not capture buffered output from an earlier test
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
        self.assertFalse(seats.deliverable({"ts": "t", "from": "owner",
                                            "react": "🔥", "tts": "t", "tfrom": "x"},
                                           "alice"))
        self.assertFalse(seats.deliverable(row("x", "ping @alice-2"), "alice"))

    def test_reaction_addition_addresses_only_the_target_author(self):  # noqa: VACUOUS_ASSERTION — every negative has a one-field positive twin below, plus the unconditional muted-room positive proves the reaction clause is reached
        reaction = {"ts": "r", "from": "bob", "react": "🔥",
                    "tts": "target-ts", "tfrom": "alice"}
        muted = {"home": "main", "mute": {"side"}, "tracked": True}
        self.assertTrue(seats.deliverable(
            reaction, "alice", room="side", scope=muted, ambient=False),
            "the target author is mention-tier even in a muted foreign room")
        cases = (
            (reaction, "carol", dict(reaction, tfrom="carol"),
             "non-author becomes author"),
            (dict(reaction, **{"from": "alice"}), "alice", reaction,
             "self-reaction becomes someone else's reaction"),
            (dict(reaction, un=True), "alice", reaction,
             "tombstone becomes an addition"),
            (dict(reaction, restored=1), "alice", reaction,
             "restored history becomes live"),
            (dict(reaction, ambient=True), "alice", reaction,
             "ambient machine state becomes a reaction"),
            (dict(reaction, ack="row-id"), "alice", reaction,
             "sender-pulled ack becomes a reaction"),
        )
        for silent, seat, live, label in cases:
            self.assertFalse(seats.deliverable(silent, seat), label)
            self.assertTrue(seats.deliverable(live, seat),
                            label + " — one-field positive control")

    def test_a_restored_row_never_delivers(self):
        """#114 / replay-presents-as-live: a journal-restored row is HISTORY —
        it carries restored=1, and every delivery surface keys on row id, so
        an unguarded reconstruction mints a fresh obligation, pierces mute as
        a mention, and wakes an armed beacon. Measured 2026-08-03: an owner
        directive from Jul 21 woke opus-integrator a step from acting on it,
        and two seats designed inside a 35-hour-old replayed meld. The row
        stays fully READABLE; only the delivery path drops it."""
        row = lambda frm, text, **kw: dict(
            {"ts": "t", "from": frm, "text": text, "restored": 1}, **kw)
        # every wake tier, each restored: none may deliver
        self.assertFalse(seats.deliverable(row("x", "hey @alice look"), "alice"),
                         "restored mention reached a seat")
        self.assertFalse(seats.deliverable(row("x", "@ALL standup"), "alice"),
                         "restored @all reached a seat")
        self.assertFalse(seats.deliverable(
            row("owner", "@alice go", origin="web"), "alice"),
            "restored owner-rail mention reached a seat")
        self.assertFalse(seats.deliverable(
            row("x", "reply", rfrom="alice", dm=None), "alice", room="main"),
            "restored reply woke the parent author")
        self.assertFalse(seats.deliverable(
            row("x", "secret", dm="alice"), "alice"),
            "restored DM reached its recipient")
        # the positive control: the SAME rows without the flag still deliver
        live = lambda frm, text, **kw: dict({"ts": "t", "from": frm,
                                             "text": text}, **kw)
        self.assertTrue(seats.deliverable(live("x", "hey @alice look"), "alice"),
                        "guard swallowed a live mention")
        self.assertTrue(seats.deliverable(live("x", "secret", dm="alice"),
                                          "alice"),
                        "guard swallowed a live DM")

    def test_owner_rail_post_no_longer_auto_wakes(self):
        """Owner steer 2026-07-21: owner-rail posts are no longer a wake class.
        A server-stamped owner post with no @mention does NOT reach a seat (was:
        the 'owner rule' delivered a web/tui-stamped row); an owner post wakes a
        seat only via an @mention or the seat's home room. OWNER_RAILS/owner_names
        still gate owner IDENTITY for the unread rail + console, just not the
        beacon."""
        row = lambda **kw: dict({"ts": "t", "from": "owner",
                                 "text": "no mention"}, **kw)
        self.assertFalse(seats.deliverable(row(), "alice"))              # spoofable CLI
        self.assertFalse(seats.deliverable(row(origin="web"), "alice"))  # owner rail: no wake now
        self.assertFalse(seats.deliverable(row(origin="tui"), "alice"))
        self.assertFalse(seats.deliverable(row(origin="cli"), "alice"))
        # an owner post that @mentions the seat still wakes it
        self.assertTrue(seats.deliverable(
            {"ts": "t", "from": "owner", "text": "@alice go", "origin": "web"},
            "alice"))

    def test_a_captured_None_cwd_is_an_ANSWER_not_a_request_to_resample(self):
        """OMITTED AND None ARE DIFFERENT ANSWERS (@codex-3, exact probe).

        `_git_owner_handle(cwd=None)` used to mean "no argument given, sample
        it yourself". So a caller that had ALREADY looked and found no cwd —
        safe_cwd() returning None, the deleted-worktree state this seam exists
        for — handed in that None and triggered a SECOND read. Two samples of
        a moving value inside one derivation, and the second can see a
        directory the first did not: the probe [None, /repo-b] derived a name
        from repo-b that the caller never observed.

        THE DERIVATION IS NOT MOCKED, and that is the whole point of this arm.
        My own earlier check patched _git_owner_handle with a lambda, so it
        bypassed the resampling it was written to detect and reported the bug
        CURED while it was live. A test that mocks the function under test
        measures the mock.
        """
        seats._OWNER_NAME = None
        calls = {"n": 0}

        def flaky_cwd():
            calls["n"] += 1
            return None if calls["n"] == 1 else "/repo-b"

        with mock.patch.object(seats, "safe_cwd", flaky_cwd):
            got = seats.owner_name()

        # POSITIVE CONTROL ON REAL DATA, not on the spy: owner_name must
        # still ANSWER. Asserting only the call count would pass on a
        # derivation that returned nothing at all, and "it did not resample"
        # is worthless if it also stopped producing a name.
        self.assertTrue(got, "owner_name produced no handle at all")
        self.assertEqual(calls["n"], 1,
                         "safe_cwd was sampled %d times inside one derivation; "
                         "the captured None was read as 'sample again'"
                         % calls["n"])
        self.assertNotEqual(got, "betaperson",
                            "a handle was derived from a cwd the caller never "
                            "observed")

    def test_an_OMITTED_cwd_still_samples_it(self):
        """THE OVER-CORRECTION CONTROL. A sentinel that never samples would
        satisfy the arm above and break every caller that legitimately omits
        the argument — the seam's own documented default."""
        calls = {"n": 0}

        def counting_cwd():
            calls["n"] += 1
            return None                       # nothing to ask git in

        with mock.patch.object(seats, "safe_cwd", counting_cwd):
            got = seats._git_owner_handle()
        self.assertEqual(calls["n"], 1,
                         "an OMITTED cwd must still be sampled exactly once, "
                         "got %d" % calls["n"])
        self.assertIsNone(got, "no cwd to ask git in must yield no answer")

        # POSITIVE CONTROL PINNED TO A KNOWN INPUT, not to whatever identity
        # this box happens to carry. @codex-3: the first version sampled the
        # AMBIENT cwd and global git identity, so it failed under isolated
        # config AND — worse — a mutation that samples and then substitutes
        # /tmp would PASS under an ambient CI identity, because any handle
        # looked like success. A control that accepts any non-empty answer is
        # not a control.
        repo = tempfile.mkdtemp(prefix="helm-owner-")
        self.addCleanup(shutil.rmtree, repo, True)
        for args in (("init", "-q"), ("config", "user.name", "Zebulon Q")):
            subprocess.run(["git", "-C", repo, *args], check=True,
                           capture_output=True)
        derived = {"n": 0}

        def repo_cwd():
            derived["n"] += 1
            return repo

        with mock.patch.object(seats, "safe_cwd", repo_cwd):
            here = seats._git_owner_handle()
        self.assertEqual(derived["n"], 1, "the omitted cwd was not sampled once")
        self.assertEqual(here, "zebulon",
                         "an OMITTED cwd must derive from the repo it "
                         "SAMPLED; got %r, which means the sample was "
                         "discarded or replaced" % (here,))

    def test_owner_names_env_override(self):
        os.environ["HELM_CHAT_OWNER_NAMES"] = "boss, Chief"
        self.assertEqual(seats.owner_names(), {"boss", "chief"})



class OwnerNameNeverServesAnotherEnvironmentsAnswerTest(unittest.TestCase):
    """THE INVARIANT THAT OUTLIVED THE MEMO.

    Three tests used to live here, each pinning one selector the memo key had
    to name: git's config FILES, then command-scope GIT_CONFIG_COUNT/KEY_n,
    then GIT_DIR. @codex found each in turn, and the fourth finding ended the
    mechanism: `owner_name` cached its FINAL answer, whose fallback is
    getpass.getuser() reading LOGNAME/USER/LNAME/USERNAME, and whose git leg
    can be re-pointed by PATH. The key would have had to name the union of a
    registry file, git's entire config resolution, PATH and four getpass
    variables — and I had called each narrowing DERIVED.

    Measured: the memo saved 1.77 ms on a function with four call sites and no
    loop. It is gone. This test remains because the PROPERTY is what mattered,
    not the mechanism: two different environments must never receive each
    other's owner. That now holds by construction — and this arm fails the
    moment anyone re-introduces a cache without solving the key."""

    def _owner_under(self, login):
        with mock.patch.dict(os.environ, {"LOGNAME": login, "USER": login}):
            with mock.patch("helm.registry.authored_host", return_value={}):
                with mock.patch.object(seats, "_git_owner_handle",
                                       return_value=None):
                    return seats.owner_name()

    def test_two_environments_get_their_own_answers(self):
        first, second = self._owner_under("alpha"), self._owner_under("beta")
        # POSITIVE CONTROL: the derivation must actually answer, or two Nones
        # would differ-not and this would measure nothing.
        self.assertEqual(first, "alpha",
                         "the login fallback did not run (got %r)" % (first,))
        self.assertEqual(second, "beta",
                         "the second environment received %r — a cached "
                         "answer from the first" % (second,))

    def test_no_process_wide_cache_survives(self):
        """The mechanism check, kept deliberately: a module-level dict here is
        how the four-round key problem returns. If a future memo is added, it
        must come with a key that names getpass's variables and PATH too."""
        self.assertFalse(hasattr(seats, "_OWNER_NAME_CACHE"),
                         "a process-wide owner-name cache is back; its key "
                         "must name every input the answer depends on, which "
                         "is why the last one was removed")
        # POSITIVE CONTROL on the same module: the PIN is still there, so an
        # absent attribute above means removed-on-purpose and not a bad import.
        self.assertTrue(hasattr(seats, "_OWNER_NAME"),
                        "the _OWNER_NAME pin vanished — five test files set it")


class OwnerNameTest(SeatsBase):
    """owner_name() — the derived owner handle (host config -> git user.name
    -> login; NO name ships in code) and the posting/recognition SEAM."""

    def setUp(self):
        super().setUp()
        seats._OWNER_NAME = None            # the explicit PIN
        os.environ.pop("HELM_CHAT_OWNER_NAMES", None)

    def tearDown(self):
        seats._OWNER_NAME = None
        super().tearDown()

    def test_host_config_wins_and_normalizes(self):
        with mock.patch("helm.registry.authored_host",
                        return_value={"owner_name": " Boss "}):
            self.assertEqual(seats.owner_name(), "boss")

    def test_git_user_name_first_token_lowercased(self):
        with mock.patch("helm.registry.authored_host", return_value={}), \
             mock.patch.object(seats, "_git_owner_handle",
                               return_value="publicuser"):
            self.assertEqual(seats.owner_name(), "publicuser")

    def test_login_fallback_when_git_absent(self):
        with mock.patch("helm.registry.authored_host", return_value={}), \
             mock.patch.object(seats, "_git_owner_handle", return_value=None), \
             mock.patch("getpass.getuser", return_value="LoginX"):
            self.assertEqual(seats.owner_name(), "loginx")

    def test_git_handle_derivation_and_rejection(self):
        # the seam contract: capture() -> stripped stdout, or None on unset/fail
        be = lambda out: mock.Mock(capture=mock.Mock(return_value=out))
        with mock.patch("helm.vcs.backend", return_value=be("Casey Bright")):
            self.assertEqual(seats._git_owner_handle(), "casey")
        with mock.patch("helm.vcs.backend", return_value=be(None)):
            self.assertIsNone(seats._git_owner_handle())
        with mock.patch("helm.vcs.backend", return_value=be("@@!! zap")):
            self.assertIsNone(seats._git_owner_handle())  # not handle-shaped

    def test_seam_posting_default_is_recognized(self):
        """THE COUPLING INVARIANT: whatever the owner surfaces post under by
        default, the rails RECOGNIZE — through ONE resolver on both sides.
        Derivation mocked to a name that is neither the derived handle nor the login, so
        this FAILS under any hardcoded default on either side (non-vacuity)."""
        from helm import human
        with mock.patch("helm.registry.authored_host", return_value={}), \
             mock.patch.object(seats, "_git_owner_handle", return_value="zed"):
            self.assertEqual(human.operator_name(), "zed")
            self.assertIn("zed", seats.owner_names())

    def test_a_deleted_cwd_reaches_the_login_fallback_instead_of_raising(self):
        """A PRUNED LANE WORKTREE IS ROUTINE HERE — that is why `safe_cwd`
        exists. owner_name's own deleted-cwd branch said "derive fresh, memoise
        nothing", and then derivation re-read the cwd RAW: `_git_owner_handle`
        called a bare os.getcwd() outside its try, so the FileNotFoundError
        came straight out of owner_name and took the posting default and the
        recognition set (owner_names) down with it. Nothing may be memoised
        under a directory that no longer exists, either — a deleted cwd must
        not inherit another directory's handle."""
        gone = os.path.join(self.tmp, "pruned-lane")
        os.makedirs(gone)
        os.chdir(gone)
        os.rmdir(gone)                    # SeatsBase.tearDown chdirs back
        # The condition really holds: safe_cwd fails open to None for exactly
        # one reason — os.getcwd() raised OSError on an unlinked directory.
        self.assertIsNone(seats.safe_cwd())
        with mock.patch("helm.registry.authored_host", return_value={}), \
             mock.patch("getpass.getuser", return_value="LoginX"):
            handle, recognized = seats.owner_name(), seats.owner_names()
        os.chdir(self.tmp)
        self.assertEqual(handle, "loginx")
        self.assertIn("loginx", recognized)
        # THE MEMO IS GONE, so "it memoised nothing" is no longer a
        # statement anyone can make. What still matters is that the very next
        # call, under different conditions, gets ITS OWN answer rather than
        # this one — which is the property the memo assertions were standing
        # in for.
        with mock.patch("helm.registry.authored_host", return_value={}), \
             mock.patch.object(seats, "_git_owner_handle", return_value="zed"):
            self.assertEqual(seats.owner_name(), "zed")

    def test_two_repos_do_not_serve_each_others_owner_handle(self):
        """THE MEMO IS KEYED ON ITS INPUTS. `_git_owner_handle` targets the CWD
        by design — identity is a property of where the human works — so a
        process-wide slot froze whichever repo answered FIRST. Measured before
        the cure: two repos with deliberate user.name AlphaPerson / BetaPerson
        both resolved to 'alphaperson'. Real repos and a real config read, so
        this cannot pass on a mock that forgot which cwd it was asked about."""
        handles = {}
        for name in ("AlphaPerson", "BetaPerson"):
            repo = os.path.join(self.tmp, name.lower())
            os.makedirs(repo)
            for argv in (("init", "-q"), ("config", "user.name", name),
                         ("config", "user.email", "%s@example.com" % name)):
                subprocess.run(["git", "-C", repo, *argv], check=True,
                               capture_output=True, text=True)
            os.chdir(repo)
            with mock.patch("helm.registry.authored_host", return_value={}):
                handles[name] = seats.owner_name()
        os.chdir(self.tmp)
        self.assertEqual(handles, {"AlphaPerson": "alphaperson",
                                   "BetaPerson": "betaperson"})
        # No memo to inspect any more; the dict above IS the property. Two
        # real repos, a real config read, distinct answers — which is what the
        # keyed-memo assertion was proving indirectly.

    def test_unreadable_authored_layer_degrades_loudly_to_derivation(self):
        from helm import registry
        err = io.StringIO()
        with mock.patch("helm.registry.authored_host",
                        side_effect=registry.AuthoredUnreadable("/x/authored")), \
             mock.patch.object(seats, "_git_owner_handle", return_value="gitname"), \
             contextlib.redirect_stderr(err):
            self.assertEqual(seats.owner_name(), "gitname")
        self.assertIn("unreadable", err.getvalue())


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

    # -- the hook read is bounded in TIME, not only in bytes ----------------
    #
    # These use a REAL os.pipe() on purpose. cmd_fd fakes stdin with a
    # BytesIO, which can never block, so the whole suite was structurally
    # incapable of seeing this defect — the fixture was kinder than reality.

    def _real_pipe_stdin(self, data=None, close=True):
        """Patch sys.stdin onto a real pipe. close=False leaves the WRITE end
        open with nothing on it — a reader waiting for a byte that never comes,
        which is exactly the live failure."""
        r, w = os.pipe()
        self.addCleanup(lambda: os.close(r) if r else None)
        if data:
            os.write(w, data)
        if close:
            os.close(w)
        else:
            self.addCleanup(os.close, w)
        fake = types.SimpleNamespace(
            buffer=types.SimpleNamespace(fileno=lambda: r, read=lambda n=-1: b""))
        return mock.patch.object(sys, "stdin", fake)

    def test_a_stalled_stdin_returns_fast_instead_of_blocking_forever(self):
        """THE DEFECT, in the owner's own words: stop-hook errors that kept
        coming back after being "fixed".

        `_hook_stdin` read `sys.stdin.buffer.read(65536)`, and its docstring
        said "bounded" — bounded in BYTES. A buffered read on a pipe that stays
        open blocks until it can fill the buffer or sees EOF, i.e. forever. The
        hook wrapper is `timeout 5 ... ; rc=$?; [ "$rc" = 2 ] && exit 2; exit 0`,
        so a timeout kill (rc 124) is NOT 2 and the wrapper returns 0: THE STOP
        IS ALLOWED AND THE GUARD NEVER RAN. Obligations unchecked, no record.

        Two rounds of fixes to what the guard DECIDES could not help, because
        it was dying before it decided anything."""
        with self._real_pipe_stdin(close=False):
            t0 = time.time()
            d = seats._hook_stdin()
            elapsed = time.time() - t0
        self.assertEqual(d, {}, "a stall yields defaults, never a hang")
        self.assertLess(elapsed, seats.HOOK_STDIN_DEADLINE_S + 2.0,
                        "the read must be bounded in TIME; this is the whole bug")
        self.assertGreaterEqual(elapsed, 0.5, "it should actually wait a little")

    def test_a_stall_leaves_a_receipt_because_a_silent_degrade_hid_this(self):
        """A silent degrade is what made this invisible for as long as it
        lasted — the wrapper converted the kill to exit 0 and nothing anywhere
        recorded it, so 'is the stop hook flaky?' had no answer but a live
        repro."""
        with self._real_pipe_stdin(close=False):
            seats._hook_stdin()
        p = os.path.join(home.helm_home(), home.GLOBAL, ".state",
                         seats._HOOK_STDIN_STALL)
        self.assertTrue(os.path.exists(p), "a stall must leave a record")
        with open(p, encoding="utf-8") as f:
            self.assertIn("stalled after", f.read())

    def test_a_complete_document_still_parses(self):
        with self._real_pipe_stdin(json.dumps({"session_id": "s-9"}).encode()):
            self.assertEqual(seats._hook_stdin()["session_id"], "s-9")

    def test_json_arriving_in_pieces_is_read_whole(self):
        """The read must not stop at the first chunk: a harness is free to
        write its JSON in more than one write, and half a document parses as
        nothing at all."""
        r, w = os.pipe()
        payload = json.dumps({"session_id": "split", "cwd": "/tmp/p"}).encode()
        os.write(w, payload[:10])
        threading.Timer(0.2, lambda: (os.write(w, payload[10:]), os.close(w))).start()
        fake = types.SimpleNamespace(
            buffer=types.SimpleNamespace(fileno=lambda: r, read=lambda n=-1: b""))
        with mock.patch.object(sys, "stdin", fake):
            d = seats._hook_stdin()
        os.close(r)
        self.assertEqual(d.get("session_id"), "split")

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
        chat.post("course correction", who="owner", origin="web")
        self.assertIsNone(seats.deliver(seat="alice"))
        chat.post("@alice course correction", who="owner", origin="web")
        self.assertIn("course correction", seats.deliver(seat="alice"))

    def test_cli_owner_name_does_not_owner_deliver(self):
        self.seat_up()
        chat.post("i am totally the owner", who="owner")   # no rail stamp
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

    def test_credential_wall_pauses_without_consuming_and_recovers_hands_free(self):
        """The complete arc: measured dark pauses BOTH direct and multi-room
        delivery before presence/cursor mutation; UNKNOWN holds the known wall;
        a later HEALTHY record resumes the SAME already-armed delivery path and
        drains the retained mention. No beacon restart or config reread exists
        in the test because neither exists in production."""
        seats.join(session="s-codex-2", seat="codex-2", cwd="/tmp/p")
        chat.post("@codex-2 retained while walled", who="owner")
        cur = dict(seats._cursor("main", "codex-2", "s-codex-2"))
        seen = os.stat(seats.seen_path("codex-2")).st_mtime_ns

        def upstream(state, dark):
            p = proxywatch._state_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            pk.write_json(p, {"ts": time.time(), "upstream": {"codex": {
                "state": state, "dark": dark, "since": "episode-start"}}})

        upstream("RATE-LIMITED", True)
        self.assertIsNone(seats.deliver(
            session="s-codex-2", seat="codex-2"))
        self.assertIsNone(seats.deliver_any(
            session="s-codex-2", seat="codex-2"))
        self.assertEqual(seats._cursor("main", "codex-2", "s-codex-2"), cur)
        self.assertEqual(os.stat(seats.seen_path("codex-2")).st_mtime_ns, seen)

        upstream("UNKNOWN", True)          # blindness is not recovery
        self.assertIsNone(seats.deliver_any(
            session="s-codex-2", seat="codex-2"))
        self.assertEqual(seats._cursor("main", "codex-2", "s-codex-2"), cur)

        upstream("HEALTHY", False)         # measured exit arc, no re-arm
        line = seats.deliver_any(session="s-codex-2", seat="codex-2")
        self.assertIn("retained while walled", line)
        self.assertNotEqual(
            seats._cursor("main", "codex-2", "s-codex-2"), cur)

    def test_a_walled_family_does_not_pause_a_healthy_sibling_family(self):
        seats.join(session="s-codex", seat="codex", cwd="/tmp/p")
        seats.join(session="s-pi-codex", seat="pi-codex", cwd="/tmp/p",
                   runtime={"family": "codex", "agent_harness": "pi"})
        seats.join(session="s-kimi", seat="kimi", cwd="/tmp/p",
                   runtime={"family": "kimi", "agent_harness": "claude"})
        chat.post("@codex held", who="owner")
        chat.post("@pi-codex runtime family held", who="owner")
        chat.post("@kimi proceeds", who="owner")
        p = proxywatch._state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        pk.write_json(p, {"ts": time.time(), "upstream": {
            "codex": {"state": "AUTH-UNAVAILABLE", "dark": True},
            "kimi": {"state": "HEALTHY", "dark": False}}})
        self.assertIsNone(seats.deliver_any(session="s-codex", seat="codex"))
        self.assertIsNone(seats.deliver_any(
            session="s-pi-codex", seat="pi-codex"))
        self.assertIn("@kimi proceeds", seats.deliver_any(
            session="s-kimi", seat="kimi"))

    def test_conamed_sessions_use_their_own_verified_runtime_family(self):
        seats.join(session="s-shared-codex", seat="shared", cwd="/tmp/p",
                   runtime={"family": "codex", "agent_harness": "pi"})
        seats.join(session="s-shared-kimi", seat="shared", cwd="/tmp/p",
                   runtime={"family": "kimi", "agent_harness": "pi"})
        row = seats.roster()["shared"]
        self.assertEqual(row["runtime_sessions"]["s-shared-codex"]
                         ["runtime"]["family"], "codex")
        self.assertEqual(row["runtime_sessions"]["s-shared-kimi"]
                         ["runtime"]["family"], "kimi")
        chat.post("@shared each provider gets its own decision", who="owner")
        pk.write_json(proxywatch._state_path(), {"ts": time.time(),
                      "upstream": {
                          "codex": {"state": "RATE-LIMITED", "dark": True},
                          "kimi": {"state": "HEALTHY", "dark": False}}})
        held = dict(seats._cursor("main", "shared", "s-shared-codex"))
        self.assertIsNone(seats.deliver_any(
            session="s-shared-codex", seat="shared"))
        self.assertEqual(seats._cursor(
            "main", "shared", "s-shared-codex"), held)
        self.assertIn("own decision", seats.deliver_any(
            session="s-shared-kimi", seat="shared"))

    def test_dark_transition_between_room_scan_and_delivery_consumes_nothing(self):  # noqa: VACUOUS_ASSERTION — moved[done] proves the interleaving fired; byte-identical cursor is the forbidden-consumption observable
        seats.join(session="s-codex", seat="codex", cwd="/tmp/p")
        chat.post("@codex race retained", who="owner")
        pk.write_json(proxywatch._state_path(), {"ts": time.time(),
                      "upstream": {"codex": {"state": "HEALTHY",
                                               "dark": False}}})
        cur = dict(seats._cursor("main", "codex", "s-codex"))
        real = seats._room_dirty
        moved = {"done": False}
        def dark_before_delivery(room, seat, session=None):
            dirty = real(room, seat, session)
            if dirty and not moved["done"]:
                moved["done"] = True
                pk.write_json(proxywatch._state_path(), {"ts": time.time(),
                              "upstream": {"codex": {
                                  "state": "AUTH-401", "dark": True}}})
            return dirty
        with mock.patch.object(seats, "_room_dirty",
                               side_effect=dark_before_delivery):
            self.assertIsNone(seats.deliver_any(
                session="s-codex", seat="codex"))
        self.assertTrue(moved["done"], "control: dark landed after initial gate")
        self.assertEqual(seats._cursor("main", "codex", "s-codex"), cur)

    def test_paused_mention_survives_room_rotation_and_drains_on_healthy(self):
        seats.join(session="s-codex", seat="codex", cwd="/tmp/p")
        chat.post("@codex survive rotation", who="owner")
        pk.write_json(proxywatch._state_path(), {"ts": time.time(),
                      "upstream": {"codex": {"state": "AUTH-401",
                                               "dark": True}}})
        with mock.patch.object(chat, "SIZE_CAP", 700):
            for i in range(40):
                chat.post("rotation noise %02d %s" % (i, "x" * 40), who="noise")
        with open(chat.room_path("main"), encoding="utf-8") as handle:
            self.assertIn("survive rotation", handle.read())
        pk.write_json(proxywatch._state_path(), {"ts": time.time(),
                      "upstream": {"codex": {"state": "HEALTHY",
                                               "dark": False}}})
        self.assertIn("survive rotation", seats.deliver_any(
            session="s-codex", seat="codex"))

    def test_corrupt_primary_recovers_dark_latch_and_waits_for_healthy(self):
        seats.join(session="s-codex", seat="codex", cwd="/tmp/p")
        chat.post("@codex observer broke", who="owner")
        dark = {"ts": time.time(), "upstream": {"codex": {
            "state": "AUTH-401", "dark": True, "since": "episode"}}}
        os.makedirs(os.path.dirname(proxywatch._state_path()), exist_ok=True)
        pk.write_json(proxywatch._backup_state_path(), dark)
        with open(proxywatch._state_path(), "w", encoding="utf-8") as handle:
            handle.write("not json")
        cur = dict(seats._cursor("main", "codex", "s-codex"))
        self.assertIsNone(seats.deliver_any(
            session="s-codex", seat="codex"))
        self.assertEqual(seats._cursor("main", "codex", "s-codex"), cur)
        pk.write_json(proxywatch._state_path(), {"ts": time.time(),
                      "upstream": {"codex": {"state": "HEALTHY",
                                               "dark": False}}})
        self.assertIn("observer broke", seats.deliver_any(
            session="s-codex", seat="codex"))

    def test_marker_untouched_by_delivery(self):
        self.seat_up()
        chat.post("@alice steer", who="owner", origin="web")  # a mention delivers
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

    def test_join_baseline_rid_suppresses_replay_after_inode_replacement(self):
        chat.post("@alice stale before join", who="bob")
        self.seat_up()
        cur = seats._cursor("main", "alice")
        self.assertTrue(cur["rid"])
        p = chat.room_path("main")
        with open(p, encoding="utf-8") as f:
            content = f.read()
        os.remove(p)
        pk.atomic_write(p, content)
        self.assertIsNone(seats.deliver(seat="alice"))
        chat.post("@alice fresh after replacement", who="bob")
        self.assertIn("fresh after replacement", seats.deliver(seat="alice"))

    def test_large_rotation_suppresses_prejoin_rows_beyond_first_scan(self):
        """A retained baseline RID can lie beyond SCAN_CAP after the native
        keep-newest-half rotation. Suppression must span bounded scans rather
        than exposing the first window's pre-join mentions."""
        chat._ensure_dir()
        p = chat.room_path("main")
        with open(p, "w", encoding="utf-8") as f:
            for i in range(1000):
                row = {"ts": "t", "from": "bob", "id": "r%06d" % i,
                       "text": "@alice STALE-PRE-JOIN %04d %s" %
                               (i, "x" * 2100)}
                f.write(json.dumps(row) + "\n")
        cap = os.path.getsize(p)
        self.seat_up()
        self.assertEqual(seats._cursor("main", "alice")["rid"], "r000999")
        with mock.patch.object(chat, "SIZE_CAP", cap):
            chat.post("@alice FRESH-AFTER-ROTATION", who="bob")
        self.assertGreater(os.path.getsize(p), seats.SCAN_CAP)
        delivered = [seats.deliver(seat="alice") for _ in range(5)]
        text = "\n".join(x for x in delivered if x)
        self.assertNotIn("STALE-PRE-JOIN", text)
        self.assertIn("FRESH-AFTER-ROTATION", text)

    def test_unknown_session_delivery_obeys_roster_then_cursor_lock_order(self):
        """Force the former cycle: delivery reaches unknown-session roster
        registration while rehome holds the roster lock and baselines cursors.
        Both operations must complete rather than roster<->cursor deadlocking."""
        import threading
        self.seat_up()
        write_entered = threading.Event()
        baseline_entered = threading.Event()
        real_write = seats.write_roster
        real_baseline = seats._baseline_room_cursors
        errors, results = [], []

        def gated_write(*args, **kwargs):
            write_entered.set()
            if not baseline_entered.wait(2):
                errors.append("rehome never reached cursor baselining")
            return real_write(*args, **kwargs)

        def gated_baseline(room, *args, **kwargs):
            if room == "new-room":
                baseline_entered.set()
            return real_baseline(room, *args, **kwargs)

        def run(fn):
            try:
                results.append(fn())
            except Exception as e:
                errors.append(repr(e))

        with mock.patch.object(seats, "write_roster", side_effect=gated_write), \
                mock.patch.object(seats, "_baseline_room_cursors",
                                  side_effect=gated_baseline):
            delivery = threading.Thread(
                target=run, args=(lambda: seats.deliver(
                    session="s-new", seat="alice", room="new-room"),),
                daemon=True)
            delivery.start()
            self.assertTrue(write_entered.wait(1))
            rehome = threading.Thread(
                target=run, args=(lambda: seats.rehome_seat(
                    "alice", "new-room"),), daemon=True)
            rehome.start()
            delivery.join(3)
            rehome.join(3)
        self.assertFalse(delivery.is_alive(), "delivery deadlocked")
        self.assertFalse(rehome.is_alive(), "rehome deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)

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
        chat.post("@alice survives producer error", who="bob")
        with mock.patch.object(seats, "_record_posttool_delegation",
                               side_effect=RuntimeError("boom")):
            rc, out = self.cmd_fd("deliver", ["--hook-json"], stdin=payload)
        self.assertEqual(rc, 0)
        self.assertIn("survives producer error", out)
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

    def test_unrenderable_row_is_skipped_and_the_backlog_drains_past_it(self):
        """A row that PASSES deliverable() but can't be RENDERED — a malformed
        non-string 'text' on a foreign/corrupt jsonl row — raises in
        _scrub/_clip BEFORE the cursor commits. Unhandled it re-raises every
        poll and strands the WHOLE backlog behind it forever. The fix skips it
        (LOUD, cursor advances past it), so a good row AFTER the offender still
        delivers = the backlog DRAINS, not merely 'the waiter survives' (OI's
        acceptance). Pre-fix, 'after' is never delivered (stranded)."""
        self.seat_up()
        seats.dm("alice", "before the bad row", who="bob")   # creates the lane
        lane = seats.dm_lane("alice")
        with open(chat.room_path(lane), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "t", "from": "bob", "dm": "alice",
                                "id": "bad", "text": 123}) + "\n")   # non-str!
            f.write(json.dumps({"ts": "t", "from": "bob", "dm": "alice",
                                "id": "g2", "text": "after the bad row"}) + "\n")
        got = []
        for _ in range(6):                     # drain; a SKIP also returns None
            line = seats.deliver_any(seat="alice")   # (so don't break on it —
            if line:                                 #  the offender halts THIS
                got.append(line)                     #  pass, next resumes past it)
        blob = " ".join(got)
        self.assertIn("before the bad row", blob)
        self.assertIn("after the bad row", blob)   # <- the drain got PAST 'bad'
        self.assertNotIn("123", blob)              # the offender never rendered
        self.assertIsNone(seats.deliver_any(seat="alice"))  # drained, no strand

    def test_unrenderable_main_row_does_not_strand_the_deliverable_scan(self):
        """The COERCION half (deliverable() non-str text -> ''): a MAIN-ROOM row
        with malformed non-string text would make deliverable()'s @mention regex
        .search() raise DURING the backlog scan (not render) — stranding every
        row behind it. The coercion makes it a clean non-match (undeliverable, no
        crash), so a real @mention row AFTER it still delivers. Complements the
        dm/render-raise strand test above; both halves (scan-coercion +
        render-skip) are load-bearing (fable gate, 2026-07-23)."""
        self.seat_up()
        with open(chat.room_path("main"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "t", "from": "bob", "id": "badm",
                                "text": 999}) + "\n")          # non-str, non-dm
            f.write(json.dumps({"ts": "t", "from": "bob", "id": "gm",
                                "text": "@alice after the bad main row"}) + "\n")
        got = []
        for _ in range(6):
            line = seats.deliver(seat="alice")   # a skipped scan-row is None too
            if line:
                got.append(line)
        blob = " ".join(got)
        self.assertIn("after the bad main row", blob)  # scan got PAST the bad row
        self.assertNotIn("999", blob)                  # offender: no mention, undelivered


class WaitTest(SeatsBase):
    def test_wait_returns_pending_and_advances_cursor(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice now", who="bob")
        line = seats.wait(seat="alice", timeout=1, poll=0.01)
        self.assertIn("@alice now", line)
        self.assertIsNone(seats.deliver(seat="alice"))  # no re-nudge

    def test_already_armed_waiter_resumes_in_place_on_healthy_transition(self):
        seats.join(session="s-codex", seat="codex", cwd="/tmp/p")
        chat.post("@codex wake after recovery", who="owner")
        p = proxywatch._state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        pk.write_json(p, {"ts": time.time(), "upstream": {"codex": {
            "state": "RATE-LIMITED", "dark": True}}})
        slept = []
        def recover(_seconds):
            slept.append(True)
            pk.write_json(p, {"ts": time.time(), "upstream": {"codex": {
                "state": "HEALTHY", "dark": False}}})
        with mock.patch.object(seats.time, "sleep", side_effect=recover):
            line = seats.wait(seat="codex", session="s-codex",
                              timeout=1, poll=0.01)
        self.assertTrue(slept, "control: the same waiter observed a paused poll")
        self.assertIn("wake after recovery", line)

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

    def test_reaction_burst_wakes_the_author_once_with_full_identity(self):  # noqa: VACUOUS_ASSERTION — the same emitted line is proved non-empty (len == 1) and contains every reactor/emoji before the noise-absence assertion
        target = chat.post("the row receiving reactions", who="alice")
        seats.join(seat="alice", cwd="/tmp/p")  # baseline after the target
        chat.react(1, "🔥", who="bob")
        chat.post("plain room noise between reactions", who="noise")
        chat.react(1, "❤️", who="carol")
        chat.react(1, "👀", who="dave")
        captured = []
        line = seats.wait(seat="alice", follow=True, timeout=0.1, poll=0.01,
                          emit=captured.append)
        self.assertIsNone(line)
        self.assertEqual(len(captured), 1, captured)
        wake = captured[0]
        self.assertIn("helm chat reaction", wake)
        for token in ("bob reacted 🔥", "carol reacted ❤️",
                      "dave reacted 👀", "alice@" + target["ts"]):
            self.assertIn(token, wake)
        self.assertNotIn("plain room noise", wake)

    def test_large_reaction_burst_keeps_target_and_reports_omissions(self):
        room = "side"
        target = chat.post("large target", who="alice", room=room)
        seats.join(seat="alice", cwd="/tmp/p", room=room)
        for i in range(12):
            chat.react(1, "🔥", who="reactor-%02d-longname" % i, room=room)
        captured = []
        seats.wait(seat="alice", room=room, follow=True, timeout=0.1,
                   poll=0.01, emit=captured.append)
        self.assertEqual(len(captured), 1, captured)
        body = captured[0].split("] ", 1)[1]
        self.assertTrue(body.startswith("alice@%s ← " % target["ts"]), body)
        self.assertIn("reactor-00-longname reacted 🔥", body)
        self.assertIn("more reactions — helm chat read --room side", body)
        self.assertLessEqual(len(body.encode("utf-8")), seats.MAX_BYTES)

    def test_large_dm_reaction_burst_points_to_the_dm_pull_surface(self):
        room = chat.dm_room("alice")
        chat.post("private target", who="alice", room=room)
        seats.join(seat="alice", cwd="/tmp/p")
        seats._init_cursor(room, "alice")
        for i in range(12):
            chat.react(1, "🔥", who="reactor-%02d-longname" % i, room=room)
        line = seats.deliver(room=room, seat="alice")
        self.assertIn("more reactions — helm chat read --dm", line)
        self.assertNotIn("--room", line)

    def test_tool_boundary_coalesces_the_same_burst_as_the_idle_beacon(self):  # noqa: VACUOUS_ASSERTION — the first boundary returns both named reactions before the second-boundary absence proves the cursor consumed the group
        chat.post("active-seat target", who="alice")
        seats.join(seat="alice", cwd="/tmp/p")
        chat.react(1, "🔥", who="bob")
        chat.react(1, "❤️", who="carol")
        line = seats.deliver_any(seat="alice")
        self.assertIn("bob reacted 🔥", line)
        self.assertIn("carol reacted ❤️", line)
        self.assertIsNone(seats.deliver_any(seat="alice"),
                          "the burst left a second boundary wake pending")

    def test_reactions_to_two_recorded_targets_do_not_coalesce(self):
        chat._append({"ts": "2026-08-05T01:00:00Z", "from": "alice",
                      "text": "first target"}, "main")
        chat._append({"ts": "2026-08-05T01:00:01Z", "from": "alice",
                      "text": "second target"}, "main")
        seats.join(seat="alice", cwd="/tmp/p")
        chat.react(1, "🔥", who="bob")
        chat.react(2, "❤️", who="carol")
        captured = []
        seats.wait(seat="alice", follow=True, timeout=0.1, poll=0.01,
                   emit=captured.append)
        self.assertEqual(len(captured), 2, captured)
        self.assertIn("alice@2026-08-05T01:00:00Z", captured[0])
        self.assertIn("alice@2026-08-05T01:00:01Z", captured[1])

    def test_reaction_wakes_neither_room_peers_nor_the_reactor(self):  # noqa: VACUOUS_ASSERTION — the target author's positive wake on the same row proves the delivery path is live before peer/self absence is asserted
        chat.post("target", who="alice")
        seats.join(seat="alice", cwd="/tmp/a")
        seats.join(seat="carol", cwd="/tmp/c")
        chat.react(1, "🔥", who="bob")
        peer = []
        seats.wait(seat="carol", follow=True, timeout=0.05, poll=0.01,
                   emit=peer.append)
        self.assertEqual(peer, [], "a non-author room peer woke")
        author = []
        seats.wait(seat="alice", follow=True, timeout=0.05, poll=0.01,
                   emit=author.append)
        self.assertEqual(len(author), 1)
        chat.react(1, "👀", who="alice")
        own = []
        seats.wait(seat="alice", follow=True, timeout=0.05, poll=0.01,
                   emit=own.append)
        self.assertEqual(own, [], "a self-reaction woke its author/reactor")

    def test_reaction_removal_is_silent_but_a_later_addition_still_wakes(self):  # noqa: VACUOUS_ASSERTION — positive additions wake immediately before and after the removal on the same observable
        chat.post("target", who="alice")
        seats.join(seat="alice", cwd="/tmp/p")
        chat.react(1, "🔥", who="bob")
        first = []
        seats.wait(seat="alice", follow=True, timeout=0.05, poll=0.01,
                   emit=first.append)
        self.assertEqual(len(first), 1)
        off, err = chat.react(1, "🔥", who="bob")  # toggle OFF
        self.assertIsNone(err)
        self.assertTrue(off["un"])
        removed = []
        seats.wait(seat="alice", follow=True, timeout=0.05, poll=0.01,
                   emit=removed.append)
        self.assertEqual(removed, [])
        chat.react(1, "🔥", who="bob")             # toggle ON again
        added = []
        seats.wait(seat="alice", follow=True, timeout=0.05, poll=0.01,
                   emit=added.append)
        self.assertEqual(len(added), 1,
                         "the silent tombstone stranded the later addition")

    def test_wait_follow_bounds_a_backlog_burst_so_the_beacon_cannot_firehose(self):
        """A --follow beacon arming to a large backlog must NOT replay it as one
        burst: each emit is a Monitor event, and >~10 in a burst trips Monitor's
        firehose auto-stop -> SIGTERM, and the seat goes DEAF (live 2026-07-23: a
        ~36-row backlog killed the beacon <8s every arm). ONE drain pass is
        capped at BEACON_DRAIN_CAP emits + a single catch-up nudge, never the
        whole backlog. Pre-fix this pass emitted all 40 (the firehose)."""
        class _PassDone(Exception):
            pass
        seats.join(seat="alice", cwd="/tmp/p")     # baselines the cursor at join
        for i in range(40):
            chat.post("@alice backlog %d" % i, who="bob")
        captured = []
        # Run EXACTLY ONE drain pass: the post-drain sleep raises out of wait(),
        # so `captured` holds precisely what a single arming burst would emit.
        with mock.patch("time.sleep", side_effect=_PassDone):
            with self.assertRaises(_PassDone):
                seats.wait(seat="alice", follow=True, poll=0.01,
                           emit=captured.append)
        self.assertLessEqual(len(captured), seats.BEACON_DRAIN_CAP + 1)
        self.assertLess(len(captured), 40)         # NOT the whole backlog
        # still WAKES the agent and points it at the backlog to catch up
        self.assertTrue(any("more pending" in c for c in captured))

    def test_wait_any_follow_bounds_a_burst_and_streams_the_residue(self):
        """The --any --follow watcher is the SAME firehose class as the seat
        drain: >BEACON_DRAIN_CAP new rows in one poll must NOT emit as one
        Monitor burst (auto-stop -> SIGTERM -> deaf). Each pass emits <=CAP + one
        nudge and advances the watermark only PAST what it emitted, so the residue
        streams the NEXT poll — never a since=total skip that drops rows."""
        class _PassDone(Exception):
            pass
        cap = seats.BEACON_DRAIN_CAP
        n = cap * 2 + 1                         # two full over-cap passes + a tail
        rows = [{"id": "r%d" % i, "from": "bob", "text": "burst %d" % i}
                for i in range(n)]
        reads = {"n": 0}
        loop_since = []
        def fake_read(room="main", since=0):
            reads["n"] += 1
            if reads["n"] == 1:
                return [], 0                   # the arm baseline -> since=0
            loop_since.append(since)           # every loop read's watermark
            return rows[since:], len(rows)
        sleeps = {"n": 0}
        def fake_sleep(*_a):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:               # stop after exactly two poll passes
                raise _PassDone
        emitted = []
        with mock.patch.object(seats.chat, "read", side_effect=fake_read):
            with mock.patch("time.sleep", side_effect=fake_sleep):
                with self.assertRaises(_PassDone):
                    seats.wait(any_row=True, follow=True, poll=0.01,
                               emit=emitted.append)
        # residue preserved: pass 2 read from the ADVANCED watermark (cap), never
        # from total — a since=total skip would have dropped rows[cap:] silently.
        self.assertEqual(loop_since, [0, cap])
        content = [c for c in emitted if "more pending" not in c]
        nudges = [c for c in emitted if "more pending" in c]
        self.assertEqual(len(nudges), 2)               # both passes were over-cap
        self.assertEqual(len(content), 2 * cap)        # cap per pass, none dropped
        self.assertLess(len(content), n)               # never the whole burst

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


class BeaconAmbientDefaultTest(SeatsBase):
    """The beacon's MENTION-ONLY default (owner directive 2026-07-29: beacons
    must not wake on every post in the home room + premise
    mute-busy-home-room-trust-mentions, owner-confirmed 2026-07-22): a
    `wait --follow` beacon wakes on @mentions (ANY room), DMs and @all —
    never on ambient home-room rows. --ambient / ambient=True restores the
    old full-room scope for quiet rooms. Non-beacon shapes keep the FULL
    delivery scope byte-identical: single-shot seat wait, --any, and the
    boundary hook (deliver/deliver_any defaults) are untouched."""

    def beacon(self, seat, session, ambient=None, timeout=0.15):
        captured = []
        line = seats.wait(seat=seat, session=session, follow=True,
                          timeout=timeout, poll=0.01, emit=captured.append,
                          ambient=ambient)
        self.assertIsNone(line)            # --follow returns only on timeout
        return captured

    def test_ambient_home_room_row_does_not_wake_the_default_beacon(self):
        seats.join(session="s-b", seat="bee", cwd="/tmp/p", room="team-b")
        chat.post("plain team chatter, no address", who="bob", room="team-b")
        self.assertEqual(self.beacon("bee", "s-b"), [])
        # the skipped row is CONSUMED (the muted-room semantics): the home
        # room is a PULL surface (`helm chat read`), so neither the boundary
        # hook nor a later beacon replays it as a stale nudge.
        self.assertIsNone(seats.deliver_any(session="s-b", seat="bee"))

    def test_mention_wakes_the_default_beacon_from_a_foreign_room(self):
        seats.join(session="s-m2", seat="mia", cwd="/tmp/p", room="team-m")
        chat.post("@mia over here", who="bob", room="elsewhere")
        got = self.beacon("mia", "s-m2")
        self.assertEqual(len(got), 1)
        self.assertIn("over here", got[0])

    def test_dm_wakes_the_default_beacon(self):
        seats.join(session="s-d", seat="dee", cwd="/tmp/p", room="team-d")
        seats.dm("dee", "private word", who="bob")
        got = self.beacon("dee", "s-d")
        self.assertEqual(len(got), 1)
        self.assertIn("private word", got[0])

    def test_at_all_wakes_the_default_beacon_in_the_home_room(self):
        """ambient=False must fall THROUGH to the @all rule, not swallow the
        home room whole — a home-room @all broadcast still wakes."""
        seats.join(session="s-a2", seat="ada", cwd="/tmp/p", room="team-a")
        chat.post("@all standup now", who="bob", room="team-a")
        got = self.beacon("ada", "s-a2")
        self.assertEqual(len(got), 1)
        self.assertIn("standup now", got[0])

    def test_ambient_true_restores_home_room_wakes(self):
        seats.join(session="s-q", seat="quinn", cwd="/tmp/p", room="team-q")
        chat.post("quiet-room chatter", who="bob", room="team-q")
        got = self.beacon("quinn", "s-q", ambient=True)
        self.assertEqual(len(got), 1)
        self.assertIn("quiet-room chatter", got[0])

    def test_single_shot_seat_wait_keeps_the_full_delivery_scope(self):
        """A non-beacon wait (no --follow) is a DELIVERY, not a beacon: an
        ambient home-room row still returns, byte-identical to before."""
        seats.join(session="s-1", seat="uno", cwd="/tmp/p", room="team-1")
        chat.post("home word for the single shot", who="bob", room="team-1")
        line = seats.wait(seat="uno", session="s-1", timeout=1, poll=0.01)
        self.assertIn("home word for the single shot", line)

    def test_cli_follow_defaults_mention_only_and_ambient_flag_opts_in(self):
        """End-to-end CLI plumbing: bare `wait --follow` skips the ambient
        home-room row; `wait --follow --ambient` streams it."""
        seats.join(session="s-c2", seat="clio", cwd="/tmp/p", room="team-c")
        chat.post("ambient cli chatter", who="bob", room="team-c")
        rc, out, _err = self.cmd("wait", ["--seat", "clio", "--follow",
                                          "--timeout", "0.05"])
        self.assertEqual(rc, 0)
        self.assertNotIn("ambient cli chatter", out)
        chat.post("more cli chatter", who="bob", room="team-c")
        rc, out, _err = self.cmd("wait", ["--seat", "clio", "--follow",
                                          "--ambient", "--timeout", "0.05"])
        self.assertEqual(rc, 0)
        self.assertIn("more cli chatter", out)


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
        chat.post("side-room note", who="owner", origin="web", room="announce")
        self.assertIsNone(seats.deliver_any(session="s-o", seat="oz"))
        chat.post("all hands", who="owner", origin="web")   # main: no longer wakes un-homed
        self.assertIsNone(seats.deliver_any(session="s-o", seat="oz"))
        chat.post("@oz ping", who="owner", origin="web")    # …but a mention does
        self.assertIn("ping", seats.deliver_any(session="s-o", seat="oz"))
        # …and a seat HOMED to the side room hears the owner there
        seats.join(session="s-an", seat="anna", cwd="/tmp/p", room="announce")
        chat.post("announce word", who="owner", origin="web", room="announce")
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
        chat.post("owner direction for team b", who="owner", origin="web",
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
        chat.post("owner in team a", who="owner", origin="web", room="team-a")
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
        chat.post("team b owner note", who="owner", origin="web", room="team-b")
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

    def test_overflow_ring_eventually_reaches_old_foreign_mention(self):
        seats.join(session="s-fair", seat="fair", cwd="/tmp/p",
                   room="home")
        chat.post("@fair stranded-direct", who="bob", room="old-direct")
        for i in range(seats.ROOM_SCAN_CAP + 8):
            chat.post("newer noise", who="bob", room="newer-%02d" % i)
        got = [seats.deliver_any(session="s-fair", seat="fair")
               for _ in range(4)]
        self.assertIn("stranded-direct", "\n".join(x for x in got if x))

    def test_overflow_ring_eventually_exposes_pending_to_stop_guard(self):
        seats.join(session="s-gate", seat="gate", cwd="/tmp/p",
                   room="home")
        chat.post("@gate stranded-pending", who="bob", room="old-pending")
        for i in range(seats.ROOM_SCAN_CAP + 8):
            chat.post("newer noise", who="bob", room="gate-newer-%02d" % i)
        pending = []
        for _ in range(4):
            pending.extend(seats._pending_all("main", "gate", "s-gate"))
            if pending:
                break
        self.assertIn("stranded-pending",
                      "\n".join(row["text"] for _room, row in pending))

    def test_roster_observation_cannot_steal_delivery_scan_slots(self):
        seats.join(session="s-victim", seat="victim", cwd="/tmp/p")
        for i in range(seats.ROOM_SCAN_CAP * 2):
            text = "@victim stranded" if i == 0 else "noise"
            chat.post(text, who="bob", room="f%02d" % i)
        row = next(r for r in seats.roster_report()["seats"]
                   if r["seat"] == "victim")
        self.assertEqual(row["pending"], 1)
        got = seats.deliver_any(session="s-victim", seat="victim")
        self.assertIn("stranded", got)

    def test_roster_observation_cannot_steal_stop_scan_slots(self):
        seats.join(session="s-gate", seat="gate", cwd="/tmp/p")
        for i in range(seats.ROOM_SCAN_CAP * 2):
            text = "@gate stranded" if i == 0 else "noise"
            chat.post(text, who="bob", room="g%02d" % i)
        seats.roster_report()
        blocks, _warns = seats.stop_guard(session="s-gate", seat="gate")
        self.assertTrue(blocks)
        self.assertIn("stranded", "\n".join(blocks))

    def test_overflow_identity_queue_survives_rooms_inserted_before_target(self):
        seats.join(session="s-churn", seat="churn", cwd="/tmp/p")
        for i in range(seats.ROOM_SCAN_CAP - 1):
            chat.post("noise", who="bob", room="a%02d" % i)
        chat.post("@churn stable-target", who="bob", room="z-target")
        self.assertIsNone(seats.deliver_any(session="s-churn", seat="churn"))
        got = []
        for turn in range(4):
            for i in range(seats.ROOM_SCAN_CAP - 1):
                chat.post("new noise", who="bob",
                          room="m%02d-%02d" % (turn, i))
            got.append(seats.deliver_any(session="s-churn", seat="churn"))
        self.assertIn("stable-target", "\n".join(x for x in got if x))

    def test_join_baselines_every_room_beyond_hot_scan_cap(self):
        for i in range(seats.ROOM_SCAN_CAP + 8):
            chat.post("@late stale prejoin", who="bob", room="pre-%02d" % i)
        seats.join(session="s-late", seat="late", cwd="/tmp/p")
        got = [seats.deliver_any(session="s-late", seat="late")
               for _ in range(4)]
        self.assertEqual(got, [None] * 4)


class BeaconScopeTest(SeatsBase):
    """Premise beacon-scope-mentions-plus-home-room-owner-posts-not-all:
    (a) @mention any room ALWAYS; (b) ANYTHING in the home room — at the
    TOOL BOUNDARY (deliver/deliver_any, what this class drives) and under a
    --ambient beacon; the DEFAULT beacon drops tier (b), covered in
    BeaconAmbientDefaultTest; (c) owner
    posts/@all never fleet-wide; (d) mute tunes (b)/(c), never (a)."""

    def test_codex2_repro_foreign_room_mention_wakes_main_homed_beacon(self):
        """THE live bug: codex-2 homed to #main never saw '@codex-2 …'
        posted in #helm-dogfood — the homing allowlist starved the beacon."""
        os.environ["HELM_CHAT_ROOM"] = "main"
        seats.join(session="s-c2", seat="codex-2", cwd="/tmp/p")
        del os.environ["HELM_CHAT_ROOM"]
        self.assertEqual(seats.roster()["codex-2"]["home_room"], "main")
        chat.post("@codex-2 please pick this up", who="owner",
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
        chat.post("owner steering team b", who="owner", origin="web",
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
        chat.post("owner note", who="owner", origin="web")
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
        self.assertIn("[helm chat dm → ben @", line)   # row's own ts follows
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
        sent = {"sent": True, "turn_hash": "a" * 64, "receipt_hash": "b" * 64,
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

    def test_dm_stores_canonical_identity_and_displays_the_roster_seat(self):
        seats.join(session="s-k", seat="Kimi", cwd="/tmp/p")
        row, _err = seats.dm("@KIMI", "case snap", who="ada")
        self.assertEqual(row["dm"], "kimi")
        self.assertEqual(row["dm_display"], "Kimi")
        self.assertIn("-> @Kimi (dm):", chat._fmt(row))
        self.assertIn("case snap", seats.deliver_any(session="s-k", seat="Kimi"))

    def test_prejoin_cross_case_dm_delivers_only_to_the_exact_later_seat(self):
        row, err = seats.dm("@KiMi", "waiting cross-case", who="ada")
        self.assertIsNone(err)
        self.assertEqual((row["dm"], row["dm_display"]), ("kimi", "KiMi"))
        # Positive non-substring control: the distinct token has a different DM
        # lane and can never consume the row merely because its name starts alike.
        seats.join(session="s-k2", seat="kimi-2", cwd="/tmp/p")
        self.assertIsNone(seats.deliver_any(session="s-k2", seat="kimi-2"))
        seats.join(session="s-kl", seat="Kimi", cwd="/tmp/p")
        self.assertIn("waiting cross-case",
                      seats.deliver_any(session="s-kl", seat="Kimi"))

    def test_direct_chat_post_cannot_bypass_canonical_dm_storage(self):  # noqa: VACUOUS_ASSERTION — stored identity and the concrete lane row are both asserted positively; malformed input raises rather than creating an absence arm
        row = chat.post("direct", who="ada", dm="@KiMi")
        self.assertEqual((row["dm"], row["dm_display"]), ("kimi", "KiMi"))
        self.assertEqual(chat.read(chat.dm_room("kimi"))[0][-1]["id"], row["id"])
        with self.assertRaisesRegex(ValueError, "exact seat token"):
            chat.post("bad", who="ada", dm="evil\x1b[2J")

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
        # Act as ada: `--seat ada` on the dm / post --dm SIGNING verbs ASSERTS
        # this ambient identity (post-actor-binding contract); `--seat vic` on
        # the READ below stays a free lane selector. SeatsBase.setUp pops
        # HELM_CHAT_NAME, so this does not leak to sibling tests.
        os.environ["HELM_CHAT_NAME"] = "ada"
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
        state = pk.read_json(home.registry_path(), {"projects": {}})
        state.setdefault("projects", {})[os.path.basename(repo)] = {"path": repo}
        pk.write_json(home.registry_path(), state)
        return repo

    def test_derives_home_from_git_project(self):
        repo = self._repo()
        _seat, line = seats.join(session="s-1", seat="pa", cwd=repo)
        self.assertEqual(seats.roster()["pa"]["home_room"], "proj-alpha")
        self.assertIn("in room proj-alpha", line)

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

    def test_first_derived_home_baselines_backlog_for_legacy_unhomed_row(self):
        repo = self._repo("proj-later")
        seats.join(session="s-old", seat="legacy", cwd=self.tmp)
        chat.post("@legacy stale", who="bob", room="proj-later")
        seats.join(session="s-new", seat="legacy", cwd=repo)
        row = seats.roster()["legacy"]
        self.assertEqual(row["home_room"], "proj-later")
        self.assertEqual(row["home_room_source"], "derived")
        self.assertIsNone(seats.deliver_any(session="s-old", seat="legacy"))
        self.assertIsNone(seats.deliver_any(session="s-new", seat="legacy"))
        chat.post("@legacy fresh", who="bob", room="proj-later")
        self.assertIn("fresh", seats.deliver_any(
            session="s-new", seat="legacy"))

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

    def test_derived_home_moves_with_same_seat_between_projects(self):
        ra, rb = self._repo("proj-a"), self._repo("proj-b")
        seats.join(session="s-move-a", seat="mover", cwd=ra)
        chat.post("@mover stale-b", who="bob", room="proj-b")
        seats.join(session="s-move-b", seat="mover", cwd=rb)
        row = seats.roster()["mover"]
        self.assertEqual(row["home_room"], "proj-b")
        self.assertEqual(row["home_room_source"], "derived")
        self.assertIsNone(seats.deliver_any(session="s-move-a", seat="mover"))
        self.assertIsNone(seats.deliver_any(session="s-move-b", seat="mover"))
        chat.post("@mover fresh-b", who="bob", room="proj-b")
        self.assertIn("fresh-b", seats.deliver_any(
            session="s-move-b", seat="mover"))

    def test_repo_named_main_stays_unhomed(self):
        repo = self._repo(name="main")
        seats.join(session="s-8", seat="ph", cwd=repo)
        self.assertIsNone(seats.roster()["ph"].get("home_room"))

    def test_homed_project_filters_sibling_chatter_not_direct_mentions(self):
        ra, rb = self._repo("proj-a"), self._repo("proj-b")
        seats.join(session="s-a", seat="sea", cwd=ra)   # homed #proj-a
        chat.post("proj-b chatter", who="bob", room="proj-b")
        chat.post("owner in proj-b", who="owner", origin="web", room="proj-b")
        self.assertIsNone(seats.deliver_any(session="s-a", seat="sea"))
        chat.post("@sea proj-b direct", who="bob", room="proj-b")
        self.assertIn("proj-b direct", seats.deliver_any(
            session="s-a", seat="sea"))
        chat.post("proj-a home chatter", who="bob", room="proj-a")
        self.assertIn("proj-a home chatter", seats.deliver_any(
            session="s-a", seat="sea"))

    def test_rehome_seat_deliberate_move_and_clear(self):
        repo = self._repo()
        seats.join(session="s-9", seat="pi", cwd=repo)
        self.assertEqual(seats.roster()["pi"]["home_room"], "proj-alpha")
        ok, msg = seats.rehome_seat("pi", "team-z")
        self.assertTrue(ok, msg)
        self.assertEqual(seats.roster()["pi"]["home_room"], "team-z")
        # Home-room chatter follows the new home immediately, while a direct
        # mention remains cross-room under the beacon-scope law.
        chat.post("proj-a chatter", who="bob", room="proj-a")
        self.assertIsNone(seats.deliver_any(session="s-9", seat="pi"))
        chat.post("@pi proj-a direct", who="bob", room="proj-a")
        self.assertIn("proj-a direct", seats.deliver_any(
            session="s-9", seat="pi"))
        chat.post("team-z home word", who="bob", room="team-z")
        self.assertIn("team-z home word", seats.deliver_any(
            session="s-9", seat="pi"))
        # clear back to un-homed
        ok, msg = seats.rehome_seat("pi", "main")
        self.assertTrue(ok, msg)
        self.assertIsNone(seats.roster()["pi"].get("home_room"))

    def test_explicit_main_beats_environment_and_derivation(self):
        repo = self._repo()
        os.environ["HELM_CHAT_ROOM"] = "team-y"
        seats.join(session="s-main", seat="pm", cwd=repo, room="main",
                   room_explicit=True)
        row = seats.roster()["pm"]
        self.assertEqual(row["home_room"], "main")
        self.assertEqual(row["home_room_source"], "explicit")
        self.assertEqual(seats._scan_rooms("main", seat="pm"), ["main"])
        chat.post("foreign chatter", who="bob", room="team-y")
        self.assertIn("team-y", seats._scan_rooms("main", seat="pm"))
        self.assertIsNone(seats.deliver_any(session="s-main", seat="pm"))
        chat.post("@pm foreign direct", who="bob", room="team-y")
        self.assertIn("foreign direct", seats.deliver_any(
            session="s-main", seat="pm"))

    def test_explicit_main_preserves_pending_main_delivery(self):
        seats.join(session="s-old", seat="main-move", cwd=self.tmp,
                   room="team-a")
        chat.post("@main-move must survive", who="bob", room="main")
        pending = seats._pending_all("team-a", "main-move", "s-old")
        self.assertEqual([row["text"] for _room, row in pending],
                         ["@main-move must survive"])
        seats.join(session="s-new", seat="main-move", cwd=self.tmp,
                   room="main", room_explicit=True)
        self.assertIn("must survive", seats.deliver_any(
            session="s-old", seat="main-move", room="main"))
        self.assertIn("must survive", seats.deliver_any(
            session="s-new", seat="main-move", room="main"))

    def test_preferred_explicit_env_room_ignores_legacy_derived_source(self):
        seats.write_roster("mixed")
        self.assertTrue(seats.rehome_seat("mixed", "operator-home")[0])
        os.environ["HELM_CHAT_ROOM"] = "explicit-new"
        os.environ["MELD_CHAT_ROOM_SOURCE"] = "derived"
        seats.join(session="s-mixed", seat="mixed", cwd=self.tmp)
        row = seats.roster()["mixed"]
        self.assertEqual(row["home_room"], "explicit-new")
        self.assertEqual(row["home_room_source"], "explicit")

    def test_operator_rehome_and_clear_survive_derived_session_start(self):
        repo = self._repo()
        seats.join(session="s-op", seat="po", cwd=repo)
        self.assertTrue(seats.rehome_seat("po", "team-z")[0])
        os.environ["HELM_CHAT_ROOM"] = "proj-alpha"
        os.environ["HELM_CHAT_ROOM_SOURCE"] = "derived"
        seats.join(session="s-op2", seat="po", cwd=repo)
        self.assertEqual(seats.roster()["po"]["home_room"], "team-z")
        self.assertEqual(seats.roster()["po"]["home_room_source"], "operator")
        self.assertTrue(seats.rehome_seat("po", "main")[0])
        seats.join(session="s-op3", seat="po", cwd=repo)
        row = seats.roster()["po"]
        self.assertIsNone(row.get("home_room"))
        self.assertEqual(row["home_room_source"], "operator")

    def test_rehome_baselines_destination_backlog(self):
        ra, rb = self._repo("proj-a"), self._repo("proj-b")
        seats.join(session="s-back", seat="back", cwd=ra)
        chat.post("@back stale", who="bob", room="proj-b")
        self.assertTrue(seats.rehome_seat("back", "proj-b")[0])
        self.assertIsNone(seats.deliver_any(session="s-back", seat="back"))
        chat.post("@back fresh", who="bob", room="proj-b")
        self.assertIn("fresh", seats.deliver_any(session="s-back", seat="back"))

    def test_unhomed_to_homed_rehome_baselines_destination_backlog(self):
        seats.join(session="s-open", seat="open", cwd=self.tmp)
        chat.post("@open stale", who="bob", room="proj-b")
        self.assertTrue(seats.rehome_seat("open", "proj-b")[0])
        self.assertIsNone(seats.deliver_any(session="s-open", seat="open"))
        chat.post("@open fresh", who="bob", room="proj-b")
        self.assertIn("fresh", seats.deliver_any(
            session="s-open", seat="open"))

    def test_rehome_baselines_cursor_for_session_older_than_roster_cap(self):
        ra = self._repo("proj-a")
        seats.join(session="s-0", seat="many", cwd=ra)
        seats._init_cursor("proj-b", "many", "s-0", at_start=True)
        for i in range(1, seats.SESSIONS_KEPT + 2):
            seats.join(session="s-%d" % i, seat="many", cwd=ra)
        self.assertNotIn("s-0", seats.roster()["many"]["sessions"])
        chat.post("@many stale", who="bob", room="proj-b")
        self.assertTrue(seats.rehome_seat("many", "proj-b")[0])
        self.assertIsNone(seats.deliver_any(session="s-0", seat="many"))

    def test_returning_home_does_not_replay_traffic_while_away(self):
        ra, rb = self._repo("proj-a"), self._repo("proj-b")
        seats.join(session="s-return", seat="return", cwd=ra)
        self.assertTrue(seats.rehome_seat("return", "proj-b")[0])
        chat.post("@return stale-a", who="bob", room="proj-a")
        self.assertTrue(seats.rehome_seat("return", "proj-a")[0])
        self.assertIsNone(seats.deliver_any(session="s-return", seat="return"))
        chat.post("@return fresh-a", who="bob", room="proj-a")
        self.assertIn("fresh-a", seats.deliver_any(
            session="s-return", seat="return"))

    def test_explicit_rejoin_baselines_previously_left_room(self):
        ra, rb = self._repo("proj-a"), self._repo("proj-b")
        seats.join(session="s-exp-a", seat="pex", cwd=ra)
        seats.join(session="s-exp-b", seat="pex", cwd=rb, room="proj-b")
        chat.post("@pex stale-a", who="bob", room="proj-a")
        seats.join(session="s-exp-c", seat="pex", cwd=ra, room="proj-a")
        self.assertIsNone(seats.deliver_any(session="s-exp-a", seat="pex"))
        self.assertIsNone(seats.deliver_any(session="s-exp-b", seat="pex"))
        self.assertIsNone(seats.deliver_any(session="s-exp-c", seat="pex"))

    def test_registry_identity_disambiguates_same_basename_repos(self):
        a = self._repo(os.path.join("org-a", "cv"))
        b = self._repo(os.path.join("org-b", "cv"))
        projects = {"org-a-cv": {"path": a}, "emberian-cv": {"path": b}}
        with mock.patch("helm.registry.load", return_value={"projects": projects}):
            self.assertEqual(seats.derive_home_room(a), "org-a-cv")
            self.assertEqual(seats.derive_home_room(b), "emberian-cv")

    def test_registered_names_colliding_after_slug_are_fingerprinted(self):
        paths = [self._repo("registered-a"), self._repo("registered-b")]
        cases = [
            ("MV", "mv"),
            ("punct.name", "punct-name"),
            ("x" * 60 + "a", "x" * 60 + "b"),
        ]
        for left, right in cases:
            projects = {left: {"path": paths[0]}, right: {"path": paths[1]}}
            with mock.patch("helm.registry.load",
                            return_value={"projects": projects}):
                rooms = [seats.derive_home_room(path) for path in paths]
            self.assertNotEqual(*rooms)
            self.assertTrue(all(len(room) <= 60 for room in rooms))

    def test_unregistered_same_path_shape_is_fingerprinted_stably(self):
        import subprocess
        roots = [tempfile.mkdtemp(prefix="helm-unregistered-a-"),
                 tempfile.mkdtemp(prefix="helm-unregistered-b-")]
        for root in roots:
            self.addCleanup(shutil.rmtree, root, True)
        a = os.path.join(roots[0], "tree-a", "org", "cv")
        b = os.path.join(roots[1], "tree-b", "org", "cv")
        for repo in (a, b):
            os.makedirs(repo)
            subprocess.run(["git", "init", "-q", "-b", "main", repo],
                           check=True, capture_output=True)
        empty = mock.patch("helm.registry.load", return_value={"projects": {}})
        with empty:
            unknown = seats.derive_home_room(a), seats.derive_home_room(b)
        with mock.patch("helm.registry.load", return_value={"projects": {}}), \
                mock.patch("helm.automap.scan_repos",
                           return_value={a: "cv", b: "org-cv"}):
            scanner_known = seats.derive_home_room(a), seats.derive_home_room(b)
        self.assertNotEqual(*unknown)
        self.assertEqual(scanner_known, unknown)
        self.assertTrue(all(name.startswith("cv-") for name in unknown))

    def test_pathless_registry_entry_cannot_claim_current_repo(self):
        repo = self._repo("actual-repo")
        old = os.getcwd()
        try:
            os.chdir(repo)
            with mock.patch("helm.registry.load", return_value={
                    "projects": {"pathless-anchor": {"path": ""}}}):
                room = seats.derive_home_room(repo)
        finally:
            os.chdir(old)
        self.assertEqual(room, pk.slug(seats._path_project(repo)))
        self.assertNotEqual(room, "pathless-anchor")

    def test_long_unregistered_basename_preserves_fingerprint_suffix(self):
        import subprocess
        base = "r" * 70
        repos = [os.path.join(self.tmp, parent, base) for parent in ("a", "b")]
        for repo in repos:
            os.makedirs(repo)
            subprocess.run(["git", "init", "-q", "-b", "main", repo],
                           check=True, capture_output=True)
        with mock.patch("helm.registry.load", return_value={"projects": {}}):
            rooms = [seats.derive_home_room(repo) for repo in repos]
        self.assertNotEqual(*rooms)
        self.assertTrue(all(len(room) <= 60 for room in rooms))
        self.assertEqual(rooms,
                         [pk.slug(seats._path_project(repo)) for repo in repos])

    def test_project_name_normalizing_to_main_stays_unhomed(self):
        repo = self._repo("Main.")
        seats.join(session="s-normal-main", seat="pn", cwd=repo)
        self.assertIsNone(seats.roster()["pn"].get("home_room"))

    def test_submodule_uses_its_own_checkout_root(self):
        import subprocess
        source = self._repo("module-source")
        with open(os.path.join(source, "README"), "w") as f:
            f.write("module\n")
        subprocess.run(["git", "-C", source, "add", "README"], check=True)
        subprocess.run(["git", "-C", source, "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm",
                        "init"], check=True)
        parent = self._repo("parent")
        subprocess.run(["git", "-c", "protocol.file.allow=always", "-C", parent,
                        "submodule", "add", "-q", source, "modules/child"],
                       check=True, capture_output=True)
        child = os.path.join(parent, "modules", "child")
        self.assertEqual(seats._git_root(child), child)
        self.assertEqual(seats.derive_home_room(child),
                         pk.slug(seats._path_project(child)))

    def test_bare_repo_is_projectless(self):
        import subprocess
        bare = os.path.join(self.tmp, "bare.git")
        subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        self.assertIsNone(seats._git_root(bare))
        self.assertIsNone(seats.derive_home_room(bare))

    def test_worktrees_from_one_bare_common_repo_share_identity(self):
        import subprocess
        source = self._repo("source")
        with open(os.path.join(source, "README"), "w") as f:
            f.write("seed\n")
        subprocess.run(["git", "-C", source, "add", "README"], check=True)
        subprocess.run(["git", "-C", source, "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm",
                        "seed"], check=True)
        bare = os.path.join(self.tmp, "common.git")
        subprocess.run(["git", "clone", "--bare", "-q", source, bare], check=True)
        wa, wb = os.path.join(self.tmp, "wa"), os.path.join(self.tmp, "wb")
        subprocess.run(["git", "--git-dir", bare, "worktree", "add", "-q",
                        wa, "main"], check=True)
        subprocess.run(["git", "--git-dir", bare, "worktree", "add", "-q",
                        "-b", "lane", wb, "main"], check=True)
        self.assertEqual(seats._git_root(wa), bare)
        self.assertEqual(seats._git_root(wb), bare)
        self.assertEqual(seats.derive_home_room(wa),
                         seats.derive_home_room(wb))

    def test_roster_report_and_cli_show_active_home(self):
        repo = self._repo()
        seats.join(session="s-report", seat="report", cwd=repo)
        row = seats.roster_report()["seats"][0]
        self.assertEqual(row["home_room"], "proj-alpha")
        self.assertEqual(row["home_room_source"], "derived")
        rc, out, err = self.cmd("seats", ("--all",))
        self.assertEqual(rc, 0, err)
        self.assertIn("home #proj-alpha (derived)", out)

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

    def test_derive_seat_answers_the_ROSTER_before_minting_a_new_name(self):
        """auto_name MINTS a name for an un-rostered session -- its own
        docstring states the precondition: "callers reach here only when the
        roster has no row for this session". derive_seat called it for sessions
        that ALREADY had a seat, so it minted a fresh bare-family name and
        returned it in preference to the binding sitting right there.

        chat.whoname has always had the order right (HELM_CHAT_NAME ->
        seat_for_session -> auto_name); derive_seat skipped the middle rung.

        LIVE COST, measured 2026-08-02 on a seat that had been working all
        night: every dispatch it sent recorded sender='claude', a string naming
        no roster seat, so the ledger could not say who owed any of them -- and
        the delivery-confirmation whisper surfaced its rows to OTHER seats as
        obligations with unprovable owners.

        NOT CURED BY PASSING cwd. That was my first fix and it is wrong: from a
        LANE WORKTREE, auto_name(sid, safe_cwd()) yields '<lane-name>-claude',
        so a seat's identity would change per lane and lose the stability
        auto_name promises. The roster is the answer, not the path."""
        seats.join(session="sess-r1", seat="helm-wren", cwd="/tmp/p")
        self.assertEqual(seats.seat_for_session("sess-r1"), "helm-wren")
        self.assertEqual(seats.derive_seat("sess-r1"), "helm-wren")

    def test_an_UNROSTERED_session_still_reaches_auto_name(self):
        """A missing roster row must still reach the stable-name mint path."""
        seats.join(session="sess-bound", seat="helm-bound", cwd="/tmp/p")
        self.assertEqual(seats.seat_for_session("sess-bound"), "helm-bound")
        self.assertIsNone(seats.seat_for_session("sess-nobody"))
        self.assertEqual(seats.derive_seat("sess-nobody", "/tmp/proj"),
                         seats.auto_name("sess-nobody", "/tmp/proj"))

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
        """Claude Code exports
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
        for bad, why in (("b", "taken"), ("owner", "reserved"),
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
    """(roster-truth invariant): keep all live agents straight on
    the roster'). A live-but-idle agent must not vanish — presence stays fresh
    when it speaks, and even when its delivery is muted."""

    def test_post_refreshes_poster_presence(self):
        seats.join(session="s-rt1", seat="rt-agent", cwd="/tmp/p")
        os.remove(seats.seen_path("rt-agent"))          # prove post re-touches
        chat.post("hello fleet", who="rt-agent")
        self.assertTrue(os.path.exists(seats.seen_path("rt-agent")))

    def test_owner_post_mints_no_presence(self):
        chat.post("owner speaks", who="owner")          # owner is not a seat
        self.assertFalse(os.path.exists(seats.seen_path("owner")))
        self.assertNotIn("owner", seats.roster())

    def test_muted_deliver_still_refreshes_presence(self):
        seats.join(session="s-rt2", seat="rt-muted", cwd="/tmp/p")
        os.remove(seats.seen_path("rt-muted"))
        os.environ["HELM_CHAT_DELIVER"] = "0"            # delivery muted…
        self.assertIsNone(seats.deliver(session="s-rt2"))   # …so no nudge…
        self.assertTrue(os.path.exists(seats.seen_path("rt-muted")))  # …still alive


class StopGuardTest(SeatsBase):
    """The idle gate (the prior harness's arbiter, ported). Hermetic: room + claims in tmp,
    HELM_ADOPTED_DIR in tmp so the silent index-cap leg can never touch a live
    MEMORY.md."""

    def guard(self, payload=None, args=()):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        return self.cmd("stop-guard", ["--hook-json", *args], stdin=stdin or b"{}")

    def test_empty_reasons_cannot_refuse_a_stop(self):
        """bug-class empty-reason-refusal: no text means no refusal."""
        with mock.patch.object(seats, "stop_guard",
                               return_value=(["", " \t"], ["", "\t"])):
            rc, out, err = self.guard()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_real_reason_still_refuses_and_prints_its_text(self):
        with mock.patch.object(
                seats, "stop_guard",
                return_value=(["", "real blocker", " \t"], ["", "\t"])):
            rc, out, err = self.guard()
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "real blocker\n")

    def test_claim_evidence_warn_latches_and_refires_per_message(self):  # noqa: VACUOUS_ASSERTION — first stop proves the same warning is present before the intentional latch-absence assertion
        """Hook payload -> transcript -> WARN is live end to end. The same
        record latches; byte-identical text in a later record re-fires."""
        tp = os.path.join(self.tmp, "claim-turn.jsonl")

        def write(uuid):
            records = [
                {"message": {"role": "user", "content": "report"}},
                {"type": "attachment", "attachment": {}},
                {"uuid": uuid, "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "42 tests green"}]}},
            ]
            with open(tp, "a") as f:
                f.write("\n".join(json.dumps(r) for r in records) + "\n")

        os.environ["HELM_STOP_GUARD_WIRING"] = "0"
        os.environ["HELM_STOP_GUARD_WHISPER"] = "0"
        write("answer-1")
        payload = {"session_id": "s-claim", "transcript_path": tp}
        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertIn("CLAIMS WITHOUT THIS TURN'S EVIDENCE", err)

        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("CLAIMS WITHOUT THIS TURN'S EVIDENCE", err)

        write("answer-2")
        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertIn("CLAIMS WITHOUT THIS TURN'S EVIDENCE", err)

    def test_claim_evidence_warns_while_stop_is_already_continuing(self):
        """stop_hook_active suppresses blocks, not advisory truth. A non-string
        UUID uses the record-offset fallback rather than escaping the rung."""
        tp = os.path.join(self.tmp, "claim-active.jsonl")
        records = [
            {"message": {"role": "user", "content": "report"}},
            {"uuid": 123, "message": {"role": "assistant", "content": [
                {"type": "text", "text": "42 tests green"}]}},
        ]
        with open(tp, "w") as f:
            f.write("\n".join(json.dumps(r) for r in records))
        payload = {"session_id": "s-active", "transcript_path": tp,
                   "stop_hook_active": True}
        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertIn("CLAIMS WITHOUT THIS TURN'S EVIDENCE", err)

    def test_unreadable_claim_evidence_warns_skipped_not_clean(self):  # noqa: VACUOUS_ASSERTION — the same SKIPPED line is asserted present before the latch-absence check
        """Unreadable and clean are different operator states. The same broken
        transcript latches; appended broken input re-arms the warning."""
        tp = os.path.join(self.tmp, "claim-unreadable.jsonl")
        with open(tp, "w") as f:
            f.write(json.dumps({"message": {"content": "missing role"}}))
        payload = {"session_id": "s-skip", "transcript_path": tp}
        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertIn("CLAIM-EVIDENCE SKIPPED", err)
        # The independent inbox may still be clean; the claim-evidence state is
        # now separately visible instead of being inferred from that line.

        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("CLAIM-EVIDENCE SKIPPED", err)

        with open(tp, "a") as f:
            f.write("\n" + json.dumps({"message": {"content": "still broken"}}))
        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertIn("CLAIM-EVIDENCE SKIPPED", err)

    def test_credential_wall_suppresses_stop_inbox_without_consuming_it(self):
        seats.join(session="s-codex", seat="codex", cwd="/tmp/p")
        chat.post("@codex cannot act until healthy", who="owner")
        pk.write_json(proxywatch._state_path(), {"ts": time.time(),
                      "upstream": {"codex": {"state": "AUTH-401",
                                               "dark": True}}})
        cur = dict(seats._cursor("main", "codex", "s-codex"))
        blocks, warns = seats.stop_guard(
            session="s-codex", room="main", seat="codex")
        self.assertFalse(any("undelivered message(s)" in b for b in blocks),
                         blocks)
        self.assertFalse(any("undelivered message(s)" in w for w in warns),
                         warns)
        blocks, warns = seats.stop_guard(
            session="s-codex", room="main", seat="codex", stop_active=True)
        self.assertFalse(any("arrived DURING" in w for w in warns), warns)
        self.assertEqual(seats._cursor("main", "codex", "s-codex"), cur)
        self.assertTrue(seats._pending_all(
            "main", "codex", "s-codex", scan_lane="test"))

    def test_block_exit_skips_invisible_claim_evidence_work(self):  # noqa: VACUOUS_ASSERTION — the unconditional inbox block proves this is the block-exit arm before assert_not_called
        """A block exit discards WARNs, so it must not parse or latch one."""
        from helm import claimev
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice review the branch", who="bob")
        with mock.patch.object(claimev, "assessment") as assessment:
            rc, _out, err = self.guard(
                {"session_id": "s-block", "transcript_path": "/missing"},
                args=["--seat", "alice"])
        self.assertEqual(rc, 2)
        self.assertIn("undelivered", err)
        self.assertIn("review the branch", err)
        assessment.assert_not_called()

    def test_an_unwritable_inbox_latch_degrades_to_warn_not_block(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls bracket the loop: the MUST-HIT block before it and the healed-latch block after it both assert presence on the same observable; the loop's absence checks are the reviewed warn-not-block shape
        """The block's own closing promise — 'a re-stop on the same rows
        passes' — is the LATCH's promise. When the latch write failed, the
        old code swallowed the OSError and blocked anyway: an unconditional
        re-block on EVERY stop, forever, wearing a sentence that said the
        opposite. The beacon rung already states the law (no latch, no
        block); this proves the inbox rung obeys it: unwritable latch =>
        the rows surface as a WARN, never a block, and the block (with its
        now-true promise) returns once the latch heals."""
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice first obligation", who="bob")
        # MUST-HIT control: a writable latch blocks these very rows once
        blocks, _w = seats.stop_guard(session="s-lat", room="main",
                                      seat="alice")
        self.assertTrue([b for b in blocks
                         if "undelivered message(s)" in b], blocks)
        chat.post("@alice second obligation", who="bob")   # new fp re-arms
        with mock.patch.object(seats.pk, "atomic_write",
                               side_effect=OSError("read-only latch dir")):
            first = seats.stop_guard(session="s-lat", room="main",
                                     seat="alice")
            second = seats.stop_guard(session="s-lat", room="main",
                                      seat="alice")
        for blocks, warns in (first, second):    # BOTH stops: warn, no block
            self.assertEqual(
                [b for b in blocks if "undelivered message(s)" in b], [],
                blocks)
            hit = [w for w in warns if "undelivered message(s)" in w]
            self.assertEqual(len(hit), 1, warns)
            self.assertIn("NOT BLOCKING", hit[0])
            self.assertIn("latch is unwritable", hit[0])
            # the warn still teaches the runnable cure
            self.assertIn("helm chat catchup --including-mentions --apply",
                          hit[0])
            # and never wears the latch's promise it cannot keep
            self.assertNotIn("a re-stop on the same rows passes", hit[0])
        # latch healed: one honest block, whose promise then comes true
        blocks, _w = seats.stop_guard(session="s-lat", room="main",
                                      seat="alice")
        hit = [b for b in blocks if "undelivered message(s)" in b]
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("a re-stop on the same rows passes", hit[0])
        blocks, _w = seats.stop_guard(session="s-lat", room="main",
                                      seat="alice")
        self.assertEqual(
            [b for b in blocks if "undelivered message(s)" in b], [], blocks)

    def test_claim_evidence_failure_isolated_on_an_allow_exit(self):  # noqa: VACUOUS_ASSERTION — rc=0 is the unconditional allow control before checking the outer failure line is absent
        """An advisory failure allows the stop without failing the whole guard."""
        from helm import claimev
        with mock.patch.object(claimev, "assessment",
                               side_effect=RuntimeError("broken advisory")):
            rc, _out, err = self.guard(
                {"session_id": "s-fail", "transcript_path": "/missing"},
                args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("THE GUARD COULD NOT RUN", err)

    def test_a_repeating_watchdog_is_ONE_line_with_a_repeat_count(self):
        """One unresolved condition must not become N obligations on screen.

        A watchdog posts the SAME sentence every cycle and each repeat is a
        separate undelivered row, so the guard's list fills with one thing
        said many times. Measured on the live estate 2026-07-30: a seat
        reached 497 undelivered of which FOUR OF THE FIRST FIVE were a single
        idle-dispatch nag about one stranded row, re-posted every 15 minutes
        for 37 hours. At that size the guard inverts its own purpose — no seat
        can act on 497 items, the only move left is to park them all, and a
        real obligation sitting at #300 gets parked with the noise.

        The COUNT stays honest (they are still rows, still undelivered). What
        is SHOWN collapses to distinct sender+subject with a repeat tally,
        which is the fact that separates "one stuck thing" from "many things".
        """
        seats.join(seat="alice", cwd="/tmp/p")
        for _ in range(4):
            chat.post("@alice STRANDED — dispatch abc123 to @bob", who="watchdog")
        chat.post("@alice the gate at deadbeef needs your verdict", who="carol")
        rc, _out, err = self.guard({"session_id": "s-dedupe"},
                                   args=["--seat", "alice"])
        self.assertEqual(rc, 2)
        self.assertIn("5 undelivered", err, "the COUNT is still every row")
        self.assertEqual(err.count("STRANDED — dispatch abc123"), 1,
                         "four identical nags must render as ONE line")
        self.assertIn("(x4 repeats)", err, "and say how many times it repeated")
        self.assertIn("carol: @alice the gate at deadbeef", err,
                      "the DISTINCT obligation must not be crowded out — it was "
                      "the 5th row and would have been buried by repeats")

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

    def test_held_lease_blocks_once_per_set_then_compresses(self):  # noqa: VACUOUS_ASSERTION — rc 2 then the compressed-line assertIn are the unconditional positive controls; the two assertNotIns pin what must stay down
        """The claim-lease rung LATCHES like every other block — since
        2026-08-04, when the owner surfaced the old behaviour as the defect:
        the identical multi-line wall printed on two consecutive stops of an
        UNCHANGED held set. (This test previously pinned that behaviour by
        name, test_held_lease_reblocks_every_stop_it_is_NOT_latched, and the
        docs it bound have been updated in the same commit — the doc sentence
        stays bound to behaviour, in its new polarity.)

        One full block per held-set fingerprint (resource + lease id + TTL
        band); the re-stop compresses to a one-line WARN that still carries
        the count; the release empties the arm entirely. The detailed spec's
        'positive proof only, re-verified every stop' stays true of the
        CLASSIFICATION — the latch governs emission only.
        tests/test_stop_lease_latch.py owns the full latch contract
        (changed-set re-print, TTL escalation, unwritable-latch degrade)."""
        ok, _m, lease = seats.claim("port:9931", "alice", ttl=600,
                                    session="s-nl")
        self.assertTrue(ok)
        rc, _o, err = self.guard({"session_id": "s-nl"})
        self.assertEqual(rc, 2)
        self.assertIn("port:9931", err)
        # SAME state, second stop: compresses to one warn line, stop passes
        rc, _o, err = self.guard({"session_id": "s-nl"})
        self.assertEqual(rc, 0, err)
        self.assertIn("unchanged since the last warning", err)
        self.assertNotIn("run the EXACT command", err)   # the wall stayed down
        # the cure is still the release — afterwards the arm says nothing
        ok, _m = seats.release("port:9931", "alice", lease=lease,
                               session="s-nl")
        self.assertTrue(ok)
        rc, _o, err = self.guard({"session_id": "s-nl"})
        self.assertEqual(rc, 0, err)
        self.assertNotIn("lease(s) held", err)

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

    def test_an_UNPROVEN_lane_lease_is_held_but_release_is_NOT_prescribed(self):
        """#66. A lease on a lane worktree cannot be proven idle, and the guard
        was prescribing release as though it had been.

        MEASURED on the integrator's live work-peek lease, 2026-08-01:
        _delegated_build returns None and BOTH its proofs are unreachable for
        that delegation shape — 0 live processes have cwd inside the claimed
        room (a subagent inherits its parent's cwd; it does not chdir into the
        lane) and the documented-subagent activity file was never written.
        UNKNOWN, not idle.

        The BLOCK is unchanged: widening an exemption on absent evidence is
        the fleet-wide un-guarding the delegation contract forbids by name.
        What changes is that a guard may not prescribe a DESTRUCTIVE action on
        a premise it could not establish.

        #112 REPLACED THE PREMISE ITSELF. The caveat used to ask about
        DELEGATION, which is both unanswerable and — measured over four live
        holds — the right basis 1 time in 4. It now reports the four reads of
        UNFINISHED WORK BOUND TO THE ROOM. This fixture's lease resolves to
        no lane room at all (planted from the test's own cwd), so the honest
        answer is FOUR missed reads and a withheld command — a strictly
        stronger version of the same refusal to prescribe.

        THE ALL-WITHHELD END of the sermon's range. The header used to
        promise "run the EXACT command shown" unconditionally — instructing
        a seat to run something no line contained. It no longer asserts
        anything about the lines at all (see the MIXED arm for why a
        per-stop promise was the wrong repair), so what is pinned here is
        that the single LINE withholds and explains itself."""
        seats.claim("worktree:helm:some-lane", "alice", ttl=60, session="s-66")
        blocks, _w = seats.stop_guard(session="s-66", seat="alice")
        self.assertTrue(blocks, "an unproven lane lease must still hold")
        text = " ".join(blocks)
        self.assertIn("4 of 4 reads could not be made", text)
        self.assertIn("an IDLE room is UNPROVEN", text)
        self.assertIn("NO release command is offered", text)
        self.assertIn("EACH LANE BELOW CARRIES ITS OWN INSTRUCTION", text)
        self.assertNotIn("run the EXACT command shown", text)
        self.assertNotIn("helm work release", text)
        self.assertNotIn("delegation is UNKNOWN, not disproven", text)

    def test_a_NON_lane_lease_keeps_its_plain_release_advice(self):
        """The control, and the reason this is scoped rather than blanket: a
        lease that names no worktree has no subagent to strand, so its advice
        stays exactly as it was. A caveat on every lease is a caveat nobody
        reads.

        landlock, not gatelock, on purpose: a fresh gatelock lease sits
        above the renewer floor and takes the renewed arm (stop allowed),
        so it was never this control's subject — with it, the old
        assertions passed on the RENEWED hint, which contains no release
        advice at all. The tightened assertIn is the positive control the
        old shape lacked."""
        seats.claim("landlock:helm", "alice", ttl=60, session="s-66b")
        blocks, _w = seats.stop_guard(session="s-66b", seat="alice")
        self.assertTrue(blocks)
        joined = " ".join(blocks)
        self.assertIn("helm chat release landlock:helm", joined)
        self.assertNotIn("mid-build", joined)

    def test_a_row_arriving_MID_TURN_is_still_SURFACED_never_blocked(self):
        """#74. stop_hook_active must stop the guard BLOCKING, not stop it
        LOOKING — and the old short-circuit did both.

        MEASURED COST: @kimi's verdict reached @helm-claude-2 mid-turn at
        18:05 on 2026-08-01; that turn ended at 18:11 with stop_hook_active
        set, so the guard returned before reading the inbox. The row was never
        shown and never latched, and the fleet idled 5.5 hours waiting on the
        land that verdict authorized.

        Blocking under stop_hook_active is the infinite-loop shape. A WARN is
        not: it never re-enters the stop path."""
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice a verdict that landed mid-turn", who="bob")
        blocks, warns = seats.stop_guard(session=None, seat="alice",
                                         stop_active=True)
        self.assertEqual(blocks, [], "stop_hook_active must never re-block")
        self.assertTrue(warns, "the mid-turn row was not surfaced at all — "
                        "this is the 5.5h hole")
        self.assertIn("arrived DURING this turn", " ".join(warns))

    def test_stop_active_with_an_EMPTY_inbox_stays_silent(self):
        """The control for the arm above: the new read must not manufacture a
        warn on every continued turn, or the signal is noise within a day."""
        seats.join(seat="quiet", cwd="/tmp/p")
        blocks, warns = seats.stop_guard(session=None, seat="quiet",
                                         stop_active=True)
        self.assertEqual((blocks, warns), ([], []))
        # Unconditional control on the SAME observable: the very same call
        # DOES speak once a row exists, so the silence above is a decision
        # rather than a rung that never fires.
        chat.post("@quiet now there is one", who="bob")
        _b, w2 = seats.stop_guard(session=None, seat="quiet", stop_active=True)
        self.assertTrue(w2)

    def test_stop_active_honors_the_inbox_kill_switch(self):
        """A disabled inbox check performs no room scan on either Stop path."""
        os.environ["HELM_STOP_GUARD_INBOX"] = "0"
        with mock.patch.object(seats, "_pending_all",
                               side_effect=AssertionError("scanned inbox")):
            self.assertEqual(seats.stop_guard(
                session=None, seat="quiet", stop_active=True), ([], []))

    def test_garbage_stdin_fails_open(self):
        rc, _o, _e = self.guard(b"not json{{")
        self.assertEqual(rc, 0)


class StopGuardDelegationTest(SeatsBase):
    """Block (b)'s delegation exemption (board row stop-guard-delegation-
    exemption) — the verifiable-saturated-state carve-out. REAL processes in
    a REAL scratch repo's lane room, read through the REAL /proc (HELM_PROC
    repointed off SeatsBase's empty fixture on purpose): the exemption may
    fire on positive proof ONLY, and every UNKNOWN must block exactly like
    the un-exempted rung. Session ids are minted random so the live-host
    needle scan can never collide with an unrelated process."""

    def setUp(self):
        super().setUp()
        # a scratch project: the SAME pure functions `helm work` mints leases
        # with (resource/lane_path) resolve the planted lease back to this
        # room — the guard anchors the project from its cwd, so chdir in
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        r = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root,
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        subprocess.run(["git", "-C", self.root, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "tip"], check=True, capture_output=True, timeout=30)
        self.wt = os.path.join(self.root + "-wt", "lane-x")
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-x", self.wt], check=True,
                       capture_output=True, timeout=30)
        self.res = "worktree:proj:lane-x"
        self.sid = "s-deleg-" + os.urandom(6).hex()
        self._cwd = os.getcwd()
        os.chdir(self.root)
        os.environ["HELM_PROC"] = "/proc"   # the REAL table — proof must be real
        self.kids = []

    def tearDown(self):
        os.chdir(self._cwd)
        for p in self.kids:
            p.kill()
            p.wait(timeout=10)
        super().tearDown()

    def guard(self, payload=None, args=()):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        return self.cmd("stop-guard", ["--hook-json", *args], stdin=stdin or b"{}")

    def spawn(self, cwd, mark=None, argv=("sleep", "300")):
        """A REAL delegate: env carries the session id exactly the way a
        Bash-tool child's CLAUDE_CODE_SESSION_ID does."""
        env = {"PATH": "/usr/bin:/bin"}
        if mark:
            env["CLAUDE_CODE_SESSION_ID"] = mark
        p = subprocess.Popen(list(argv), cwd=cwd, env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.kids.append(p)
        return p

    def settle(self, pid, cwd, mark=None):
        """Wait out the fork→exec window: pre-exec, /proc shows the PARENT's
        environ — the child is proof only once its own cwd link AND (when
        marked) its own environ read back."""
        want = os.path.realpath(cwd)
        for _ in range(200):
            try:
                ok = os.path.realpath("/proc/%d/cwd" % pid) == want
                if ok and mark:
                    with open("/proc/%d/environ" % pid, "rb") as f:
                        ok = mark.encode() in f.read()
                if ok:
                    return
            except OSError:
                pass
            time.sleep(0.025)
        self.fail("child %d never settled into %s" % (pid, cwd))

    def test_live_delegated_child_exempts_stating_its_proof(self):
        ok, _m, _l = seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.assertTrue(ok)
        p = self.spawn(self.wt, mark=self.sid)
        self.settle(p.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("live delegated build", err)   # SAYS what it verified
        self.assertIn("pid %d" % p.pid, err)
        self.assertIn(self.wt, err)
        self.assertIn(self.res, err)
        self.assertIn("lease retained", err)

    def test_child_in_a_different_directory_never_exempts(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        p = self.spawn(elsewhere, mark=self.sid)     # OUR child, WRONG room
        self.settle(p.pid, elsewhere, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)                      # the poach-shield holds
        self.assertIn(self.res, err)
        self.assertIn("release", err)
        self.assertNotIn("delegated", err)

    def test_foreign_process_in_the_room_never_exempts(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        p = self.spawn(self.wt)                      # right room, NOT our tree
        self.settle(p.pid, self.wt)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn("release", err)

    def test_dead_child_re_blocks_the_very_next_stop(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        p = self.spawn(self.wt, mark=self.sid)
        self.settle(p.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        p.kill()
        p.wait(timeout=10)                           # the SA finished/died
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)                      # per-stop proof, no cache
        self.assertIn("release", err)

    def test_unreadable_proc_table_blocks(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        p = self.spawn(self.wt, mark=self.sid)       # a live delegate EXISTS —
        self.settle(p.pid, self.wt, mark=self.sid)
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "no-such-proc")
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)                      # — but unprovable blocks
        self.assertIn("release", err)

    def test_ppid_chain_reaches_the_marked_ancestor(self):
        # the codex-SA shape: the delegate's own env is scrubbed (env -i),
        # proof arrives via the ppid walk to its live MARKED parent shell
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        pidfile = os.path.join(self.tmp, "kid.pid")
        self.spawn(self.root, mark=self.sid, argv=(
            "bash", "-c",
            "cd '%s' && env -i sleep 300 & echo $! > '%s'; wait"
            % (self.wt, pidfile)))
        for _ in range(200):
            try:
                with open(pidfile) as f:
                    kid = int(f.read().strip())
                break
            except (OSError, ValueError):
                time.sleep(0.025)
        else:
            self.fail("delegate never wrote its pidfile")
        self.addCleanup(self._reap, kid)
        self.settle(kid, self.wt)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("live delegated build", err)
        self.assertIn("pid %d" % kid, err)

    def _reap(self, pid):
        try:
            os.kill(pid, 9)
        except (OSError, ProcessLookupError):
            pass

    def test_interval_sampling_allows_subagent_between_tool_calls(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        sub = self.spawn(self.root, mark=self.sid, argv=(
            "python3", "-c",
            "import os, time; os.chdir('%s'); open('%s/ready', 'w').close(); time.sleep(0.5); os.chdir('%s'); time.sleep(300)"
            % (self.wt, self.tmp, self.tmp)))
        self.settle(sub.pid, self.wt, mark=self.sid)
        # First stop-guard call records process activity in self.wt
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("live delegated build", err)

        # Wait for sub's cwd to move away from self.wt (simulating between-tool-calls state)
        for _ in range(100):
            try:
                if os.path.realpath("/proc/%d/cwd" % sub.pid) != os.path.realpath(self.wt):
                    break
            except OSError:
                pass
            time.sleep(0.025)

        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("recent activity", err)

    def test_posttool_subagent_activity_between_stops_exempts(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))

        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2, err)

        payload = json.dumps({
            "hook_event_name": "PostToolUse",
            "session_id": self.sid,
            "cwd": self.wt,
            "tool_name": "Read",
            "agent_id": "agent-regression",
            "agent_type": "Explore",
        }).encode()
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id, create=True):
            rc, _out = self.cmd_fd("deliver", ["--hook-json"], stdin=payload)
            self.assertEqual(rc, 0)
            rc, _o, err = self.guard({"session_id": self.sid})

        self.assertEqual(rc, 0, err)
        self.assertIn("recent subagent activity", err)
        self.assertIn("agent-regression", err)
        self.assertIn(self.res, err)
        self.assertIn("lease retained", err)

    def test_subagent_stop_removes_interval_exemption(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        event = {
            "session_id": self.sid, "cwd": self.wt,
            "agent_id": "agent-complete", "agent_type": "Explore",
        }
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            body = dict(event, hook_event_name="PostToolUse", tool_name="Read")
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(body).encode())
            rc, _o, err = self.guard({"session_id": self.sid})
            self.assertEqual(rc, 0, err)
            body = dict(event, hook_event_name="SubagentStop")
            rc, _out = self.cmd_fd("delegation-stop", ["--hook-json"],
                                   stdin=json.dumps(body).encode())
            self.assertEqual(rc, 0)
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2, err)
        self.assertIn("release", err)

    def test_subagent_stop_tombstone_survives_claim_lock_contention(self):
        import fcntl
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        event = {"session_id": self.sid, "cwd": self.wt,
                 "agent_id": "agent-contended"}
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            body = dict(event, hook_event_name="PostToolUse", tool_name="Read")
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(body).encode())
        with open(seats.claims_path() + ".lock", "a") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            body = dict(event, hook_event_name="SubagentStop")
            rc, _out = self.cmd_fd("delegation-stop", ["--hook-json"],
                                   stdin=json.dumps(body).encode())
            self.assertEqual(rc, 0)
        self.assertTrue(seats._agent_stopped(self.sid, "agent-contended"))
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2, err)

    def test_posttool_requires_agent_and_exact_claimed_lane(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        nested = os.path.join(self.wt, "nested")
        os.makedirs(nested)
        base = {
            "hook_event_name": "PostToolUse",
            "session_id": self.sid,
            "cwd": self.wt,
            "tool_name": "Read",
            "agent_id": "agent-regression",
            "agent_type": "Explore",
        }
        cases = {
            "main-thread": {k: v for k, v in base.items()
                            if k not in ("agent_id", "agent_type")},
            "blank-agent": dict(base, agent_id=""),
            "agent-type-only": {k: v for k, v in base.items() if k != "agent_id"},
            "wrong-event": dict(base, hook_event_name="PreToolUse"),
            "project-root": dict(base, cwd=self.root),
            "nested-lane": dict(base, cwd=nested),
            "wrong-session": dict(base, session_id="s-other"),
        }
        for name, body in cases.items():
            with self.subTest(name=name), \
                    mock.patch.object(seats, "_enclosing_claude_holder",
                                      return_value=holder_id):
                rc, _out = self.cmd_fd(
                    "deliver", ["--hook-json"],
                    stdin=json.dumps(body).encode())
                self.assertEqual(rc, 0)
                self.assertFalse(os.path.exists(
                    seats._delegation_activity_path(self.sid, self.res)))

        os.environ["HELM_CHAT_NAME"] = "bob"
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(base).encode())
        self.assertFalse(os.path.exists(
            seats._delegation_activity_path(self.sid, self.res)))

        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_DELEGATION"] = "0"
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(base).encode())
        self.assertFalse(os.path.exists(
            seats._delegation_activity_path(self.sid, self.res)))
        os.environ.pop("HELM_STOP_GUARD_DELEGATION")

        class NoLock:
            f = None

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id), \
                mock.patch.object(seats, "_flocked", return_value=NoLock()):
            self.assertFalse(seats._record_posttool_delegation(base))
        self.assertFalse(os.path.exists(
            seats._delegation_activity_path(self.sid, self.res)))

    def test_activity_lock_contention_never_holds_hook_paths(self):  # noqa: VACUOUS_ASSERTION — exact started/completed dictionaries prove both workers reached the barrier and returned while the lock stayed held; forcing blocking acquisition makes completed false
        import fcntl
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        # This test times the CLAIMS-lock boundary, not every independent Stop
        # rung. A lane adding one module legitimately activates wiring's AST
        # census and made the 200ms assertion fail while neither worker held
        # the claims lock — a false verdict about the path this test names.
        os.environ["HELM_STOP_GUARD_WIRING"] = "0"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        event = {"hook_event_name": "PostToolUse", "session_id": self.sid,
                 "cwd": self.wt, "tool_name": "Read",
                 "agent_id": "agent-lock"}
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.assertTrue(seats._record_posttool_delegation(event))
            results, failures = {}, {}
            start = threading.Event()
            ready = {name: threading.Event() for name in ("produce", "consume")}
            done = {name: threading.Event() for name in ready}

            def run(name, fn):
                ready[name].set()
                start.wait()
                try:
                    results[name] = fn()
                except Exception as e:
                    failures[name] = e
                finally:
                    done[name].set()

            workers = [
                threading.Thread(
                    target=run, args=("produce", lambda:
                        seats._record_posttool_delegation(
                            dict(event, agent_id="agent-contender")))),
                threading.Thread(
                    target=run, args=("consume", lambda:
                        seats.stop_guard(session=self.sid, seat="alice"))),
            ]
            with open(seats.claims_path() + ".lock", "a") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX)
                for worker in workers:
                    worker.start()
                started = {name: event.wait(5) for name, event in ready.items()}
                start.set()
                completed = {name: event.wait(5) for name, event in done.items()}
            for worker in workers:
                worker.join(2)
        self.assertEqual(started, {"produce": True, "consume": True},
                         "a worker never reached the start barrier")
        self.assertEqual(completed, {"produce": True, "consume": True},
                         "a hook path waited on the held claims lock")
        self.assertEqual(failures, {})
        self.assertFalse(results["produce"])
        self.assertTrue(results["consume"][0])

    def test_same_basename_foreign_clone_activity_never_exempts(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        other = os.path.join(self.tmp, "other", "proj")
        os.makedirs(other)
        subprocess.run(["git", "init", "-q", "-b", "main", other],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "-C", other, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "tip"], check=True, capture_output=True, timeout=30)
        other_wt = other + "-wt/lane-x"
        subprocess.run(["git", "-C", other, "worktree", "add", "-q",
                        "-b", "lane-x", other_wt], check=True,
                       capture_output=True, timeout=30)
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        base = {"hook_event_name": "PostToolUse", "session_id": self.sid,
                "tool_name": "Read"}
        foreign = dict(base, cwd=other_wt, agent_id="agent-foreign")
        current = dict(base, cwd=self.wt, agent_id="agent-current")
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(foreign).encode())
            rc, _o, err = self.guard({"session_id": self.sid})
            self.assertEqual(rc, 2, err)

            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(current).encode())
            rc, _o, err = self.guard({"session_id": self.sid})
            self.assertEqual(rc, 0, err)
            self.assertIn("agent-current", err)

            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(foreign).encode())
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("agent-current", err)

    def test_posttool_requires_same_holder_incarnation_and_window(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        payload = json.dumps({
            "hook_event_name": "PostToolUse", "session_id": self.sid,
            "cwd": self.wt, "tool_name": "Read",
            "agent_id": "agent-window",
        }).encode()
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"], stdin=payload)
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=(holder_id[0], holder_id[1] + 1)):
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2, err)

        # The block above LATCHED this held set (the 2026-08-04 same-state
        # latch), and the next probe examines CLASSIFICATION — an expired
        # window must not exempt — not emission. Reset the emission memory so
        # the probe can read the classification directly: with the latch
        # cleared, a wrongly-exempting guard exits 0 saying 'live delegated
        # build' and a correctly-blocking one exits 2.
        import glob as _glob
        for p in _glob.glob(os.path.join(
                chat.chat_dir(), "*.%s.*" % seats.LEASE_LATCH)):
            os.unlink(p)

        path = seats._delegation_activity_path(self.sid, self.res)
        data = seats.pk.read_json(path, {})
        key = seats._activity_record_key("post_tool_use", "agent-window")
        data["records"][key]["mono"] = seats._now_mono() - 301
        seats.pk.write_json(path, data)
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2, err)

    def test_posttool_and_live_scan_sources_do_not_clobber(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        child = self.spawn(self.wt, mark=self.sid)
        self.settle(child.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        event = {
            "hook_event_name": "PostToolUse", "session_id": self.sid,
            "cwd": self.wt, "tool_name": "Read",
        }
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            for agent in ("agent-a", "agent-b"):
                self.cmd_fd("deliver", ["--hook-json"],
                            stdin=json.dumps(dict(event, agent_id=agent)).encode())
        records = seats._get_delegation_activity(self.sid, self.res)
        self.assertEqual(
            sorted(r["source"] for r in records.values()),
            ["live_scan", "post_tool_use", "post_tool_use"])

        stop = {"hook_event_name": "SubagentStop", "session_id": self.sid,
                "cwd": self.wt, "agent_id": "agent-b"}
        self.cmd_fd("delegation-stop", ["--hook-json"],
                    stdin=json.dumps(stop).encode())
        records = seats._get_delegation_activity(self.sid, self.res)
        self.assertEqual({r.get("agent") for r in records.values()},
                         {None, "agent-a"})

    def test_rebind_and_rollback_purge_posttool_activity(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        path_a = seats._delegation_activity_path(self.sid, self.res)
        payload = {
            "hook_event_name": "PostToolUse", "session_id": self.sid,
            "cwd": self.wt, "tool_name": "Read",
            "agent_id": "agent-rebind",
        }
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(payload).encode())
        self.assertTrue(os.path.exists(path_a))

        sid_b = self.sid + "-new"
        self.assertEqual(seats.rebind_claim_sessions("alice", self.sid, sid_b),
                         [self.res])
        self.assertFalse(os.path.exists(path_a))
        payload["session_id"] = sid_b
        path_b = seats._delegation_activity_path(sid_b, self.res)
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(payload).encode())
        self.assertTrue(os.path.exists(path_b))
        self.assertEqual(seats.rollback_claim_sessions(
            "alice", self.sid, sid_b, [self.res]), 1)
        self.assertFalse(os.path.exists(path_b))

    def test_interval_sampling_refuses_recycled_or_expired_pid(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        sub = self.spawn(self.root, mark=self.sid, argv=(
            "python3", "-c",
            "import os, time; os.chdir('%s'); open('%s/ready', 'w').close(); time.sleep(0.5)"
            % (self.wt, self.tmp)))
        self.settle(sub.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        # Wait for sub process to exit completely
        sub.wait()
        for _ in range(50):
            if not seats._is_pid_alive(sub.pid):
                break
            time.sleep(0.02)

        # Exited PID no longer passes delegation check, fail-closed BLOCK
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn("release", err)

    def test_interval_sampling_refuses_missing_or_mismatched_starttime(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        sub = self.spawn(self.wt, mark=self.sid)
        self.settle(sub.pid, self.wt, mark=self.sid)

        real_st = seats._get_pid_starttime(sub.pid)
        self.assertIsInstance(real_st, int)

        # 1. Missing starttime with require_starttime=True -> False
        self.assertFalse(seats._is_pid_alive(sub.pid, starttime=None, require_starttime=True))

        # 2. Non-integer starttime -> False
        self.assertFalse(seats._is_pid_alive(sub.pid, starttime="invalid", require_starttime=True))

        # 3. Mismatched recycled starttime -> False
        self.assertFalse(seats._is_pid_alive(sub.pid, starttime=real_st + 9999, require_starttime=True))

        # 4. Correct starttime -> True
        self.assertTrue(seats._is_pid_alive(sub.pid, starttime=real_st, require_starttime=True))

    def test_release_and_lease_nonce_clean_up_delegation_activity(self):
        ok, _msg, lease_a = seats.claim(self.res, "alice", ttl=120, session=self.sid)
        self.assertTrue(ok)
        self.assertIsNotNone(lease_a)

        sub = self.spawn(self.wt, mark=self.sid)
        self.settle(sub.pid, self.wt, mark=self.sid)

        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        act_file = seats._delegation_activity_path(self.sid, self.res)
        self.assertTrue(os.path.exists(act_file))

        act = seats._get_delegation_activity(self.sid, self.res)
        self.assertIsNotNone(act)
        self.assertEqual(act["live_scan"].get("lease"), lease_a)

        # Release lease -> unlinks delegation activity record physically
        own = seats.own_leases("alice")
        token = own.get(self.res)
        ok_rel, _m = seats.release(self.res, "alice", lease=token, session=self.sid)
        self.assertTrue(ok_rel)

        self.assertFalse(os.path.exists(act_file))
        act_after = seats._get_delegation_activity(self.sid, self.res)
        self.assertIsNone(act_after)

    def test_reacquire_lease_nonce_mismatch_unlinks_stale_activity(self):
        ok, _m, lease_a = seats.claim(self.res, "alice", ttl=120, session=self.sid)
        self.assertTrue(ok)

        sub = self.spawn(self.wt, mark=self.sid)
        self.settle(sub.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        act_file = seats._delegation_activity_path(self.sid, self.res)
        self.assertTrue(os.path.exists(act_file))

        # Stale file on disk carries lease_a
        act_raw = seats.pk.read_json(act_file, {})
        self.assertEqual(act_raw["records"]["live_scan"].get("lease"), lease_a)

        # Simulate claim lease being re-granted / rotated to lease_b in claims.json (ABA scenario)
        lease_b = os.urandom(8).hex()
        c = seats.pk.read_json(seats.claims_path(), {})
        c[self.res]["lease"] = lease_b
        seats.pk.write_json(seats.claims_path(), c)

        # _get_delegation_activity detects lease_a != lease_b mismatch, returns None, and unlinks file
        act = seats._get_delegation_activity(self.sid, self.res)
        self.assertIsNone(act)
        self.assertFalse(os.path.exists(act_file))

    def test_corrupt_json_or_corrupt_lease_fails_closed_and_unlinks(self):
        ok, _m, lease_a = seats.claim(self.res, "alice", ttl=120, session=self.sid)
        self.assertTrue(ok)

        act_file = seats._delegation_activity_path(self.sid, self.res)
        with open(act_file, "w") as f:
            f.write("{corrupt json[")

        self.assertTrue(os.path.exists(act_file))
        act = seats._get_delegation_activity(self.sid, self.res)
        self.assertIsNone(act)
        self.assertFalse(os.path.exists(act_file))

        # Corrupt lease "None" string in claim
        c = seats.pk.read_json(seats.claims_path(), {})
        c[self.res]["lease"] = "None"
        seats.pk.write_json(seats.claims_path(), c)

        self.assertIsNone(seats._get_claim_lease(self.res))

    def test_resource_sha256_hash_prevents_slug_collisions(self):
        seats.chat._ensure_dir()
        res_dot = "worktree:p:lane.a"
        res_dash = "worktree:p:lane-a"

        path_dot = seats._delegation_activity_path(self.sid, res_dot)
        path_dash = seats._delegation_activity_path(self.sid, res_dash)

        self.assertNotEqual(path_dot, path_dash)

        with open(path_dot, "w") as f:
            f.write("{}")
        with open(path_dash, "w") as f:
            f.write("{}")

        seats._unlink_delegation_activity(res_dot)
        self.assertFalse(os.path.exists(path_dot))
        self.assertTrue(os.path.exists(path_dash))

        seats._unlink_delegation_activity(res_dash)
        self.assertFalse(os.path.exists(path_dash))

    def test_kill_switch_restores_the_strict_block(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        p = self.spawn(self.wt, mark=self.sid)
        self.settle(p.pid, self.wt, mark=self.sid)
        os.environ["HELM_STOP_GUARD_DELEGATION"] = "0"
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn("release", err)
        self.assertNotIn("delegated", err)

    def test_non_lane_leases_never_consult_proc_and_block(self):
        seats.claim("worktree-main", "alice", ttl=60, session=self.sid)
        seats.claim("worktree:other:lane-x", "alice", ttl=60, session=self.sid)
        p = self.spawn(self.wt, mark=self.sid)       # live delegate, but no
        self.settle(p.pid, self.wt, mark=self.sid)   # lease NAMES its room
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn("worktree-main", err)          # not lane-shaped
        self.assertIn("worktree:other:lane-x", err)  # foreign project

    def test_mixed_leases_block_on_the_bare_one_without_advisory_noise(self):
        """Was ..._and_state_the_proof, which pinned the delegated-lease
        advisory INSIDE the exit-2 emission. Claude Code renders every
        exit-2 stop hook as a red "Stop hook error:", so that pin kept
        correct-behavior reassurance glowing red beside the one real
        demand (the owner watched it all evening, 2026-07-31). The
        advisory now rides the ALLOW exit only; the block carries just
        the items that demand action."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        seats.claim("db-migration", "alice", ttl=60, session=self.sid)
        p = self.spawn(self.wt, mark=self.sid)
        self.settle(p.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn("db-migration", err)           # the bare lease blocks
        self.assertNotIn(self.res + " (", err)       # delegated one is NOT in
        self.assertNotIn("live delegated build", err)  # the held list, and its
        # advisory stays OFF the exit-2 emission (not a demand; it returns
        # on the allow exit once the cure lands)


class DelegationHolderAncestryTest(SeatsBase):
    def setUp(self):
        super().setUp()
        self.proc = os.path.join(self.tmp, "holder-proc")
        os.makedirs(self.proc)

    def plant(self, pid, ppid, starttime, comm, stat_comm=None):
        pdir = os.path.join(self.proc, str(pid))
        os.makedirs(pdir)
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(starttime), "0"]
        with open(os.path.join(pdir, "stat"), "w") as f:
            f.write("%d (%s) %s" %
                    (pid, stat_comm or comm, " ".join(fields)))
        with open(os.path.join(pdir, "comm"), "w") as f:
            f.write(comm + "\n")

    def test_walks_wrappers_and_parses_process_controlled_comm(self):
        self.plant(100, 90, 1000, "python3", stat_comm="odd ) worker")
        self.plant(90, 80, 900, "timeout")
        self.plant(80, 70, 800, "sh")
        self.plant(70, 1, 700, "claude")
        self.assertEqual(seats._proc_stat_link(100, self.proc), (1000, 90))
        self.assertEqual(seats._enclosing_claude_holder(
            start_pid=100, proc_dir=self.proc), (70, 700))

    def test_nonblocking_evidence_lock_returns_unknown_immediately(self):
        import fcntl
        path = os.path.join(self.tmp, "claims.lock")
        with open(path, "a") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            started = time.monotonic()
            with seats._flocked(path, blocking=False) as got:
                self.assertIsNone(got.f)
            self.assertLess(time.monotonic() - started, 0.1)

    def test_recheck_rejects_reused_intermediate_generation(self):
        self.plant(100, 90, 1000, "python3")
        self.plant(90, 70, 900, "timeout")
        self.plant(70, 1, 700, "claude")
        real = seats._proc_stat_link
        seen = {}

        def changed(pid, proc_dir="/proc"):
            link = real(pid, proc_dir=proc_dir)
            seen[pid] = seen.get(pid, 0) + 1
            if pid == 90 and seen[pid] > 1:
                return link[0] + 1, link[1]
            return link

        with mock.patch.object(seats, "_proc_stat_link", side_effect=changed):
            self.assertIsNone(seats._enclosing_claude_holder(
                start_pid=100, proc_dir=self.proc))


class StopGuardGatePendingTest(SeatsBase):
    """STAGES 2+3 of the claims-block exemption: LEASE-HELD-WHILE-GATE-
    PENDING (measured live 2026-07-29 — three seats force-continued all
    evening on gate-pending leases; release opens the lane mid-gate,
    finishing is the reviewer's move) and LEASE-HELD-AWAITING-LAND
    (measured live 2026-07-31, bug class verb-designed-noun-lifecycle-
    holed: lane lr-land-ack-deletions-reachable sat APPROVED with a
    VERIFIED gate and every stop still exited 2 — the exemption died the
    stage after it fired). The proof is TWO-PART — lane correspondence
    (stem-family, premise lane-names-are-a-family-probe-the-stem) AND a
    ref↔HEAD match — against a kind=review dispatch row that is OPEN, or
    VERDICT polarity=approve with a BOUND gate token. Neither half alone:
    lane name without the ref is a naming coincidence, ref without the
    lane is a branching coincidence (measured live 2026-07-31: a fresh
    lane branched at an under-review tip inherited ANOTHER lane's gate).
    UNKNOWN → BLOCK at every rung, recomputed per stop; fix/supersede/
    ungated verdicts re-block."""

    def setUp(self):
        super().setUp()
        # scratch project + lane room, anchored from the project root —
        # the SAME pure functions helm work mints leases with
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        r = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root,
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        # one repo, one real git worktree: the dispatch ref must be a real
        # commit (the writer validates), and the room's HEAD must equal it
        subprocess.run(["git", "-C", self.root, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "tip"], check=True, capture_output=True,
                       timeout=30)
        self.head = subprocess.run(
            ["git", "-C", self.root, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
            timeout=30).stdout.strip()
        self.wt = self.root + "-wt/lane-g"
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-g", self.wt],
                       check=True, capture_output=True, timeout=30)
        self.res = "worktree:proj:lane-g"
        self.sid = "s-gate-" + os.urandom(6).hex()
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self._ledger = dispatches.ledger_path
        self.ledger = os.path.join(self.tmp, "dispatches.jsonl")
        dispatches.ledger_path = staticmethod(lambda: self.ledger)

    def tearDown(self):
        dispatches.ledger_path = self._ledger
        os.chdir(self._cwd)
        super().tearDown()

    def guard(self, payload):
        import json as _json
        stdin = _json.dumps(payload).encode()
        return self.cmd("stop-guard", ["--hook-json"], stdin=stdin)

    def _plant_foreign_ref(self, verdict=False):
        """A REAL review row whose ref is a real commit that is NOT the
        worktree's HEAD (a moved-head / other-lane gate)."""
        other = subprocess.run(
            ["git", "-C", self.root, "rev-parse", "HEAD~0"],
            capture_output=True, text=True, check=True,
            timeout=30).stdout.strip()
        subprocess.run(["git", "-C", self.root, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "second"], check=True, capture_output=True,
                       timeout=30)
        newer = subprocess.run(
            ["git", "-C", self.root, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
            timeout=30).stdout.strip()
        assert newer != self.head
        return self.plant(ref=newer, verdict=verdict)

    def plant(self, ref=None, kind="review", recipient="ds4pro",
              lane="lane-g", repo=None, verdict=False, force=False):
        """A REAL ledger row through the real writer (the projection's
        grammar is v3/seq-0; hand-planted JSONL is a fiction the replay
        discards). Rows land in the swapped ledger path from setUp.
        verdict=True mints a BOUND approve; verdict="fix" records a fix
        verdict (token-free evidence — mark_verdict accepts that, only an
        APPROVE demands a verified gate)."""
        # the real writer now refuses an identityless author (floor-is-never-
        # an-author, 2026-08-02); the planting identity is SCOPED to the write
        # so the stop-guard under test still runs exactly as un-named as the
        # hook process it models
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "gate-fixture"}):
            row = dispatches.add(recipient, lane, ref=ref or self.head,
                                 kind=kind, notify=False,
                                 repo=repo or self.root, new_work=True,
                                 force=force)
        self.assertIsNotNone(row, "plant: the real writer refused")
        # delivery OBSERVED, or the stop-whisper's needs-confirmation rung
        # blocks every test here for a reason orthogonal to the claims block
        dispatches._mark_delivered(row["id"], ref or self.head)
        if verdict == "fix":
            out, verr = dispatches.mark_verdict(
                row["id"], ref or self.head, "rework: see review notes",
                polarity="fix")
            self.assertIsNone(verr, "plant fix verdict: %s" % verr)
        elif verdict:
            # BIND the approve — the writer refuses an ungated one outright
            # (an ungated approve can never authorize a landing). Mint a
            # real receipt at the root repo's HEAD; every approve caller
            # reviews that same commit (self.head, or _plant_foreign_ref's
            # fresh tip, which moves root HEAD with it), so it binds.
            # SUITE is patched, not argv passed — a custom-argv receipt
            # records no interpreter and bind refuses it by design.
            from helm import gate as _gate
            with mock.patch.object(_gate, "SUITE",
                                   ("-c", "import sys; print("
                                    "'Ran 1 test in 0.0s\\n\\nOK', "
                                    "file=sys.stderr)")):
                g, gerr = _gate.run(repo=self.root)
            self.assertIsNone(gerr, "plant gate: %s" % gerr)
            out, verr = dispatches.mark_verdict(
                row["id"], ref or self.head, _gate.evidence_line(g),
                polarity="approve")
            self.assertIsNone(verr, "plant verdict: %s" % verr)
        return row

    def test_open_review_dispatch_matching_HEAD_exempts_and_SAYS_so(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant()
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("lane in gate", err)
        self.assertIn("ds4pro", err)
        self.assertIn("lease retained", err)

    def test_an_ABBREVIATED_ref_matches_the_full_HEAD(self):
        """The ledger stores the ref as typed at dispatch time — measured
        live: kimi's own gate row carried a22d98d for a full head."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(ref=self.head[:7])
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("lane in gate", err)

    def test_a_BOUND_approve_matching_HEAD_exempts_awaiting_land(self):
        """STAGE 3 (was test_a_BOUND_verdict_reblocks_the_very_next_stop,
        which pinned the lifecycle hole itself: the exemption lapsed the
        moment the reviewer APPROVED, exactly when the holder could least
        act — measured live 2026-07-31, every stop rc 2 on a lane whose
        only remaining verb was the integrator's land)."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(verdict=True)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)  # noqa: VACUOUS_ASSERTION — rc 0 is the allow; the assertIns below are the positive controls on err
        self.assertIn("approved", err)
        self.assertIn("awaiting the land window", err)
        self.assertIn("lease retained", err)

    def test_a_FIX_verdict_does_NOT_exempt_rework_is_owed(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(verdict="fix")
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn(self.res, err)
        self.assertNotIn("in gate", err)
        self.assertNotIn("awaiting the land window", err)

    def test_an_APPROVE_at_a_foreign_ref_does_NOT_exempt(self):
        """The ref↔HEAD match is the proof for the approved arm too — an
        approve of some OTHER commit says nothing about this worktree."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self._plant_foreign_ref(verdict=True)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn(self.res, err)
        self.assertNotIn("in gate", err)
        self.assertNotIn("awaiting the land window", err)

    def test_a_ref_mismatch_blocks_moved_head_is_new_work(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(ref="f" * 40) if False else self._plant_foreign_ref()
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertNotIn("in gate", err)

    def test_a_build_kind_row_never_exempts(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(kind="build")
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertNotIn("in gate", err)

    def test_no_ledger_at_all_blocks(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertNotIn("in gate", err)

    def test_two_live_gates_for_one_head_are_ambiguous_and_block(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant()
        self.plant(force=True)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertNotIn("in gate", err)

    def test_a_lane_NAME_alone_is_never_evidence(self):
        """A review row for the same lane NAME but a different ref must
        not exempt — the ref↔HEAD match is the only proof."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self._plant_foreign_ref()
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertNotIn("in gate", err)

    def test_a_ref_alone_is_never_evidence_foreign_lane_same_tip_blocks(self):
        """The other half of the coincidence: every fresh lane starts at
        some existing tip, so ANOTHER lane's review row at the same ref
        proves nothing about THIS lane (measured live 2026-07-31 — lane
        approved-lane-exempts-stop, freshly branched at an unlanded stack
        tip, was exempted by verdict-polarity-consolidated's row 1267f509
        purely because the refs coincided)."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(lane="prospero")   # same ref (HEAD), different family
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn(self.res, err)
        self.assertNotIn("in gate", err)

    def test_a_round_suffixed_lane_row_is_the_same_family_and_exempts(self):
        """Premise lane-names-are-a-family-probe-the-stem: a -review/-rN/
        -xrev rename is the SAME work family, so the correspondence check
        must not reintroduce exact-string blindness."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(lane="lane-g-review-r2")
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)  # noqa: VACUOUS_ASSERTION — rc 0 is the allow; the assertIn below is the positive control on err
        self.assertIn("in gate", err)

    def test_the_ALLOW_exit_carries_the_advisory_and_no_block_text(self):
        """Emission routing, allow side: an advisory-only state exits 0
        with the advisory, and nothing block-shaped rides along."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant()
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)  # noqa: VACUOUS_ASSERTION — rc 0 is the allow; the assertIn below is the positive control on err
        self.assertIn("lease retained", err)
        self.assertNotIn("live claim lease(s)", err)

    def test_a_blocking_lease_keeps_advisories_OFF_the_exit2_emission(self):
        """Emission routing, block side: Claude Code renders EVERY exit-2
        stop-hook emission as a red "Stop hook error:", so an advisory
        printed beside a block turns "stop allowed, lease retained" into
        error text — the owner watched exactly that red line all evening
        (2026-07-31). A block emits ONLY the items that demand action."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant()          # this lease is exempt: advisory, not a demand
        seats.claim("worktree:proj:ghost", "alice", ttl=60,
                    session=self.sid)   # no lane room: UNKNOWN, blocks
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn("worktree:proj:ghost", err)
        self.assertNotIn("lease retained", err)
        self.assertNotIn("in gate", err)


class StopGuardRoomUnfinishedTest(SeatsBase):
    """#112. The release advice under a held LANE lease used to ask about
    DELEGATION — and delegation was the wrong question AND an unanswerable
    one. Four live holds measured in one night: an outstanding review, a
    gate in flight, a cure awaiting re-review after a FIX verdict, and one
    genuine subagent build. The correct answer was HOLD all four times and
    delegation explained exactly ONE, so the guard's basis was right 1 time
    in 4 — and the other three were correct holds for reasons no subagent
    probe can see.

    The wider question — IS THERE UNFINISHED WORK BOUND TO THIS ROOM? — is
    deterministically computable from state helm already keeps, through the
    four primitives that already own it (`_room_status`, `_merge_state`,
    `dispatches.owed`, `gate.inflight`). Every case here plants REAL state
    in a REAL scratch repo and asserts the ADVICE NAMES IT; every one has
    its negative, because a sentence that appears whatever the room holds
    would be decoration, not a measurement."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        r = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root,
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.commit("tip", self.root)
        self.head = self.rev(self.root)
        self.wt = self.root + "-wt/lane-u"
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-u", self.wt], check=True,
                       capture_output=True, timeout=30)
        self.res = "worktree:proj:lane-u"
        self.sid = "s-room-" + os.urandom(6).hex()
        self._cwd = os.getcwd()
        os.chdir(self.root)             # the guard anchors the project from cwd
        self._ledger = dispatches.ledger_path
        self.ledger = os.path.join(self.tmp, "dispatches.jsonl")
        dispatches.ledger_path = staticmethod(lambda: self.ledger)

    def tearDown(self):
        dispatches.ledger_path = self._ledger
        os.chdir(self._cwd)
        super().tearDown()

    def commit(self, msg, cwd):
        subprocess.run(["git", "-C", cwd, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", msg], check=True, capture_output=True,
                       timeout=30)

    def rev(self, cwd, ref="HEAD"):
        return subprocess.run(["git", "-C", cwd, "rev-parse", ref],
                              capture_output=True, text=True, check=True,
                              timeout=30).stdout.strip()

    def plant(self, ref=None, recipient="ds4pro", lane="lane-u", repo=None):
        """A REAL review row through the REAL writer — hand-planted JSONL is
        a fiction the replay discards."""
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "room-fixture"}):
            row = dispatches.add(recipient, lane, ref=ref or self.head,
                                 kind="review", notify=False,
                                 repo=repo or self.root, new_work=True)
        self.assertIsNotNone(row, "plant: the real writer refused")
        dispatches._mark_delivered(row["id"], ref or self.head)
        return row

    def other_repo(self, name="other-project"):
        """A SECOND real repository — the foreign half of @codex-2's
        two-repo fixture. Its head is a valid ref for the REAL writer, so
        a foreign row is stamped with a repo_id the writer actually emits,
        never a hand-typed shape."""
        other = os.path.join(self.tmp, name)
        os.makedirs(other)
        r = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other,
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.commit("tip", other)
        return other

    def corrupt_ledger_repo_id(self, value):
        """Rewrite the ledger's `repo_id` to a NON-CANONICAL value, leaving
        every other byte the real writer emitted. @codex-2's repro used the
        integer 123: truthy, unequal to the room's, and validated by
        nothing. As with `age_ledger_to_pre_repo_id`, the caller must
        re-prove the row still rides the REAL owed replay before reading."""
        with open(self.ledger) as fh:
            lines = fh.readlines()
        with open(self.ledger, "w") as fh:
            for ln in lines:
                d = json.loads(ln)
                d["repo_id"] = value
                fh.write(json.dumps(d) + "\n")

    def test_a_MALFORMED_repo_id_is_UNREADABLE_never_silently_foreign(self):
        """A `repo_id` that is truthy but not a canonical path is NOT proof
        of another project — it is a value nothing validated, and treating
        it as foreign EXCLUDES the row leaving no trace at all.

        @codex-2's exact repro: rewrite a real row's `repo_id` to the
        integer 123. Replay still owes it, and the first cut answered
        findings=[] unknowns=[] — a false clean assembled from an
        unvalidated field. Exclusion is the one outcome with no residue,
        so it must be earned by a POSITIVE canonical identity rather than
        by mere inequality."""
        self.ledger = os.path.join(self.tmp, "d-malformed.jsonl")
        row = self.plant(ref=self.head)
        self.corrupt_ledger_repo_id(123)
        # The row must still ride the REAL owed replay, or this arm would be
        # asserting about a row the reader never saw — same re-proof the
        # legacy arm makes, not assumed.
        snap, note = seats._ledger_snapshot()
        self.assertFalse(note, note)
        carried = [r for r in dispatches.owed(snap) if r.get("id") == row["id"]]
        self.assertEqual(len(carried), 1,
                         "fixture: replay must still owe the corrupted row")
        self.assertEqual(carried[0].get("repo_id"), 123)
        findings, unknowns = seats._room_unfinished(self.res)
        reviews = [x for x in findings if "review dispatch" in x]
        unread = [x for x in unknowns if x.startswith("review:")]
        self.assertEqual(reviews, [], "a row helm cannot identify must not "
                                      "be reported as this room's work")
        self.assertEqual(len(unread), 1,
                         "a malformed repo_id must contribute exactly ONE "
                         "review UNKNOWN, not silence and not N entries")
        self.assertIn("READABLE", unread[0])

    def test_MALFORMED_and_KNOWN_FOREIGN_do_not_share_an_outcome(self):
        """The discrimination that matters is malformed vs FOREIGN, because
        only foreign earns silent exclusion. Malformed shares its signature
        with legacy-unscoped ON PURPOSE — both are unreadable — so the pair
        this arm separates is the one where being wrong is invisible."""
        other = self.other_repo()

        def owes(row_id):
            snap, note = seats._ledger_snapshot()
            self.assertFalse(note, note)
            return len([r for r in dispatches.owed(snap)
                        if r.get("id") == row_id])

        self.ledger = os.path.join(self.tmp, "d-mal.jsonl")
        mal_row = self.plant(ref=self.head)
        self.corrupt_ledger_repo_id(123)
        self.assertEqual(owes(mal_row["id"]), 1,
                         "fixture: replay must still owe the corrupted row")
        f_mal, u_mal = seats._room_unfinished(self.res)
        mal = (len([x for x in f_mal if "review dispatch" in x]),
               len([x for x in u_mal if x.startswith("review:")]))
        self.ledger = os.path.join(self.tmp, "d-for.jsonl")
        for_row = self.plant(ref=self.rev(other), repo=other)
        self.assertEqual(owes(for_row["id"]), 1,
                         "fixture: replay must still owe the foreign row")
        f_for, u_for = seats._room_unfinished(self.res)
        foreign = (len([x for x in f_for if "review dispatch" in x]),
                   len([x for x in u_for if x.startswith("review:")]))
        self.assertEqual(mal, (0, 1))       # unreadable -> named as UNKNOWN
        self.assertEqual(foreign, (0, 0))   # positively elsewhere -> excluded
        self.assertNotEqual(mal, foreign,
                            "an unvalidated repo_id was treated as proof of "
                            "another project — the silent-exclusion hole")

    def test_the_SAME_repo_spelled_ODDLY_is_never_silently_excluded(self):
        """The room's own work, written with a non-normalised `repo_id`, is
        still the room's own work. `room_id` comes through `_repo_info`'s
        `os.path.realpath`; the row side was merely stripped, so the two
        halves of the `==` normalised DIFFERENTLY and the loser was dropped
        with no trace — an exclusion earned by INEQUALITY, which is the one
        thing this lane's own law forbids (@helm-claude-2's T2 finding).

        Every spelling below resolves to the room's canonical id and is
        DERIVED from it, never typed, so the arm cannot drift from whatever
        the writer actually stamps. The canonical spelling rides the SAME
        assertions as a control: if the reader stopped seeing same-room work
        at all, the control fails first and the two findings cannot pass by
        being uniformly invisible."""
        canon = (dispatches._repo_info(self.root) or {}).get("repo_id")
        self.assertTrue(canon and canon.startswith(os.sep),
                        "fixture: the room must have a canonical repo_id")

        def read_under(i, spelling):
            """Plant a REAL row, rewrite ONLY its `repo_id` to `spelling`,
            re-prove the replay still owes it, and return what the room read
            said. Every case travels this one path, so the controls and the
            findings differ in the spelling and in nothing else."""
            self.ledger = os.path.join(self.tmp, "d-spell-%d.jsonl" % i)
            row = self.plant(ref=self.head)
            self.corrupt_ledger_repo_id(spelling)
            snap, note = seats._ledger_snapshot()
            self.assertFalse(note, note)
            carried = [r for r in dispatches.owed(snap)
                       if r.get("id") == row["id"]]
            self.assertEqual(len(carried), 1,
                             "fixture: replay must still owe the row")
            self.assertEqual(carried[0].get("repo_id"), spelling)
            findings, unknowns = seats._room_unfinished(self.res)
            return ([f for f in findings if "OPEN review dispatch" in f],
                    [x for x in unknowns if x.startswith("review:")])

        # THE CONTROL RUNS UNCONDITIONALLY, ahead of any loop. A reader that
        # stopped seeing same-room work AT ALL would fail both spellings
        # below with the identical `0 != 1` and then go identically green on
        # any change that restored nothing — so the claim "the odd spellings
        # were excluded" is only measurable against a canonical row proven
        # visible on the same code path, in the same test, every run.
        named, unread = read_under(0, canon)
        self.assertEqual(len(named), 1,
                         "control: a canonically-spelled same-room review "
                         "must be NAMED, or this arm measures nothing")
        self.assertEqual(unread, [],
                         "control: a canonical id is readable")

        # SECOND CONTROL, on the OTHER observable. Every `unread == []` in
        # this arm is an ABSENCE, and an absence is only a measurement once
        # something is shown to reach that list on the same path — otherwise
        # a reader whose UNKNOWNs never carried the `review:` prefix would
        # satisfy every one of them forever. @codex-2's integer repro is the
        # cheapest thing that must land there.
        named, unread = read_under(1, 123)
        self.assertEqual(named, [],
                         "control: an unreadable id is not this room's work")
        self.assertEqual(len(unread), 1,
                         "control: an unreadable id MUST reach the review "
                         "UNKNOWN channel, or `unread == []` proves nothing")

        # The two spellings run UNROLLED, not in a loop. A loop body's
        # absence assertion is covered only by that same body's positive
        # one, so an empty sequence silences BOTH and the arm passes having
        # measured nothing — the vacuity this file's rung exists to catch.
        # Two cases do not need a loop to hide behind.
        parent, base = os.path.split(canon)
        doubled = parent + os.sep + os.sep + base
        self.assertNotEqual(doubled, canon, "fixture: a doubled separator "
                                            "must differ from the canonical")
        self.assertEqual(os.path.realpath(doubled), canon,
                         "fixture: it must still name the room's own repo")
        named, unread = read_under(2, doubled)
        self.assertEqual(len(named), 1,
                         "a DOUBLED SEPARATOR names this room's open review "
                         "and must not be dropped over a spelling comparison")
        self.assertEqual(unread, [],
                         "a resolvable same-room spelling is READABLE — it "
                         "must not be laundered into an UNKNOWN either")

        dotdot = os.sep.join([parent, os.pardir,
                              os.path.basename(parent), base])
        self.assertNotEqual(dotdot, canon, "fixture: a dot-dot round trip "
                                           "must differ from the canonical")
        self.assertEqual(os.path.realpath(dotdot), canon,
                         "fixture: it must still name the room's own repo")
        named, unread = read_under(3, dotdot)
        self.assertEqual(len(named), 1,
                         "a DOT-DOT ROUND TRIP names this room's open review "
                         "and must not be dropped over a spelling comparison")
        self.assertEqual(unread, [],
                         "a resolvable same-room spelling is READABLE — it "
                         "must not be laundered into an UNKNOWN either")

    def test_a_BARE_SLASH_repo_id_stays_UNREADABLE_after_normalising(self):
        """The negative that keeps the cure honest. Resolving the row side
        makes `/` a perfectly valid path — the filesystem root — so a naive
        `realpath` BEFORE the absolute-path guard would promote a junk value
        into a readable identity and silently exclude it as foreign. The
        `rstrip` stays ahead of the guard, so a bare separator still collapses
        to unreadable and the row rides `unknowns` where it belongs."""
        self.ledger = os.path.join(self.tmp, "d-bare-slash.jsonl")
        row = self.plant(ref=self.head)
        self.corrupt_ledger_repo_id(os.sep)
        snap, note = seats._ledger_snapshot()
        self.assertFalse(note, note)
        carried = [r for r in dispatches.owed(snap)
                   if r.get("id") == row["id"]]
        self.assertEqual(len(carried), 1,
                         "fixture: replay must still owe the row")
        self.assertEqual(carried[0].get("repo_id"), os.sep)
        findings, unknowns = seats._room_unfinished(self.res)
        named = [f for f in findings if "OPEN review dispatch" in f]
        unread = [x for x in unknowns if x.startswith("review:")]
        self.assertEqual(named, [], "a bare separator identifies nothing")
        self.assertEqual(len(unread), 1,
                         "a bare separator must contribute exactly ONE review "
                         "UNKNOWN — not silence, and not N entries")
        self.assertIn("READABLE", unread[0])

    def age_ledger_to_pre_repo_id(self):
        """Rewrite the ledger's rows to the bytes a PRE-repo_id writer
        emitted: today's real-writer rows minus the one field that did not
        exist yet. NOT hand-built JSON — every row was written by the real
        writer first, and the caller must re-prove the aged row still rides
        the REAL snapshot/owed replay before reading anything from it."""
        with open(self.ledger) as fh:
            lines = fh.readlines()
        with open(self.ledger, "w") as fh:
            for ln in lines:
                d = json.loads(ln)
                d.pop("repo_id", None)
                fh.write(json.dumps(d) + "\n")

    # --- read 1: a live worker's uncommitted bytes ------------------------
    def test_uncommitted_bytes_in_the_room_are_NAMED(self):
        """The read that covers the ONE specimen delegation got right — a
        delegate genuinely building leaves files behind, and those files are
        measurable where the delegate is not."""
        clean, unknowns = seats._room_unfinished(self.res)
        self.assertEqual(unknowns, [], "a live scratch room must read clean")
        self.assertEqual([f for f in clean if "UNCOMMITTED" in f], [],
                         "the negative control: a clean room claims nothing")
        with open(os.path.join(self.wt, "half_built.py"), "w") as fh:
            fh.write("x = 1\n")
        dirty, unknowns = seats._room_unfinished(self.res)
        self.assertEqual(unknowns, [])
        named = [f for f in dirty if f.startswith("UNCOMMITTED changes")]
        self.assertEqual(len(named), 1, dirty)
        self.assertRegex(named[0],
                         r"^UNCOMMITTED changes in the room \(newest write \d+s ago\)$")

    # --- read 2: committed work the trunk does not have -------------------
    def test_a_committed_but_UNLANDED_tip_is_NAMED(self):
        """Specimens 1-3 share this shape: the lane is CLEAN and the work is
        real, sitting in commits the trunk has never seen. Delegation
        answers NO to all three and would have advised opening the room."""
        landed, _u = seats._room_unfinished(self.res)
        self.assertEqual([f for f in landed if "NOT landed" in f], [],
                         "a room at the branch point owes the trunk nothing")
        self.commit("lane work", self.wt)
        tip = self.rev(self.wt)
        unlanded, unknowns = seats._room_unfinished(self.res)
        self.assertEqual(unknowns, [])
        self.assertIn("the room's tip %s is NOT landed by ancestry or patch "
                      "identity vs the trunk" % tip[:12], unlanded)

    # --- read 3: somebody else holds the verdict --------------------------
    def test_an_open_review_is_NAMED_at_the_exact_tip_the_exemption_cannot_use(self):
        """WIDER THAN THE EXEMPTION, on purpose. `_gate_pending` demands
        ref == HEAD because it grants an UN-GUARDING; this only NAMES what
        is outstanding, so a review row whose ref the holder has since
        committed past is still named — and that is exactly the specimen
        (cure-on-top-of-review) the exemption is built to refuse."""
        row = self.plant(ref=self.head)
        self.commit("cure", self.wt)
        self.assertIsNone(seats._gate_pending(self.res),
                          "precondition: the exemption must NOT fire here")
        findings, unknowns = seats._room_unfinished(self.res)
        self.assertEqual(unknowns, [])
        named = [f for f in findings if "OPEN review dispatch" in f]
        self.assertEqual(len(named), 1, findings)
        self.assertIn(row["id"][:12], named[0])
        self.assertIn("ds4pro", named[0])
        self.assertIn("NOT the room's current tip", named[0])
        # the negative: a CLOSED obligation is not outstanding work
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "room-fixture"}):
            done, err = dispatches.mark_cancel(row["id"], "withdrawn")
        self.assertIsNone(err, err)
        self.assertTrue(done)
        after, _u = seats._room_unfinished(self.res)
        self.assertEqual([f for f in after if "review dispatch" in f], [],
                         "a cancelled row must stop being named")

    def test_an_open_review_AT_the_tip_says_so_rather_than_guessing(self):
        """The ref DOES match here, so the wording must change with the
        fact. (This shape normally exits earlier through the exemption; the
        read is asserted directly because the sentence has to be right
        wherever it is reached — a moved lane stem, an ambiguous pair.)"""
        row = self.plant(ref=self.head)
        findings, _u = seats._room_unfinished(self.res)
        named = [f for f in findings if "OPEN review dispatch" in f]
        self.assertEqual(len(named), 1, findings)
        self.assertIn("at this exact tip", named[0])
        self.assertNotIn("NOT the room's current tip", named[0])
        self.assertIn(row["id"][:12], named[0])

    def test_a_same_lane_review_in_ANOTHER_repo_is_NOT_this_rooms_work(self):
        """@codex-2's exact two-repo fixture, real writer + real owed
        frontier. `_room_unfinished` derived only the lane STEM, so the one
        open review named `lane-u` — living in a DIFFERENT project — was
        printed as UNFINISHED WORK BOUND TO THIS ROOM with unknowns=[]. A
        lane name is free text shared across every project on the box; the
        row's writer-stamped repo_id against `dispatches._repo_info(room)`
        is the identity, and a row POSITIVELY scoped elsewhere is not this
        room's work in any sense — excluded, not UNKNOWN."""
        other = self.other_repo()
        foreign = self.plant(ref=self.rev(other), repo=other)
        # the fixture's own premise, measured: the writer stamped a real,
        # DIFFERENT canonical repo_id on the foreign row
        snap, note = seats._ledger_snapshot()
        self.assertFalse(note, note)
        frow = snap[foreign["id"]]
        self.assertTrue(frow.get("repo_id"), "the real writer stamps repo_id")
        self.assertNotEqual(frow["repo_id"],
                            dispatches._repo_info(self.wt)["repo_id"])
        findings, unknowns = seats._room_unfinished(self.res)
        self.assertEqual([f for f in findings if "review dispatch" in f], [],
                         findings)
        self.assertEqual(unknowns, [],
                         "a KNOWN foreign row is excluded, never UNKNOWN")
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep)
        self.assertIn("found nothing bound to this room YET", text)
        # POSITIVE CONTROL on the same observable: the same lane stem in
        # THIS repo IS named, and the foreign row still is not
        mine = self.plant(ref=self.head)
        findings2, unknowns2 = seats._room_unfinished(self.res)
        named = [f for f in findings2 if "OPEN review dispatch" in f]
        self.assertEqual(len(named), 1, findings2)
        self.assertIn(mine["id"][:12], named[0])
        self.assertNotIn(foreign["id"][:12], "\n".join(findings2))
        self.assertEqual(unknowns2, [])

    def test_a_LEGACY_unscoped_same_lane_row_is_UNKNOWN_never_same_project_proof(self):
        """The third arm, the one it is tempting to collapse either way. A
        row predating the repo_id field is RELEVANT (same lane family) but
        UNPROVABLE (scoped to nothing), so it may neither count as
        same-project proof nor vanish like a known-foreign row: it rides
        `unknowns` — task/142's settled Stop rule (unscoped relevant rows
        contribute UNKNOWN), applied at this read."""
        row = self.plant(ref=self.head)
        self.age_ledger_to_pre_repo_id()
        # the aging is honest only if the REAL replay still owes the row —
        # re-proven here, not assumed
        snap, note = seats._ledger_snapshot()
        self.assertFalse(note, note)
        carried = [r for r in dispatches.owed(snap)
                   if r.get("id") == row["id"]]
        self.assertEqual(len(carried), 1,
                         "fixture: replay must still owe the aged row")
        self.assertFalse(carried[0].get("repo_id"))
        findings, unknowns = seats._room_unfinished(self.res)
        self.assertEqual([f for f in findings if "review dispatch" in f], [],
                         "an unprovable row may not be named as this room's")
        named = [u for u in unknowns if u.startswith("review:")]
        self.assertEqual(len(named), 1, unknowns)
        # The contract BROADENED when malformed joined absent: the entry no
        # longer says "no repo identity" (which named only the missing case)
        # but "no READABLE repo identity", which covers absent AND
        # non-canonical. Asserting the OLD substring would pin a narrower
        # promise than production makes — @codex-2 caught this arm stale on
        # gate:5ea3b7210678a500, red on wording alone with every behavioural
        # assertion around it passing.
        self.assertIn("no READABLE repo identity", named[0])
        self.assertIn("UNKNOWN", named[0])
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep, "an UNKNOWN review may not authorise a release")
        self.assertIn("1 of 4 reads could not be made", text)
        self.assertIn("NO release command is offered", text)

    def test_review_repo_scope_stays_THREE_WAY_distinct_at_this_consumer(self):  # noqa: VACUOUS_ASSERTION — sig["same-repo"] == (1, 0) and sig["legacy"] == (0, 1) are unconditional positive controls on the same observables (the review finding, the review: unknown); the foreign empty discriminates against them
        """SAME-REPO / KNOWN-FOREIGN / LEGACY-UNSCOPED as (findings,
        unknowns) signatures over the review read: (1,0) / (0,0) / (0,1).
        Pairwise distinctness is the property — collapsing foreign into
        legacy manufactures uncertainty about rows helm can positively
        exclude, and collapsing legacy into either neighbour launders an
        unprovable row into proof or into silence. One arm per ledger, so
        no arm's rows shadow another's."""
        other = self.other_repo()
        sig = {}

        def read():
            f, u = seats._room_unfinished(self.res)
            return (len([x for x in f if "review dispatch" in x]),
                    len([x for x in u if x.startswith("review:")]))
        self.ledger = os.path.join(self.tmp, "d-mine.jsonl")
        self.plant(ref=self.head)
        sig["same-repo"] = read()
        self.ledger = os.path.join(self.tmp, "d-foreign.jsonl")
        self.plant(ref=self.rev(other), repo=other)
        sig["foreign"] = read()
        self.ledger = os.path.join(self.tmp, "d-legacy.jsonl")
        self.plant(ref=self.head)
        self.age_ledger_to_pre_repo_id()
        sig["legacy"] = read()
        self.assertEqual(sig["same-repo"], (1, 0))
        self.assertEqual(sig["foreign"], (0, 0))
        self.assertEqual(sig["legacy"], (0, 1))
        self.assertEqual(len(set(sig.values())), 3,
                         "two repo-scope states collapsed at the consumer")

    def test_an_UNREADABLE_room_identity_degrades_same_lane_rows_to_UNKNOWN(self):
        """When the room's own repo identity cannot be read there is no
        basis to classify ANY same-family row — same-repo and foreign are
        indistinguishable — so the whole review read degrades, in ONE
        counted entry (the `_missed` law: entries are per READ, and a
        partially-made read may not inflate the N-of-4 count). The missing
        checkout drives it here: three reads already degrade on the absent
        room, and the review read joins them the moment it holds rows it
        cannot tie down — 4 of 4, not a laundered clean."""
        self.plant(ref=self.head)
        shutil.rmtree(self.wt)
        findings, unknowns = seats._room_unfinished(self.res)
        self.assertEqual([f for f in findings if "review dispatch" in f], [],
                         findings)
        named = [u for u in unknowns if u.startswith("review:")]
        self.assertEqual(len(named), 1, unknowns)
        self.assertIn("repo identity is unreadable", named[0])
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep)
        self.assertIn("4 of 4 reads could not be made", text)

    # --- read 4: a suite is running in this room --------------------------
    def test_a_LIVE_gate_in_this_room_is_NAMED(self):  # noqa: VACUOUS_ASSERTION — len(named)==1 plus the pid assertIn on the SAME observable (the GATE IS RUNNING findings) is the unconditional positive control; the before/after empties are the discrimination
        """A LIVENESS PROOF, not a receipt's mtime: `gate.inflight` reports
        an owner file whose (boot, pid, start) still resolves to a running
        process. The owner registered here is this very test process, so
        the pid it prints is verifiable from inside the assertion."""
        from helm import gate as gatemod
        quiet, unknowns = seats._room_unfinished(self.res)
        self.assertEqual(unknowns, [])
        self.assertEqual([f for f in quiet if "GATE IS RUNNING" in f], [])
        nonce = gatemod._inflight_open(self.wt)
        self.assertIsNotNone(nonce, "the fixture could not register an owner")
        try:
            gating, unknowns = seats._room_unfinished(self.res)
        finally:
            gatemod._inflight_close(self.wt, nonce)
        self.assertEqual(unknowns, [])
        named = [f for f in gating if "GATE IS RUNNING" in f]
        self.assertEqual(len(named), 1, gating)
        self.assertIn("pid %d" % os.getpid(), named[0])
        after, _u = seats._room_unfinished(self.res)
        self.assertEqual([f for f in after if "GATE IS RUNNING" in f], [],
                         "a closed owner file must stop reading as a gate")

    def test_an_UNREADABLE_gate_dir_is_COUNTED_not_laundered_into_clean(self):
        """@codex-2's reproduction, pinned as the regression it was. Before
        the typed census, `gate.inflight()` swallowed a PermissionError on
        the marker dir into None and this read treated that None as a
        successful empty census: findings=[], unknowns=[], and the advice
        printed "no gate running" about a directory helm never saw — the
        function's own EVERY-READ-DEGRADES-TO-UNKNOWN contract, falsified
        by its narrowest read. Read 4 now consumes `gate.inflight_census`,
        so blindness lands in `unknowns` with its reason and the advice
        says COULD NOT MEASURE rather than asserting an absence.

        THE PATCH, NOT chmod 000: chmod does not bind root, so a root-run
        suite would make a chmod arm vacuous. The patch is scoped to the
        room's own marker dir; every other listdir answers honestly."""
        from helm import gate as gatemod
        d = gatemod.inflight_dir(self.wt)
        self.assertTrue(d, "the fixture room must resolve a marker dir")
        real = os.listdir

        def deny(path):
            if str(path) == str(d):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)
        with mock.patch.object(gatemod.os, "listdir", side_effect=deny):
            findings, unknowns = seats._room_unfinished(self.res)
            keep, text = seats._room_advice(self.res)
        self.assertEqual([f for f in findings if "GATE IS RUNNING" in f], [],
                         "an unmeasured gate may not be asserted either way")
        named = [u for u in unknowns if u.startswith("gate:")]
        self.assertEqual(len(named), 1, unknowns)
        self.assertIn("Permission denied", named[0])
        self.assertFalse(keep, "a blind census may not authorise a release")
        self.assertIn("could not be made", text)
        self.assertIn("NO release command is offered", text)
        self.assertNotIn("no gate running", text)
        self.assertNotIn("found nothing bound to this room", text)

    def test_gate_census_states_stay_THREE_WAY_distinct_at_this_consumer(self):  # noqa: VACUOUS_ASSERTION — sig["live"] == (1, 0) and sig["blind"] == (0, 1) are unconditional positive controls on the same observables (the GATE IS RUNNING finding, the gate: unknown); the empties discriminate against them
        """EMPTY / LIVE / UNREADABLE-owner-file, as (findings, unknowns)
        signatures on the SAME room: (0,0) / (1,0) / (0,1). Pairwise
        distinctness is the property — a census that collapsed any two
        would merge two of these signatures, and this arm is the one that
        goes red when it does. The unreadable arm here is the OWNER FILE
        (the dir lists fine); the dir arm has its own test above, so both
        blind shapes @codex-2 named are pinned at this consumer."""
        from helm import gate as gatemod
        sig = {}

        def read():
            f, u = seats._room_unfinished(self.res)
            return (len([x for x in f if "GATE IS RUNNING" in x]),
                    len([x for x in u if x.startswith("gate:")]))
        sig["empty"] = read()
        nonce = gatemod._inflight_open(self.wt)
        self.assertIsNotNone(nonce, "the fixture could not register an owner")
        try:
            sig["live"] = read()
            target = os.path.join(gatemod.inflight_dir(self.wt),
                                  nonce + ".json")
            real = open

            def deny(file, *args, **kwargs):
                if str(file) == str(target):
                    raise PermissionError(13, "Permission denied", str(target))
                return real(file, *args, **kwargs)
            with mock.patch("builtins.open", side_effect=deny):
                sig["blind"] = read()
        finally:
            gatemod._inflight_close(self.wt, nonce)
        self.assertEqual(sig["empty"], (0, 0))
        self.assertEqual(sig["live"], (1, 0))
        self.assertEqual(sig["blind"], (0, 1))
        self.assertEqual(len(set(sig.values())), 3,
                         "two gate census states collapsed at the consumer")

    # --- the composed advice ---------------------------------------------
    def test_an_ALL_CLEAN_room_REPORTS_but_offers_NO_RELEASE_COMMAND(self):
        """THE READS ARE DIAGNOSTIC, NEVER PERMISSIVE — the owner's ruling,
        against my first draft, on evidence I did not have.

        I had this branch print "4 reads found no unfinished work" and
        invite the release. Two measurements killed it. 2026-08-05: the
        guard fired on THREE lease-held rooms at once while a live
        Agent-tool subagent was building in ALL THREE, and
        `_delegated_build` fired for NONE of them — two of the three were
        caught by a SINGLE read (one by unlanded commits at uncommitted=0,
        one by uncommitted delta at ahead=0). Then the case that is not an
        extrapolation at all: a room 12 minutes old with a live spawned
        subagent measured uncommitted=0, ahead=0, no bound dispatch, no
        gate — CLEAN ON ALL FOUR.

        THIS FIXTURE IS THAT ROOM, synthetically: a lane at HEAD with no
        commits, no dirt, no dispatch and no gate marker. The advice must
        REPORT and must NOT hand anyone a runnable release."""
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep, "an all-clean room may not authorise a release")
        self.assertIn("4 reads found nothing bound to this room YET", text)
        self.assertIn("NO release command is offered", text)
        self.assertIn("CANNOT SEE a subagent that has not written a file yet",
                      text)
        self.assertIn("Lease retained", text)
        # it may not tell anyone the room is free
        self.assertNotIn("no unfinished work", text)
        self.assertNotIn("do NOT run that", text)

    def drive_four_situations(self, probe):
        """Drive the four situations `_room_advice` distinguishes, calling
        `probe(label, thunk)` for each.

        SHARED so the coverage arm and the distinctness arm cannot drift
        onto different fixtures — two arms that claim to be about the same
        four situations, silently exercising three and five, would make the
        cross-check between them meaningless.

        ORDERED, because the situations are made of real room state and the
        changes are one-way: clean -> commit -> remove. The last is driven
        by making the measurement itself raise, which is the only way to
        reach that branch without stubbing the function under test."""
        probe("all clean", lambda: seats._room_advice(self.res))
        self.commit("lane work", self.wt)
        probe("findings", lambda: seats._room_advice(self.res))
        shutil.rmtree(self.wt)
        probe("unknowns", lambda: seats._room_advice(self.res))
        with mock.patch.object(seats, "_room_unfinished",
                               side_effect=RuntimeError("unreadable")):
            probe("measure raised", lambda: seats._room_advice(self.res))

    def test_FOUR_SITUATIONS_get_FOUR_DISTINCT_ANSWERS(self):
        """THE MERGER CATCH, which the coverage arm structurally cannot do.

        That arm keys branch identity on RETURN LINE. Line identity is
        coarser than semantic identity, so branches MERGING onto one line —
        or collapsing into a dispatch table's single shared `return` —
        shrink BOTH sides of its `len(outcomes) == len(branches)` control at
        once and it stays green while covering less. It catches ADDITION,
        never MERGER.

        The cure is NOT a finer line identity. Bytecode offsets or
        `sys.monitoring` would chase the implementation's layout, which is
        exactly the thing a refactor is allowed to change. The property
        actually worth having is simpler and layout-free: DIFFERENT
        SITUATIONS GET DIFFERENT ANSWERS.

        That is refactor-proof in both directions. Two branches merging onto
        one line while still returning different values loses nothing, and
        this arm stays green — correctly. Two branches merging such that a
        CLEAN room and an UNREADABLE room now give the same answer is a real
        defect, and two fixtures collide, the set shrinks, and this goes red
        — caught by its consequence rather than by its syntax. A dispatch
        table with one shared `return` passes, as it should, so long as the
        answers still differ.

        Keyed on the WHOLE outcome `(keep, sentence)`, not the sentence
        alone: two situations that agree on the words but disagree on
        whether a command may print are still different answers."""
        outcomes = []
        self.drive_four_situations(
            lambda label, call: outcomes.append((label, *call())))
        self.assertEqual(len(outcomes), 4, "the driver stopped short")
        answers = {(keep, text) for _label, keep, text in outcomes}
        self.assertEqual(
            len(answers), len(outcomes),
            "two of the four situations gave the SAME (keep, sentence): %s"
            % sorted((label, keep, text[:60]) for label, keep, text in outcomes))

    def test_EVERY_advice_branch_either_OFFERS_or_EXPLAINS(self):  # noqa: VACUOUS_ASSERTION — len(outcomes)==len(branches) and missed==set() are unconditional and prove the loop runs over every branch, so no assertion in it can pass on an empty set; the rung cannot see that through the loop
        """WHAT MAKES THE HEADER'S PROSE SAFE RATHER THAN LUCKY.

        The header tells the reader that a lane helm could measure names
        its exact command and a lane it could not prove idle says why and
        offers none. That sentence is not a claim about any one line — it
        is an EXHAUSTIVENESS claim about `_room_advice`: EVERY branch
        either offers a command or states why none is offered. It happened
        to be true because the four branches partition that way, and a
        fifth branch that did neither would falsify the header without
        tripping any other arm here.

        DRIVEN FROM THE BRANCH SET ITSELF, not from a hand-written list of
        four fixtures — a hand-listed four is correct today and silent the
        day someone adds a fifth, which is the same defect as a hardcoded
        module pin. The branches are read out of `_room_advice`'s OWN AST
        (top-level `return`s, nested helpers excluded), a line tracer
        records which of them each fixture actually reaches, and the arm
        fails unless EVERY one was reached AND every reached one satisfies
        the disjunction. Add a branch no fixture covers -> red. Add a
        branch that neither offers nor explains -> red. Either way the
        failure lands at the moment of introduction, which is when it is
        still cheap to fix.

        THE DISJUNCTION, stated where it is actually decidable: this
        function returns (may_print_the_command, sentence) and the CALLER
        attaches the runnable command, so the property is
            keep is True  XOR  the sentence says no command is offered.
        keep=True lanes get their exact `work release ... --lease <id>`
        (pinned end to end by the FINDINGS and MIXED arms); keep=False
        lanes must say so in the sentence that replaces it."""
        import ast as _ast
        import inspect
        import textwrap

        fn = _ast.parse(textwrap.dedent(
            inspect.getsource(seats._room_advice))).body[0]
        base = seats._room_advice.__code__.co_firstlineno

        def top_returns(node):
            """Returns belonging to THIS function — a nested helper's return
            lives in a different code object the tracer never sees, so
            counting it would make the arm unsatisfiable rather than
            strict."""
            for child in _ast.iter_child_nodes(node):
                if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                      _ast.Lambda)):
                    continue
                if isinstance(child, _ast.Return):
                    yield child
                yield from top_returns(child)

        branches = {base + r.lineno - 1 for r in top_returns(fn)}
        self.assertGreaterEqual(len(branches), 4, "AST read found no branches")

        code = seats._room_advice.__code__
        reached, outcomes = set(), []

        def localtrace(frame, event, _arg):
            if event == "line":
                reached.add(frame.f_lineno)
            return localtrace

        def globaltrace(frame, _event, _arg):
            return localtrace if frame.f_code is code else None

        def probe(label, fn_call):
            sys.settrace(globaltrace)
            try:
                keep, text = fn_call()
            finally:
                sys.settrace(None)
            outcomes.append((label, keep, text))

        self.drive_four_situations(probe)

        # THE COUNT CONTROL, unconditional and load-bearing twice over: it
        # ties the FIXTURE set to the BRANCH set, so adding a fifth branch
        # without adding a fifth fixture is red on this line alone — before
        # coverage is even consulted — and it guarantees the loop below runs
        # over a non-empty set rather than passing on zero outcomes.
        self.assertEqual(len(outcomes), len(branches),
                         "%d branches in _room_advice but %d fixtures here — "
                         "every branch needs one that reaches it"
                         % (len(branches), len(outcomes)))
        missed = branches - reached
        self.assertEqual(missed, set(),
                         "advice branch(es) at line(s) %s are not covered by "
                         "any fixture here — a branch nobody exercises is a "
                         "branch nobody has checked offers-or-explains"
                         % sorted(missed))

        for label, keep, text in outcomes:
            self.assertTrue(text.strip(), "%s: advice may never be empty" % label)
            says_none = "NO release command is offered" in text
            self.assertNotEqual(
                bool(keep), says_none,
                "%s: branch must EITHER keep its command OR say none is "
                "offered, never both and never neither — got keep=%r, "
                "text=%r" % (label, keep, text))
        # DISTINCTNESS LIVES IN ITS OWN ARM, deliberately. Asserting it here
        # too would make this arm red on a MERGER as well, and then it could
        # not demonstrate what it structurally cannot see — the whole point
        # of the pair is that a merger leaves THIS arm green and reddens the
        # other one.

    def test_a_MIXED_stop_lets_every_lane_speak_for_itself(self):
        """THE HETEROGENEOUS SERMON — the normal case for an orchestrating
        seat, and the one with no coverage until now. Eight lane leases were
        held in one session while this was written, across at least three
        room states; a stop of that session prints a mixed block.

        The defect CLASS this pins is not "the header over-promises" or
        "the header under-promises" — it is THE HEADER ASSERTING ANYTHING
        ABOUT CONTENT IT DOES NOT OWN. That has now appeared twice one
        level apart (unconditional, then would-be per-stop), so the fix is
        structural: the header says why the stop is held and sends the
        reader down; each LINE owns its own lane. Nothing can be inherited,
        so a mixed sermon cannot lie in either direction.

        TWO ROOMS, ONE STOP: lane-u carries an UNLANDED COMMIT (measured →
        its exact command), lane-v is untouched (all clean → no command,
        and the caveat instead)."""
        second = self.root + "-wt/lane-v"
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-v", second], check=True,
                       capture_output=True, timeout=30)
        self.commit("lane work", self.wt)          # lane-u: MEASURED
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        seats.claim("worktree:proj:lane-v", "alice", ttl=600, session=self.sid)
        blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        sermon = next(b for b in blocks if "claim lease(s) held" in b)
        head, *rows = sermon.split("\n")
        by_lane = {}
        for row in rows:
            for lane in ("lane-u", "lane-v"):
                if row.strip().startswith("worktree:proj:" + lane):
                    by_lane[lane] = row
        self.assertEqual(sorted(by_lane), ["lane-u", "lane-v"], rows)

        # THE MEASURED LANE: its own exact, runnable command.
        self.assertIn("NOT landed by ancestry or patch identity",
                      by_lane["lane-u"])
        self.assertRegex(by_lane["lane-u"],
                         r"helm work release lane-u --lease [0-9a-f]{8,}")

        # THE CLEAN LANE: no command at all, and the reason in its place.
        self.assertIn("NO release command is offered", by_lane["lane-v"])
        self.assertIn("CANNOT SEE a subagent that has not written a file yet",
                      by_lane["lane-v"])
        self.assertNotIn("helm work release lane-v", by_lane["lane-v"])
        self.assertNotIn("--lease", by_lane["lane-v"])

        # THE HEADER: promises NEITHER, so neither line inherits anything.
        self.assertIn("EACH LANE BELOW CARRIES ITS OWN INSTRUCTION", head)
        self.assertNotIn("run the EXACT command shown", head)
        self.assertNotIn("NO release command is offered for any of them", head)
        self.assertNotIn("--lease", head)

    def test_the_all_clean_stop_prints_NO_runnable_release_for_that_lease(self):
        """The witness the ruling asked for, through the guard the owner
        actually reads: the suppression has to survive composition, not
        just live in the helper's return value. A clean lane lease's sermon
        line must carry the caveat and NO `helm work release`."""
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        text = " ".join(blocks)
        self.assertIn(self.res, text)              # the lease IS reported
        self.assertIn("NO release command is offered", text)
        self.assertNotIn("helm work release", text)
        self.assertNotIn("--lease", text)

    def test_the_advice_never_speaks_the_RESERVED_token_delegated(self):  # noqa: VACUOUS_ASSERTION — each of the three texts has an UNCONDITIONAL assertIn on its own content before the loop is entered, so no branch can pass on an empty string; the rung cannot pair them across the loop
        """A FORGED SIGNAL, caught by the regression sweep rather than by
        me. Two StopGuardDelegationTest cases read the ABSENCE of the token
        "delegated" from a stop's stderr as proof the delegation EXEMPTION
        did not fire. My all-clean sentence said "every delegated build",
        which put that token in front of them from a rung that had not
        fired — the same forged-token defect the sermon's own comment warns
        about for "auto-claimed", one string over.

        Pinned across EVERY branch, not just the one that broke, because
        the next edit will be to a different branch."""
        clean = seats._room_advice(self.res)[1]
        # UNCONDITIONAL POSITIVE CONTROL on the same observable, outside the
        # loop: this is the string the absences below are read from, so if it
        # were ever empty every assertNotIn would pass for free.
        self.assertIn("4 reads found nothing bound to this room YET", clean)
        cases = [("all clean", clean)]
        shutil.rmtree(self.wt)
        missing = seats._room_advice(self.res)[1]
        self.assertIn("3 of 4 reads could not be made", missing)
        cases.append(("missing room", missing))
        nolane = seats._room_advice("worktree:nosuchproj:x")[1]
        self.assertIn("4 of 4 reads could not be made", nolane)
        cases.append(("no lane room", nolane))
        for label, text in cases:
            self.assertNotIn("delegated", text, label)
        # the findings branch too, from a room that still exists
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-w", self.root + "-wt/lane-w"],
                       check=True, capture_output=True, timeout=30)
        self.commit("lane work", self.root + "-wt/lane-w")
        found = seats._room_advice("worktree:proj:lane-w")[1]
        self.assertIn("NOT landed by ancestry or patch identity", found)
        self.assertNotIn("delegated", found)

    def test_RECENT_FILE_MTIMES_ARE_NOT_A_SIGNAL_and_must_never_become_one(self):
        """NO MTIME RUNG. This is a tripwire against the fifth read the
        clean case invites someone to add.

        MEASURED 2026-08-05 on a 12-minute-old room with a live delegate:
        mtime fails in BOTH directions. Read naively it reported 510
        authored files touched in 15 minutes — pure CHECKOUT NOISE, every
        one stamped with the room's creation second, README.md and
        docs/VERBS.md included — which screams activity for a room nobody
        has touched. Read correctly as the newest AUTHORED write it said
        idle for a room that had a live subagent in it. It cannot rescue
        the clean case and an mtime rung would make it worse.

        So: touching every file to NOW, with no content change, must move
        NOTHING. The second half is the unconditional positive control —
        the same room with one byte of real content does report — so this
        is discrimination, not an absence assertion standing alone."""
        now = time.time()
        touched = 0
        for dirpath, _dirs, files in os.walk(self.wt):
            if ".git" in dirpath:
                continue
            for name in files:
                os.utime(os.path.join(dirpath, name), (now, now))
                touched += 1
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep)
        self.assertIn("4 reads found nothing bound to this room YET", text)
        # POSITIVE CONTROL on the same observable: real content DOES move it
        with open(os.path.join(self.wt, "actually_written.py"), "w") as fh:
            fh.write("x = 1\n")
        keep2, text2 = seats._room_advice(self.res)
        self.assertTrue(keep2)
        self.assertIn("UNCOMMITTED changes in the room", text2)

    def test_a_room_with_FINDINGS_still_gets_its_release_command(self):
        """The discrimination that stops the suppression being a blanket.
        A MEASURED room has standing: helm named what is in it, so a holder
        who knows that work is theirs to abandon gets a fully-informed
        line. Withhold everywhere and the reads really would be
        decorative."""
        self.commit("lane work", self.wt)
        with open(os.path.join(self.wt, "half_built.py"), "w") as fh:
            fh.write("x = 1\n")
        keep, text = seats._room_advice(self.res)
        self.assertTrue(keep, "a measured room keeps its command")
        self.assertIn("UNFINISHED WORK BOUND TO IT", text)
        self.assertNotIn("NO release command is offered", text)
        self.assertNotIn("4 reads found nothing bound to this room YET", text)
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        self.assertIn("helm work release lane-u --lease", " ".join(blocks))

    def test_a_MISSING_room_reports_three_missed_reads_and_withholds(self):
        """UNKNOWN-AND-SAY-SO, counted honestly. The count is derived from
        one entry PER READ, so the missing checkout cannot report itself as
        a single blind spot when it blinds three — and a room helm could
        not read has no more standing to authorise than a clean one."""
        shutil.rmtree(self.wt)
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep, "an unmeasured room may not authorise")
        self.assertIn("3 of 4 reads could not be made", text)
        for read in ("working tree", "landedness", "gate"):
            self.assertIn("%s: the claimed room is not on disk" % read, text)
        self.assertIn("an IDLE room is UNPROVEN", text)
        self.assertIn("NO release command is offered", text)
        self.assertNotIn("found no unfinished work", text)

    def test_an_unreadable_ledger_degrades_the_review_read_to_UNKNOWN(self):
        """The ledger read is the one that can fail without the checkout
        failing, and its failure must never collapse to 'no reviews'."""
        os.unlink(self.ledger) if os.path.exists(self.ledger) else None
        os.makedirs(self.ledger)         # a directory: unreadable as a ledger
        snap, note = seats._ledger_snapshot()
        self.assertTrue(note, "fixture: the ledger must read as unavailable")
        findings, unknowns = seats._room_unfinished(self.res, snap, note)
        self.assertEqual(findings, [])
        self.assertEqual(len(unknowns), 1, unknowns)
        self.assertTrue(unknowns[0].startswith(
            "review: the dispatch ledger could not be read"), unknowns)
        keep, text = seats._room_advice(self.res, snap, note)
        self.assertFalse(keep)
        self.assertIn("1 of 4 reads could not be made", text)

    # --- end to end, through the guard the owner actually reads -----------
    def test_the_release_advice_NAMES_THE_WORK_and_no_longer_asks_about_delegation(self):
        """The deliverable. Two real facts in the room, both named in the
        block that carries the release command — and the retired question
        gone from it."""
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        self.commit("lane work", self.wt)
        with open(os.path.join(self.wt, "half_built.py"), "w") as fh:
            fh.write("x = 1\n")
        blocks, _warns = seats.stop_guard(session=self.sid, seat="alice")
        text = " ".join(blocks)
        # the command still prints in full — the advice is a caveat ON it,
        # not a replacement FOR it
        self.assertIn("helm work release lane-u --lease", text)
        self.assertIn("UNFINISHED WORK BOUND TO IT", text)
        self.assertIn("UNCOMMITTED changes in the room", text)
        self.assertIn("NOT landed by ancestry or patch identity", text)
        self.assertIn("Releasing it opens the lane on that work", text)
        # THE RETIRED QUESTION, pinned gone
        self.assertNotIn("delegation is UNKNOWN", text)
        self.assertNotIn("SUBAGENT is building in this lane", text)

    def test_the_claims_block_reads_the_190ms_ledger_ONCE_for_N_lane_leases(self):  # noqa: VACUOUS_ASSERTION — len(folds)==3 counts the SAME instrumented reads the rows_reads==[] assertion is about, and the advice assertIn proves the rungs ran at all
        """COST, asserted rather than hoped for.

        The ledger fold measured 190ms on a 1,577-event file and the claims
        block asks it TWICE PER LANE LEASE — once for the gate-pending
        exemption, once for the review read. Unthreaded that is 2N reads on
        every stop; threaded it is one, whatever N is. TWO rooms here, so a
        per-lease read cannot hide behind a count of one.

        `dispatches.rows` is asserted UNCALLED because that is the exact call
        the exemption used to make: it is the mutation site, and a revert of
        the threading turns this red rather than merely slower."""
        second = self.root + "-wt/lane-v"
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-v", second], check=True,
                       capture_output=True, timeout=30)
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        seats.claim("worktree:proj:lane-v", "alice", ttl=600, session=self.sid)
        self.commit("lane work", self.wt)
        folds, rows_reads = [], []
        real_snapshot, real_rows = dispatches.snapshot, dispatches.rows

        def counted_snapshot():
            folds.append(1)
            return real_snapshot()

        def counted_rows():
            rows_reads.append(1)
            return real_rows()

        with mock.patch.object(dispatches, "snapshot", counted_snapshot), \
                mock.patch.object(dispatches, "rows", counted_rows):
            blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        self.assertIn("UNFINISHED WORK BOUND TO IT", " ".join(blocks))
        self.assertEqual(rows_reads, [],
                         "the gate-pending exemption re-read the ledger")
        # 1 claims block (both leases, both rungs) + 1 review-spiral rung +
        # 1 stop-whisper rung. The two sibling rungs are outside this lane;
        # the number is pinned so a NEW read cannot appear unremarked.
        self.assertEqual(len(folds), 3,
                         "the ledger was folded %d times in one stop"
                         % len(folds))

    def test_a_LATCHED_re_stop_does_not_recompute_advice_nobody_prints(self):
        """The other half of the cost story. The same-state latch compresses
        a re-stop to one line that never carried a hint, so the four reads
        must not run for it — the advice is built inside the branch that
        prints it."""
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        self.commit("lane work", self.wt)
        calls, real = [], seats._room_unfinished

        def counted(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        with mock.patch.object(seats, "_room_unfinished", counted):
            blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
            self.assertEqual(len(calls), 1, "the first stop must measure")
            self.assertIn("UNFINISHED WORK BOUND TO IT", " ".join(blocks))
            _b2, warns2 = seats.stop_guard(session=self.sid, seat="alice")
        self.assertIn("unchanged since the last warning", " ".join(warns2))
        self.assertEqual(len(calls), 1,
                         "the compressed re-stop paid for four git reads")

    def test_a_LATCHED_second_stop_promises_nothing_the_first_did_not_print(self):
        """THE HEADER RULING, EXTENDED TO THE LATCH PATH — the one path the
        heterogeneous mixed-lease witness never drives. The latched
        one-liner said "the full detail (exact release commands) printed
        then" UNCONDITIONALLY, but a first stop on a room helm could not
        prove idle withholds EVERY command — so the second stop pointed its
        reader back at commands that were never printed: the header/line
        coupling surviving one path over. The ruling is structural and
        already applied to the sermon: a summary owns the sermon's SHAPE
        (each line carried its own instruction, or said why none is
        offered — the exhaustiveness the branch arm pins), never its
        CONTENT. This drive is a first stop that WITHHELD, then a second on
        the same state."""
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        first = " ".join(blocks)
        # the control's premise, MEASURED rather than assumed: the first
        # stop really did withhold every release command (all-clean room)
        self.assertIn(self.res, first)
        self.assertIn("NO release command is offered", first)
        self.assertNotIn("helm work release", first)
        _b2, warns2 = seats.stop_guard(session=self.sid, seat="alice")
        latched = [w for w in warns2
                   if "unchanged since the last warning" in w]
        self.assertEqual(len(latched), 1, warns2)
        line = latched[0]
        # the old unconditional claim, pinned gone — it was FALSE in this
        # exact drive
        self.assertNotIn("exact release commands", line)
        self.assertNotIn("helm work release", line)
        self.assertNotIn("--lease", line)
        # what it may say instead: the structure every sermon line is
        # proven to have, content unasserted
        self.assertIn("each line carried its own instruction", line)
        self.assertIn("why none is offered", line)
        self.assertIn("1 lease(s) held", line)


class StopGuardRenewingLeaseTest(SeatsBase):
    """A lease whose remaining TTL RISES (or holds flat) between two reads
    has a live renewer behind it — the gate legacy-lock daemon refreshes
    gatelock:<project> every 5s against a 30s TTL (gate.py
    _GATE_LEGACY_RENEW_S/_GATE_LEGACY_TTL_S), so a healthy in-flight gate
    lease displays ~26-29s left INDEFINITELY. The claims block printed that
    number beside a pre-filled release command and led with "run the EXACT
    command shown": a number that looks like an expiry plus a command
    presented as the remedy made releasing the correct-looking act, and
    releasing the runner's lock kills the suite mid-run — measured live
    twice in one day (hc2 ~12:00Z, caught only by sampling twice and seeing
    the remainder go UP; kimi 18:29Z gate #29, released, renewer failed
    "gatelock:helm is not claimed; suite killed", gate closed UNKNOWN).
    Only a strictly DECREASING remainder is a real strand; a renewing lease
    must NAME the live run instead of offering the command that breaks it.
    One read cannot tell renewing from lapsing, so the guard reads the
    claims file twice with a short pause; a slow hook is fine — a killed
    suite is not."""

    def setUp(self):
        super().setUp()
        self.sid = "s-renew-" + os.urandom(6).hex()

    def guard(self):
        import json as _json
        stdin = _json.dumps({"session_id": self.sid}).encode()
        return self.cmd("stop-guard", ["--hook-json"], stdin=stdin)

    def _expire_in(self, resource, seconds):
        """Point the stored row's expiry at now+seconds — the shape a live
        renewer (near full TTL) or a genuinely lapsing lease (near 0)
        presents, without sleeping the test for either."""
        with seats._flocked(seats.claims_path() + ".lock"):
            c = pk.read_json(seats.claims_path(), {}) or {}
            c[resource]["exp_mono"] = seats._now_mono() + seconds
            c[resource]["exp_wall"] = time.time() + seconds
            pk.write_json(seats.claims_path(), c)

    def test_renewing_lease_allows_the_stop_and_retains_the_lease(self):
        # production shape: renewed every 5s against a 30s TTL, the row a
        # stop actually meets shows ~24-29s remaining — never near 0.
        # The renewal is proof of an OUT-OF-TURN holder, so the stop is
        # ALLOWED (a blocked stop only forces a poll loop against the
        # seat's own suite — three in one session, 2026-08-03); what the
        # b4485c30 fix established is RETAINED: no release command.
        seats.claim("gatelock:proj", "alice", ttl=30, session=self.sid)
        self._expire_in("gatelock:proj", 27.0)
        rc, _o, err = self.guard()
        self.assertEqual(rc, 0, err)
        self.assertIn("RENEWED", err)
        self.assertIn("Stop allowed, lease retained", err)
        self.assertNotIn("live claim lease(s) held", err)
        self.assertNotIn("helm chat release", err)
        # AND THE LEASE SURVIVED THE ALLOWED STOP — retained, not released
        c = pk.read_json(seats.claims_path(), {}) or {}
        self.assertIn("gatelock:proj", c)

    def test_decreasing_lease_keeps_the_release_command(self):
        # a genuinely lapsing lease: minted 30s and left alone, it falls
        # toward 0 — THIS is the strand the release command exists for
        seats.claim("gatelock:proj", "alice", ttl=30, session=self.sid)
        self._expire_in("gatelock:proj", 20.0)
        rc, _o, err = self.guard()
        self.assertEqual(rc, 2, err)
        self.assertIn("helm chat release gatelock:proj", err)
        self.assertNotIn("RENEWED", err)

    def test_kill_switch_restores_the_release_command(self):
        os.environ["HELM_STOP_GUARD_LEASE_TTL"] = "0"
        seats.claim("gatelock:proj", "alice", ttl=30, session=self.sid)
        self._expire_in("gatelock:proj", 27.0)
        rc, _o, err = self.guard()
        self.assertEqual(rc, 2, err)
        self.assertIn("helm chat release gatelock:proj", err)
        self.assertNotIn("RENEWED", err)

    def test_manually_claimed_gatelock_fails_safe_then_falls_through(self):
        # a MANUAL gatelock claim has no renewer: it reads as "renewed"
        # only while its remainder is still near the full TTL (the first
        # ~6s at 30/5), then falls through to the release command — the
        # conservative direction throughout, and DECLARED rather than
        # discovered (hc2's observation 1 in that review). The first phase now
        # ALLOWS the stop (an allowed stop cannot worsen a lease that
        # self-expires within one TTL); it still withholds the release
        # command, which is the half that kills suites.
        seats.claim("gatelock:proj", "alice", ttl=30, session=self.sid)
        self._expire_in("gatelock:proj", 25.0)
        rc, _o, err = self.guard()
        self.assertEqual(rc, 0, err)
        self.assertIn("RENEWED", err)          # fails safe: withholds
        self.assertNotIn("helm chat release", err)
        self._expire_in("gatelock:proj", 23.0)
        rc, _o, err = self.guard()
        self.assertEqual(rc, 2, err)
        self.assertIn("helm chat release gatelock:proj", err)

    def test_floor_tracks_the_renewers_constants_not_hardcoded_30_5(self):
        # the import-time fallback (30, 5) must never silently take over:
        # if gate._GATE_LEGACY_* moves or is renamed, THIS reddens instead
        # of the guard degrading on a stale floor (hc2's observation 2).
        from helm import gate
        self.assertEqual(float(gate._GATE_LEGACY_TTL_S), 30.0)
        self.assertEqual(float(gate._GATE_LEGACY_RENEW_S), 5.0)
        seats.claim("gatelock:proj", "alice", ttl=30, session=self.sid)
        floor = float(gate._GATE_LEGACY_TTL_S) - float(
            gate._GATE_LEGACY_RENEW_S) - 1.0
        with mock.patch.object(gate, "_GATE_LEGACY_TTL_S", 60.0), \
                mock.patch.object(gate, "_GATE_LEGACY_RENEW_S", 10.0):
            moved_floor = 60.0 - 10.0 - 1.0    # 49.0
            # below the MOVED floor but above the old one: the guard must
            # follow the constants, not the fallback
            self._expire_in("gatelock:proj", moved_floor - 2.0)
            rc, _o, err = self.guard()
            self.assertEqual(rc, 2, err)
            self.assertIn("helm chat release gatelock:proj", err)
            self.assertNotIn("RENEWED", err)
            self.assertNotEqual(floor, moved_floor)


class BeaconProcsTest(SeatsBase):
    """The DETECTION half of the armed-beacon gate, against a synthetic /proc
    tree — proof it reads the real process table (rearm.py's exact argv shape)
    rather than guessing from env. Ground-truthed live at build time against 10
    real beacons of the form `python3 .../helm chat wait --seat X --follow`."""

    LAUNCHER = 9001      # a live bash wrapper — what a healthy beacon parents to

    def proc(self, pid, argv, env=None, mode=None, ppid=None, comm="python3"):
        """A WHOLE /proc entry: cmdline, environ, stat and comm.

        stat/comm were added when strict liveness started asking who a beacon's
        LAUNCHER is. An entry with no stat is not a process anybody could
        observe, and leaving it out made every fixture beacon read as one whose
        parent helm could not identify."""
        root = os.path.join(self.tmp, "proc")
        d = os.path.join(root, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"\0".join((env or []) and
                               [e.encode() for e in env]) + b"\0")
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%d (%s) S %s 100\n"
                    % (pid, comm,
                       " ".join([str(self.LAUNCHER if ppid is None else ppid)]
                                + ["0"] * 17)))
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")
        if pid != self.LAUNCHER and not os.path.isdir(
                os.path.join(root, str(self.LAUNCHER))):
            self.proc(self.LAUNCHER, ["bash", "-c", "helm chat wait"],
                      ppid=1, comm="bash")
        if mode is not None:
            os.chmod(os.path.join(d, mode), 0)
        return root

    def test_seat_flag_attributes_the_waiter(self):
        root = self.proc(101, ["python3", "/x/bin/helm", "chat", "wait",
                               "--seat", "oi", "--follow"])
        self.assertEqual(seats.beacon_procs("oi", root), ([101], None))
        self.assertEqual(seats.beacon_procs("OI", root), ([101], None))  # fold
        self.assertEqual(seats.beacon_procs("other", root), ([], None))

    def test_bash_wrapper_alone_is_not_a_beacon(self):
        """rearm.py's exact-shape gate: the Monitor wrapper carries the text
        inside ONE -c argument with no standalone `helm` token. Matching it
        would report a beacon for a wrapper whose child already died."""
        root = self.proc(102, ["bash", "-c",
                               "helm chat wait --seat oi --follow"])
        self.assertEqual(seats.beacon_procs("oi", root), ([], None))

    def test_env_attributes_a_seatless_waiter(self):
        root = self.proc(103, ["python3", "/x/bin/helm", "chat", "wait",
                               "--follow"], env=["HELM_CHAT_NAME=oi", "A=b"])
        self.assertEqual(seats.beacon_procs("oi", root), ([103], None))
        self.assertEqual(seats.beacon_procs("kimi", root), ([], None))

    def test_unattributable_waiter_is_lenient(self):
        """A waiter with neither --seat nor a naming env MIGHT be ours: count
        it. Leniency is one-directional — a missed block only weakens the
        guard, a wrong block stops a healthy seat."""
        root = self.proc(104, ["python3", "/x/bin/helm", "chat", "wait"],
                         env=["A=b"])
        self.assertEqual(seats.beacon_procs("oi", root), ([104], None))

    def test_unattributable_waiter_is_UNKNOWN_in_strict_mode(self):
        root = self.proc(104, ["python3", "/x/bin/helm", "chat", "wait"],
                         env=["A=b"])
        pids, trouble = seats.beacon_procs("oi", root, strict=True)
        self.assertEqual(pids, [])
        self.assertIn("could not be attributed", trouble)

    def test_exact_waiter_wins_even_with_an_unattributable_sibling(self):
        """Attribution still beats an unattributable sibling — but STRICT now
        also needs a LIVE SESSION behind the shape, so the exact hit carries
        one. An argv shape says a `helm chat wait` process EXISTS; only the
        session says anything is still listening (see helm/beacons.py)."""
        from helm import beacons
        sid = "aaaaaaaa-1111-2222-3333-444444444444"
        root = self.proc(104, ["python3", "/x/bin/helm", "chat", "wait"],
                         env=["A=b"])
        root = self.proc(108, ["python3", "/x/bin/helm", "chat", "wait",
                               "--seat", "oi", "--follow"],
                         env=["CLAUDE_CODE_SESSION_ID=%s" % sid])
        with mock.patch.object(beacons, "live_sessions",
                               return_value={sid: 4242}):
            self.assertEqual(seats.beacon_procs("oi", root, strict=True),
                             ([108], None))

    def test_a_DEAD_sessions_waiter_never_satisfies_the_strict_claim(self):
        """The zombie that made resumeturn's recovery line a lie: a waiter
        whose session is proven dead is a proven ABSENCE of a wake path, not a
        beacon. The full ghost/deaf census lives in tests/test_beacons.py; this
        pins the seam right here, where the claim is made."""
        from helm import beacons
        sid = "aaaaaaaa-1111-2222-3333-444444444444"
        root = self.proc(108, ["python3", "/x/bin/helm", "chat", "wait",
                               "--seat", "oi", "--follow"],
                         env=["CLAUDE_CODE_SESSION_ID=%s" % sid])
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.object(beacons, "holder_from_records",
                                  return_value=(4242, 100)):
            self.assertEqual(seats.beacon_procs("oi", root, strict=True),
                             ([], None))

    def test_non_wait_helm_process_is_not_a_beacon(self):
        root = self.proc(105, ["python3", "/x/bin/helm", "chat", "stop-guard",
                               "--hook-json"])
        self.assertEqual(seats.beacon_procs("oi", root), ([], None))

    def test_unlistable_proc_is_trouble_not_absence(self):
        pids, trouble = seats.beacon_procs("oi", os.path.join(self.tmp, "nope"))
        self.assertEqual(pids, [])
        self.assertIn("unlistable", trouble)

    def test_not_dumpable_process_is_skipped_not_trouble(self):
        """The live-run bug: `systemd --user` is uid-1000 with an unreadable
        cmdline/environ. Treating that PermissionError as trouble disabled the
        guard on every real host — a guard that can never fire."""
        self.proc(106, ["python3", "/x/bin/helm", "chat", "wait",
                        "--seat", "oi", "--follow"])
        root = self.proc(107, ["systemd", "--user"], mode="cmdline")
        pids, trouble = seats.beacon_procs("oi", root)
        self.assertIsNone(trouble)          # opaque != broken
        self.assertEqual(pids, [106])       # the real beacon still found


class BeaconGateTest(SeatsBase):
    """GUARD 1 — a turn must not end with no armed beacon (the restarted-
    integrator incident: the seat's Monitor died with the old process, was
    never re-armed, and the seat ran BLIND for ~1.5h accumulating 98
    undelivered rows while missing a gate verdict it was waiting on).

    Hermetic: HELM_CHAT_NAME is the launch-seam stamp, beacon_procs is stubbed
    so no test ever depends on the host's real process table."""

    def guard(self, payload=None, args=(), pids=(), trouble=None):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        with mock.patch.object(seats, "beacon_procs",
                               return_value=(list(pids), trouble)):
            return self.cmd("stop-guard", ["--hook-json", *args],
                            stdin=stdin or b"{}")

    def seat_up(self, name="oi"):
        """A LAUNCHED fleet seat: the launch seam's HELM_CHAT_NAME plus a
        roster row from its SessionStart join."""
        os.environ["HELM_CHAT_NAME"] = name
        seats.join(seat=name, cwd="/tmp/p")

    # ── the block itself ──────────────────────────────────────────────────
    def test_first_turn_with_no_beacon_blocks_with_the_arm_instruction(self):
        self.seat_up()
        rc, _o, err = self.guard({"session_id": "s-b1"})
        self.assertEqual(rc, 2)
        self.assertIn("NO ARMED BEACON", err)
        self.assertIn("seat 'oi'", err)
        # the EXACT Monitor call, copy-pasteable
        self.assertIn('Monitor(command: "helm chat wait --seat oi --follow", '
                      'persistent: true)', err)
        self.assertIn("select:Monitor", err)      # …and the DEFERRED escape
        self.assertIn("98 undelivered", err)      # why, from the real incident

    def test_armed_beacon_does_not_block(self):
        self.seat_up()
        rc, _o, err = self.guard({"session_id": "s-b2"}, pids=[4242])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO ARMED BEACON", err)

    def test_block_is_once_per_episode_and_re_arms_on_a_later_loss(self):
        """Non-wedging by construction: the latch stores the OBSERVED state, so
        the transition into `missing` blocks exactly once. A seat that legit-
        imately cannot arm one is never trapped."""
        self.seat_up()
        self.assertEqual(self.guard({"session_id": "s-b3"})[0], 2)
        rc, _o, err = self.guard({"session_id": "s-b3"})
        self.assertEqual(rc, 0, err)                    # re-stop passes
        for _ in range(4):                              # …and keeps passing
            self.assertEqual(self.guard({"session_id": "s-b3"})[0], 0)
        # armed, then lost again -> the block RE-ARMS (one per episode)
        self.assertEqual(self.guard({"session_id": "s-b3"}, pids=[7])[0], 0)
        rc, _o, err = self.guard({"session_id": "s-b3"})
        self.assertEqual(rc, 2)
        self.assertIn("NO ARMED BEACON", err)

    def test_a_restart_gets_a_fresh_block(self):
        """The incident shape: the latch is per (seat, session), so the NEW
        session of a restarted seat blocks on its own first turn even though
        the old session had already been pointed at."""
        self.seat_up()
        self.assertEqual(self.guard({"session_id": "s-old"})[0], 2)
        self.assertEqual(self.guard({"session_id": "s-old"})[0], 0)
        rc, _o, err = self.guard({"session_id": "s-new"})
        self.assertEqual(rc, 2)
        self.assertIn("NO ARMED BEACON", err)

    # ── the three precision gates (a bad signal is worse than no guard) ────
    def test_an_unstamped_session_is_never_blocked(self):
        """No HELM_CHAT_NAME = not a launched fleet seat: an ad-hoc claude
        session in a helm project keeps today's advisory warn, never a block."""
        seats.join(seat="adhoc", cwd="/tmp/p")
        rc, _o, err = self.guard({"session_id": "s-b4"}, args=["--seat", "adhoc"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO ARMED BEACON", err)
        self.assertIn("inbox clean", err)             # the old warn still rides

    def test_a_stamp_for_a_different_seat_never_blocks(self):
        os.environ["HELM_CHAT_NAME"] = "someone-else"
        seats.join(seat="oi", cwd="/tmp/p")
        rc, _o, err = self.guard({"session_id": "s-b5"}, args=["--seat", "oi"])
        self.assertEqual(rc, 0, err)

    def test_an_unregistered_seat_never_blocks(self):
        """No roster row = the join never ran = no delivery lane to go dark."""
        os.environ["HELM_CHAT_NAME"] = "ghost"
        rc, _o, err = self.guard({"session_id": "s-b6"}, args=["--seat", "ghost"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO ARMED BEACON", err)

    def test_probe_trouble_never_blocks(self):
        """Absence must be PROVEN: an unreadable process table means liveness
        is UNKNOWN, not absent."""
        self.seat_up()
        rc, _o, err = self.guard({"session_id": "s-b7"},
                                 trouble="process table unlistable (EPERM)")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO ARMED BEACON", err)

    def test_kill_switch_disables_the_gate(self):
        self.seat_up()
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        try:
            rc, _o, err = self.guard({"session_id": "s-b8"})
        finally:
            os.environ.pop("HELM_STOP_GUARD_BEACON")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO ARMED BEACON", err)
        self.assertEqual(self.guard({"session_id": "s-b8"})[0], 2)  # back on

    def test_global_kill_switch_covers_it_too(self):
        self.seat_up()
        os.environ["HELM_STOP_GUARD"] = "0"
        try:
            rc, _o, err = self.guard({"session_id": "s-b9"})
        finally:
            os.environ.pop("HELM_STOP_GUARD")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_stop_hook_active_short_circuits_the_gate(self):
        self.seat_up()
        rc, _o, err = self.guard({"session_id": "s-b10",
                                  "stop_hook_active": True})
        self.assertEqual(rc, 0, err)

    def test_a_raising_detector_never_wedges_the_stop(self):
        self.seat_up()
        stdin = json.dumps({"session_id": "s-b11"}).encode()
        with mock.patch.object(seats, "beacon_procs",
                               side_effect=RuntimeError("boom")):
            rc, _o, err = self.cmd("stop-guard", ["--hook-json"], stdin=stdin)
        self.assertEqual(rc, 0, err)

    def test_the_beacon_block_rides_the_same_exit_2_as_the_inbox_block(self):
        """Arbiter shape: ALL blockers surface in ONE exit-2 (fix everything in
        one shot), never one gate at a time."""
        self.seat_up()
        chat.post("@oi look at this", who="bob")
        rc, _o, err = self.guard({"session_id": "s-b12"})
        self.assertEqual(rc, 2)
        self.assertIn("undelivered", err)
        self.assertIn("NO ARMED BEACON", err)


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

    def test_dirty_streak_names_the_check_and_never_claims_green(self):
        """THE SIBLING OF THE UNBANKED-GREEN RUNG, and it was worse. That rung
        verifies every latest gate is green before saying so; THIS one never
        reads `latest` at all and still said "bank the GREEN slice". It also
        counts dirtying OPS, not work — a mutation-test cp/sed loop runs the
        counter up without producing anything worth committing.

        Fixed together because per-rung correctness guarantees nothing when two
        rungs jointly cover one decision (watchdogs-correct-composition-holed):
        the shared-checkout claim was repaired in one and left asserting itself
        two screens down, and this rung then fired at the integrator on a tree
        holding another seat's work with everything of its own already pushed."""
        seats.join(session="s-w3", seat="wisp", cwd="/tmp/p")
        self.plant("s-w3", **{"dirty-streak": 8})
        rc, _o, err = self.guard({"session_id": "s-w3"})
        self.assertEqual(rc, 2)
        self.assertIn("8 tool calls with the tree DIRTY", err)
        # NOT "8 dirtying ops". record.py bumps dirty-streak on EVERY tool call
        # while last-dirty is true — reads and greps included — so the old
        # wording named a quantity the counter never measured. Measured
        # 2026-07-27: 41 against exactly 2 dirty paths, both another seat's.
        self.assertNotIn("dirtying ops", err)
        self.assertNotIn("green slice", err)        # NO gate was checked here
        # cwd never reaches the guard here, so the venue probe is BLIND — the
        # rung says UNKNOWN now instead of laundering the blindness as SHARED
        # (the venue-honesty fix; the measured venues have their own tests in
        # StopWhisperStagedDeletionTest).
        self.assertIn("UNKNOWN", err)
        self.assertNotIn("SHARED", err)
        self.assertIn("git diff --stat HEAD", err)  # the check, not an imperative
        self.assertIn("NOT yours", err)
        self.assertIn("stash create", err)

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
        chat.post("@wisp land the fix", who="owner")
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
        # keyed on "with the tree DIRTY", which ONLY the dirty rung emits — the
        # original key was "bank the green", a phrase the unbanked-green rung
        # also carried, so this ordering assertion could have been satisfied by
        # the wrong rung.
        self.assertNotIn("with the tree DIRTY", err)
        # next stop: stuck latched, dirty (still live) takes the slot
        rc, _o, err = self.guard({"session_id": "s-w5"})
        self.assertEqual(rc, 2)
        self.assertIn("with the tree DIRTY", err)

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
        self.assertIn("add -u", err)
        # It must NOT recommend `add -A`. The dirty signal counts UNTRACKED
        # files and the fleet shares one checkout, so this rung fires on files
        # another seat wrote; `-A` would stage them. Live 2026-07-26: it fired
        # at the integrator over a lane's untracked audit doc that was out for
        # review, and `-A` would have committed it to main past its own gate.
        self.assertNotIn("add -A &&", err)
        # AND IT MUST NOT CLAIM THE DIRT IS THIS SEAT'S. `-u` was believed to be
        # the safe form because untracked files were the 2026-07-26 danger. Live
        # 2026-07-27 proved that reasoning incomplete: another seat staged lane
        # content into the shared tree, REVERTING three pushed commits, and those
        # were TRACKED modifications — `-u` stages them exactly as `-A` would.
        # This rung fired telling the integrator to bank them as "the proven
        # slice." So it may not print an unconditional imperative: it must name
        # the read-the-diff check, and the non-destructive move for dirt that
        # turns out not to be yours.
        self.assertNotIn("bank the proven slice", err)
        # no cwd reaches the guard: the venue probe is BLIND, and the rung
        # says UNKNOWN rather than asserting SHARED from blindness
        self.assertIn("UNKNOWN", err)
        self.assertNotIn("SHARED", err)
        self.assertIn("NOT yours", err)
        self.assertIn("stash", err)               # the free, non-destructive out
        self.assertIn("never commit", err)
        rc, _o, err = self.guard("s-b1")
        self.assertEqual(rc, 0, err)                 # latched per green state
        self.plant_runs("s-b1", {"token": "pytest", "exit": 0, "digest": "hhh"},
                        mode="a")                    # a NEW proven-green re-arms
        self.assertEqual(self.guard("s-b1")[0], 2)

    def test_the_whisper_never_clips_away_its_own_instruction(self):
        """The 240-byte cap is real, the gate TOKEN is caller-supplied, and _clip
        truncates the TAIL — so whatever sits last is what overflow eats. The first
        draft put the advice last and shipped as `bank it with \\`git ad…`: the
        reader got the alarm and not the repair.

        WHAT ACTUALLY PROTECTS IT IS ORDER, not the token clip. Measured: at a
        40-char token this body reaches 247 bytes and loses 7, which fall in the
        trailing boilerplate. I first wrote this test believing the clip was
        load-bearing, and the mutation restoring clip 40 stayed GREEN — so the
        assertion below pins the invariant that holds (instruction present, and
        positioned ahead of the once-per-state note) rather than the knob I
        happened to turn."""
        seats.join(session="s-b1c", seat="wisp", cwd="/tmp/p")
        self.plant("s-b1c", **{"last-dirty": 1})
        self.plant_edits("s-b1c", "seats.py")
        self.plant_runs("s-b1c", {"token": "pytest " + "tests/test_x.py " * 6,
                                  "exit": 0, "digest": "iii"})
        rc, _o, err = self.guard("s-b1c")
        self.assertEqual(rc, 2)
        self.assertLessEqual(len(err.strip()), 260)   # frame + cap, still bounded
        # UNKNOWN, not SHARED: this fixture's guard never receives a cwd, so
        # the venue probe is blind and the honest venue word is UNKNOWN
        for must in ("UNKNOWN", "git diff --stat HEAD", "NOT yours",
                     "never commit"):
            self.assertIn(must, err, "clipped away: %s" % must)
        # THE INVARIANT: the advice precedes the boilerplate, so overflow can only
        # ever eat the note. Assert the ORDER, because that is what survives a
        # future re-wording — an index check cannot be satisfied by accident.
        self.assertLess(err.index("never commit"), err.index("holds once per state"))

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


class StopWhisperStagedDeletionTest(SeatsBase):
    """The staged-deletion refusal (live 2026-07-29): eight owner-gated doc
    deletions sat STAGED in the shared checkout while the unbanked-green
    whisper said `yours -> add -u` — following it would have committed ~1648
    lines of landed documentation. While any staged deletion exists in a
    SHARED checkout the guard must not offer `add -u` (or any bulk bank verb)
    at all; it names the paths at stake instead, bounded, with an explicit
    omitted count. UNKNOWN (no cwd / not a repo / git failed) keeps the legacy
    wording — the pre-existing exposure, never a NEW verdict; a lane worktree
    (non-shared) is untouched. Fixtures are real git repos in tmp — the probe
    under test IS a git read."""

    def guard(self, sid, cwd=None):
        payload = {"session_id": sid}
        if cwd is not None:
            payload["cwd"] = cwd
        return self.cmd("stop-guard", ["--hook-json"],
                        stdin=json.dumps(payload).encode())

    def plant(self, sid, **counters):
        pk.write_json(os.path.join(record.session_dir(sid), "counters.json"),
                      counters)

    def plant_runs(self, sid, *rows):
        p = os.path.join(record.session_dir(sid), "command-log.jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            for i, r in enumerate(rows):
                f.write(json.dumps({"ts": int(time.time()) + i, **r}) + "\n")

    def plant_edits(self, sid, *names):
        p = os.path.join(record.session_dir(sid), "edit-targets.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("".join(n + "\n" for n in names))

    def arm_unbanked(self, sid, token="pytest"):
        """last-dirty + a code edit + a green run: the unbanked rung's state."""
        self.plant(sid, **{"last-dirty": 1})
        self.plant_edits(sid, "seats.py")
        self.plant_runs(sid, {"token": token, "exit": 0, "digest": "ggg"})

    def git(self, d, *args):
        r = subprocess.run(["git", "-C", d, "-c", "user.email=t@t",
                            "-c", "user.name=t", *args],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def repo(self, *files):
        """A MAIN (shared-shaped) checkout: .git is the common dir itself."""
        d = tempfile.mkdtemp(prefix="shared-", dir=self.tmp)
        self.git(d, "init", "-q")
        for p in files:
            fp = os.path.join(d, p)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w") as f:
                f.write("body of %s\n" % p)
        self.git(d, "add", "-A")
        self.git(d, "commit", "-q", "-m", "seed")
        return d

    def whisper_line(self, err):
        return next(l for l in err.splitlines() if "[helm stop-whisper]" in l)

    def test_staged_deletion_suppresses_add_u_and_names_the_path(self):
        repo = self.repo("docs/gone.md", "docs/keep.md")
        self.git(repo, "rm", "-q", "docs/gone.md")
        seats.join(session="s-sd1", seat="wisp", cwd=repo)
        self.arm_unbanked("s-sd1")
        rc, _o, err = self.guard("s-sd1", cwd=repo)
        self.assertEqual(rc, 2, err)
        line = self.whisper_line(err)
        # the destructive branch is NOT OFFERED — not even mentioned
        self.assertNotIn("add -u", line)
        self.assertNotIn("add -A", line)
        # the reader gets the real information instead: the path at stake
        self.assertIn("docs/gone.md", line)
        self.assertIn("STAGED deletion", line)
        self.assertIn("SHARED", line)
        self.assertIn("do NOT stage/commit", line)
        # latch: same green state + same deletion set — a re-stop passes
        rc, _o, err = self.guard("s-sd1", cwd=repo)
        self.assertEqual(rc, 0, err)

    def test_deletion_resolved_rearms_the_legacy_wording(self):
        """The fp carries the deletion set: once the scrub lands/unwinds, the
        normal unbanked advice (a DIFFERENT fp) still gets its one shot."""
        repo = self.repo("docs/gone.md", "docs/keep.md")
        self.git(repo, "rm", "-q", "docs/gone.md")
        seats.join(session="s-sd2", seat="wisp", cwd=repo)
        self.arm_unbanked("s-sd2")
        self.assertEqual(self.guard("s-sd2", cwd=repo)[0], 2)   # fires + latches
        self.git(repo, "reset", "-q")                # unstage the deletion
        self.git(repo, "checkout", "-q", "--", ".")  # restore the file
        with open(os.path.join(repo, "docs/keep.md"), "a") as f:
            f.write("still dirty\n")                 # tree stays dirty
        rc, _o, err = self.guard("s-sd2", cwd=repo)
        self.assertEqual(rc, 2, err)
        self.assertIn("add -u", self.whisper_line(err))   # legacy branch, once

    def test_no_staged_deletion_keeps_the_legacy_wording_byte_identical(self):
        repo = self.repo("docs/keep.md")
        with open(os.path.join(repo, "docs/keep.md"), "a") as f:
            f.write("dirty\n")                       # tracked mod, nothing staged
        seats.join(session="s-sd3", seat="wisp", cwd=repo)
        self.arm_unbanked("s-sd3")
        rc, _o, err = self.guard("s-sd3", cwd=repo)
        self.assertEqual(rc, 2, err)
        # THE PIN'S PURPOSE IS UNCHANGED: this path keeps the PLAIN dirty
        # wording and inherits NONE of the deletion-specific text. Only the
        # literal moved, and a byte pin must admit intentional evolution while
        # still forbidding drift — so the literal is updated, never loosened.
        # WHY IT MOVED (2026-08-05): a THIRD state was added — "a live
        # DELEGATE's -> LEAVE IT". yours/not-yours was exhaustive only while
        # every agent edited in a claimed lane; a Task subagent in the shared
        # checkout holds no lane and no claim, so it is invisible to the
        # lane-scoped delegation machinery BY DESIGN. This whisper fired three
        # times at an integrator whose subagent held the two dirty files and
        # BOTH prescribed branches were wrong (`add -u` commits a delegate's
        # half-finished edit; `stash create` steals a file from a live writer).
        # NOTE THE TRAILING `…`, IT IS NOT A TYPO AND NOT A HAND-TRIM: the
        # third state costs 31 bytes, so the composed line reaches 247 and
        # _clip takes the last 7 at STOP_WHISPER_CAP=240 — even at this short
        # `pytest` token. What it eats is the "a re-stop passes." boilerplate,
        # never the advice, which is exactly the ordering law the sibling test
        # test_the_whisper_never_clips_away_its_own_instruction asserts by
        # index (line ~4548, and it stayed green through this). If a future
        # re-wording makes this pin lose `never commit` instead, the advice has
        # started paying for the note and the ORDER is what broke.
        self.assertEqual(
            self.whisper_line(err),
            "[helm stop-whisper] gate GREEN (`pytest`) + dirty, but this "
            "checkout is SHARED — read `git diff --stat HEAD`: yours -> "
            "add -u; a live DELEGATE's -> LEAVE IT; NOT yours -> stash "
            "create, never commit This holds once per state — a re-stop …")

    def test_many_deletions_bounded_with_an_explicit_omitted_count(self):
        repo = self.repo(*["docs/d%d.md" % i for i in range(9)])
        for i in range(9):
            self.git(repo, "rm", "-q", "docs/d%d.md" % i)
        seats.join(session="s-sd4", seat="wisp", cwd=repo)
        self.arm_unbanked("s-sd4")
        rc, _o, err = self.guard("s-sd4", cwd=repo)
        self.assertEqual(rc, 2, err)
        line = self.whisper_line(err)
        self.assertLessEqual(len(line.encode()), seats.STOP_WHISPER_CAP + 3)
        self.assertNotIn("add -u", line)
        self.assertIn("9 STAGED deletion", line)     # the full count
        self.assertIn("docs/d0.md", line)            # the first named path
        self.assertIn("+7 more", line)               # truncation NAMES itself
        self.assertIn("do NOT stage/commit", line)   # the advice survived

    def test_worst_case_paths_and_token_never_clip_the_advice(self):
        """MEASURED bound (the unbanked comment's own law): with the token
        clipped to 12 and each named path to 20 bytes, the body peaks ~237
        bytes — count text, '+N more' and the prohibition all inside the cap.
        Overflow may only ever eat the trailing once-per-state note."""
        names = ["docs/%s%d.md" % ("x" * 60, i) for i in range(3)]
        repo = self.repo(*names)
        for n in names:
            self.git(repo, "rm", "-q", n)
        seats.join(session="s-sd5", seat="wisp", cwd=repo)
        self.plant("s-sd5", **{"last-dirty": 1})
        self.plant_edits("s-sd5", "seats.py")
        self.plant_runs("s-sd5", {"token": "pytest " + "tests/test_x.py " * 6,
                                  "exit": 0, "digest": "www"})
        rc, _o, err = self.guard("s-sd5", cwd=repo)
        self.assertEqual(rc, 2, err)
        line = self.whisper_line(err)
        self.assertLessEqual(len(line.encode()), seats.STOP_WHISPER_CAP + 3)
        self.assertIn("docs/" + "x" * 15, line)      # first path, clipped visible
        self.assertIn("+1 more", line)
        self.assertIn("do NOT stage/commit", line)
        self.assertIn("act per-path", line)
        self.assertLess(line.index("+1 more"), line.index("do NOT"))

    def test_a_measured_lane_room_is_called_a_lane_not_shared(self):
        """The probe MEASURED this tree non-shared, and the old fallback still
        said 'this checkout is SHARED' — a whisper asserting a state its own
        probe had just refuted. A lane owns its index, so the advice drops the
        whose-dirt question entirely: bank it in your room."""
        main = self.repo("docs/gone.md")
        wt = os.path.join(self.tmp, "lane-wt")
        self.git(main, "worktree", "add", "-q", wt, "-b", "lane/x")
        self.git(wt, "rm", "-q", "docs/gone.md")     # staged in the LANE tree
        seats.join(session="s-sd6", seat="wisp", cwd=wt)
        self.arm_unbanked("s-sd6")
        rc, _o, err = self.guard("s-sd6", cwd=wt)
        self.assertEqual(rc, 2, err)
        line = self.whisper_line(err)
        self.assertIn("add -u", line)                # a lane owns its own index
        self.assertIn("LANE room", line)
        self.assertIn("commit in your room", line)
        self.assertNotIn("SHARED", line)
        self.assertNotIn("STAGED deletion", line)

    def test_unknown_probe_says_unknown_never_a_venue(self):
        """No cwd in the payload / not a repo: the probe returns UNKNOWN. The
        old wording still asserted 'this checkout is SHARED' — a verdict
        minted from blindness. The honest form keeps the read-then-decide
        advice (exactly right when the venue is unknowable) and says UNKNOWN
        where the venue word stood."""
        seats.join(session="s-sd7", seat="wisp", cwd="/tmp/p")
        self.arm_unbanked("s-sd7")
        rc, _o, err = self.guard("s-sd7")            # no cwd at all
        self.assertEqual(rc, 2, err)
        line = self.whisper_line(err)
        self.assertIn("add -u", line)
        self.assertIn("UNKNOWN", line)
        self.assertNotIn("SHARED", line)
        notrepo = tempfile.mkdtemp(prefix="plain-", dir=self.tmp)
        seats.join(session="s-sd8", seat="wisp2", cwd=notrepo)
        self.arm_unbanked("s-sd8")
        rc, _o, err = self.guard("s-sd8", cwd=notrepo)
        self.assertEqual(rc, 2, err)
        line = self.whisper_line(err)
        self.assertIn("add -u", line)
        self.assertIn("UNKNOWN", line)
        self.assertNotIn("SHARED", line)

    def test_dirty_streak_rung_refuses_the_commit_verb_too(self):
        """The sibling-rung law: `yours -> commit` banks staged deletions
        exactly as `add -u` would — they are ALREADY staged; a bare commit
        ships them. Fixing one rung and leaving the sibling asserting the
        same thing is the watchdogs-correct-composition-holed failure."""
        repo = self.repo("docs/gone.md", "docs/keep.md")
        self.git(repo, "rm", "-q", "docs/gone.md")
        seats.join(session="s-sd9", seat="wisp", cwd=repo)
        self.plant("s-sd9", **{"dirty-streak": 8, "last-dirty": 1})
        rc, _o, err = self.guard("s-sd9", cwd=repo)
        self.assertEqual(rc, 2, err)
        line = self.whisper_line(err)
        self.assertNotIn("yours -> commit", line)
        self.assertNotIn("add -u", line)
        self.assertIn("docs/gone.md", line)
        self.assertIn("do NOT stage/commit", line)

    def test_dirty_streak_rung_unchanged_without_deletions(self):
        repo = self.repo("docs/keep.md")
        with open(os.path.join(repo, "docs/keep.md"), "a") as f:
            f.write("dirty\n")
        seats.join(session="s-sd10", seat="wisp", cwd=repo)
        self.plant("s-sd10", **{"dirty-streak": 8, "last-dirty": 1})
        rc, _o, err = self.guard("s-sd10", cwd=repo)
        self.assertEqual(rc, 2, err)
        line = self.whisper_line(err)
        self.assertIn("yours -> commit", line)
        self.assertIn("stash create", line)
        self.assertIn("SHARED", line)     # a MAIN checkout — measured, not guessed

    def test_dirty_streak_rung_calls_a_lane_a_lane(self):
        """The sibling of the sibling law: the dirty-streak rung said 'SHARED
        checkout' on a tree its own probe had measured as a lane worktree.
        Same venue honesty as the unbanked arm: LANE names itself, the advice
        is commit-in-your-room, and the whose-dirt fork disappears."""
        main = self.repo("docs/keep.md")
        wt = os.path.join(self.tmp, "lane-dirty-wt")
        self.git(main, "worktree", "add", "-q", wt, "-b", "lane/d")
        with open(os.path.join(wt, "docs/keep.md"), "a") as f:
            f.write("lane dirt\n")
        seats.join(session="s-sd11", seat="wisp", cwd=wt)
        self.plant("s-sd11", **{"dirty-streak": 8, "last-dirty": 1})
        rc, _o, err = self.guard("s-sd11", cwd=wt)
        self.assertEqual(rc, 2, err)
        line = self.whisper_line(err)
        self.assertIn("LANE room", line)
        self.assertIn("commit in your room", line)
        self.assertNotIn("SHARED", line)

    def test_dirty_streak_rung_says_unknown_on_a_blind_probe(self):
        notrepo = tempfile.mkdtemp(prefix="plain-", dir=self.tmp)
        seats.join(session="s-sd12", seat="wisp", cwd=notrepo)
        self.plant("s-sd12", **{"dirty-streak": 8, "last-dirty": 1})
        rc, _o, err = self.guard("s-sd12", cwd=notrepo)
        self.assertEqual(rc, 2, err)
        line = self.whisper_line(err)
        self.assertIn("UNKNOWN", line)
        self.assertIn("yours -> commit", line)   # read-then-decide survives
        self.assertNotIn("SHARED", line)


class WorkOfferTest(SeatsBase):
    """AX primitive #1 — the fleet self-saturation rung (bottom of the ladder):
    a GENUINELY idle seat is offered ONE terse take-it-or-pass for the top
    UNOWNED backlog row. Hermetic: the dispatch ledger is planted as raw
    event-sourced rows (no git), claims/roster in tmp. Offerable = an OBSERVED,
    not-overdue open dispatch whose recipient is this seat or an absent/gone
    seat (never a different LIVE seat), and whose `dispatch:<id8>` claim key is
    free."""

    def guard(self, payload=None, args=()):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        return self.cmd("stop-guard", ["--hook-json", *args], stdin=stdin or b"{}")

    def git(self, repo, *args):
        p = subprocess.run(["git", "-C", repo, "-c", "user.email=t@t",
                            "-c", "user.name=t", *args], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def repo(self):
        repo = tempfile.mkdtemp(prefix="offer-repo-", dir=self.tmp)
        self.git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, "seed"), "w") as f:
            f.write("seed\n")
        self.git(repo, "add", "seed")
        self.git(repo, "commit", "-q", "-m", "seed")
        return repo

    def plant_dispatch(self, rid, recipient, lane="review the canary",
                       ts=None, observed=True, repo_id=None, kind=None,
                       tip=None, ref=None, reviewed_tip=None):
        """One OPEN dispatch (+ a delivered event unless observed=False) written
        straight to the ledger — an observed, not-overdue row that dispatches.
        stop_candidate() ignores (so the dispatch RUNG stays quiet and the seat
        reads idle), but open_rows() surfaces as offerable backlog. `kind`
        rides the v3 row verbatim (dict(row) passthrough in _new_state), so
        the actuator tests can plant build/review/junk/unrecorded kinds."""
        from helm import dispatches
        ts = ts or pk.now_ts()
        rows = [{"v": 3, "event": "dispatch", "seq": 0, "id": rid, "ts": ts,
                 "recipient": recipient, "lane": lane, "tip": tip or "a" * 40,
                 "ref": ref or "abc", "deadline_s": 2700, "status": "open"}]
        if reviewed_tip is not None:
            rows[0]["reviewed_tip"] = reviewed_tip
        if repo_id is not None:
            rows[0]["repo_id"] = repo_id
        if kind is not None:
            rows[0]["kind"] = kind
        if observed:
            rows.append({"v": 3, "event": "delivered", "seq": 1, "id": rid,
                         "ts": ts, "delivery_ref": "dm-x"})
        p = dispatches.ledger_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_idle_seat_offered_top_backlog_with_claim_cmd(self):
        seats.join(session="s-o1", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("a1b2c3d4e5f60011", "ghost")   # recipient absent
        rc, _o, err = self.guard({"session_id": "s-o1"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2, err)
        self.assertIn("[helm stop-whisper]", err)
        self.assertIn("you're free", err)
        self.assertIn("top of backlog is a1b2c3d4", err)   # short id, glanceable
        self.assertIn("review the canary", err)            # the one plain line
        self.assertIn("helm chat claim dispatch:a1b2c3d4", err)   # exact claim cmd
        self.assertIn("or pass", err)
        # latch: same backlog HEAD, a re-stop passes — never spam every stop
        rc, _o, err = self.guard({"session_id": "s-o1"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stop-whisper", err)

    def test_paused_live_waiter_keeps_ownership_when_seen_ages_out(self):
        seats.join(session="s-offer", seat="ds4pro", cwd="/tmp/p")
        seats.join(session="s-wall", seat="codex", cwd="/tmp/p")
        old = time.time() - seats.QUIET_S - 60
        os.utime(seats.seen_path("codex"), (old, old))
        pk.write_json(proxywatch._state_path(), {"ts": time.time(),
                      "upstream": {"codex": {"state": "AUTH-401",
                                               "dark": True}}})
        self.plant_dispatch("ab12cd34ef560011", "codex")
        with mock.patch.object(seats, "beacon_procs",
                               return_value=([4321], None)):
            rows = seats._offer_rows("ds4pro")
        self.assertEqual(rows, [],
                         "a paused but armed owner was misread as stranded")
        with mock.patch.object(seats, "beacon_procs", return_value=([], None)):
            rows = seats._offer_rows("ds4pro")
        self.assertEqual([r[0] for r in rows], ["ab12cd34"],
                         "a proven-dead waiter must not own work forever")

    # ---- task/290: the ledger becomes the rung's SECOND producer ----

    def _file_task(self, title, owner=None, status="open"):
        """One row in the FIXTURE ledger — HELM_HOME is redirected by SeatsBase,
        so tasks.ledger_path() lands in tmp and no live row is ever read."""
        from helm import tasks
        row, err = tasks.add(title, owner, status=status)
        self.assertIsNone(err, err)
        return row

    def _proj(self, cwd_project):
        """_git_project for both sides of the scope check: the SEAT's cwd (tmp)
        answers `cwd_project`, a foreign repo path answers its own name, and
        anything else — notably helm's own package dir, which is what
        _ledger_project() passes — answers "helm"."""
        def f(p):
            p = str(p or "")
            if p.startswith(self.tmp):
                return cwd_project
            if "some-other" in p:
                return "some-other-repo"
            return "helm"
        return f

    def test_the_task_ledger_REACHES_an_idle_seat(self):
        """THE ADOPTION GAP, and the whole point of task/290. This rung had one
        producer — the dispatch ledger — and a dispatch row is tip-bound by
        construction, so an un-started plan could never reach the one surface
        whose job is handing work to an idle seat. Measured after the ledger
        landed and before this wire: 15 rows filed by 2 seats, both of them the
        authors. The pool reached nobody."""
        seats.join(session="s-tk1", seat="ds4pro", cwd="/tmp/p")
        free = self._file_task("nobody holds this one")
        with mock.patch.object(seats, "_git_project",
                               side_effect=self._proj("helm")):
            rows = seats._offer_rows("ds4pro")
        tasks_offered = [r for r in rows if r[4] == "task"]
        self.assertEqual([r[5]["id"] for r in tasks_offered], [free["id"]])
        short = free["id"].split("/", 1)[-1]
        self.assertEqual(tasks_offered[0][2], "helm task claim " + short)
        self.assertIn("nobody holds this one", tasks_offered[0][1])
        self.assertFalse(tasks_offered[0][3])       # pool is never auto-claim

    def test_a_FOREIGN_project_seat_gets_no_task_rows_but_keeps_its_own(self):
        """The task ledger is GLOBAL and its rows carry NO project, so the
        dispatch rung's rule — only a PROVEN mismatch excludes — cannot be
        ported: every row would read legacy-unknown and the filter would be a
        no-op that LOOKS like a guard. The proof runs once and must be
        POSITIVE. The dispatch half is the must-hit: over-excluding silences
        the rung, and a silent rung is indistinguishable from an empty
        backlog."""
        seats.join(session="s-tk2", seat="ds4pro", cwd="/tmp/p")
        self._file_task("a helm row a foreign seat must not be offered")
        self.plant_dispatch("aa11bb22cc33dd44", "ghost",
                            repo_id="/tmp/estate/some-other-repo/.git")
        with mock.patch.object(seats, "_git_project",
                               side_effect=self._proj("some-other-repo")):
            rows = seats._offer_rows("ds4pro")
        self.assertEqual([r[0] for r in rows], ["aa11bb22"])   # MUST-HIT first
        self.assertEqual([r for r in rows if r[4] == "task"], [])

    def test_an_UNKNOWN_project_offers_no_task_rows_and_never_silences_dispatch(self):
        """Opposite fail-direction from the dispatch rows above, and on purpose.
        A dispatch row with no repo_id stays offerable because only a proven
        mismatch excludes THERE. Here the ledger's ownership cannot be proven
        against an unreadable seat project, and the failure being prevented is
        the owner's own report — a seat in another project offered a HELM item
        and spending a turn declining it. So task rows need POSITIVE proof and
        an unknown offers none, while the dispatch producer is untouched."""
        seats.join(session="s-tk3", seat="ds4pro", cwd="/tmp/p")
        self._file_task("a helm row with nothing to prove scope against")
        self.plant_dispatch("bb22cc33dd44ee55", "ghost")        # no repo_id
        with mock.patch.object(seats, "_git_project", return_value=None):
            rows = seats._offer_rows("ds4pro")
        self.assertEqual([r[0] for r in rows], ["bb22cc33"])   # MUST-HIT first
        self.assertEqual([r for r in rows if r[4] == "task"], [])

    def test_a_LIVE_task_owners_row_is_not_offered_through_the_rung(self):
        """The liveness pass-through, asserted AT THE SEAM rather than only in
        tasks.offer_rows' own suite. Measured on the live ledger before the
        cure: 110 rows offered, 69 owned by a seat live at that instant. If
        this call ever stops passing `live`, that returns — and it returns
        here, not in the producer's tests."""
        seats.join(session="s-tk4", seat="ds4pro", cwd="/tmp/p")
        seats.join(session="s-tk4b", seat="codex", cwd="/tmp/p")
        busy = self._file_task("codex is building this", "codex")
        gone = self._file_task("a departed seat still holds this", "ghost-seat")
        with mock.patch.object(seats, "_git_project",
                               side_effect=self._proj("helm")):
            rows = seats._offer_rows("ds4pro")
        offered = [r[5]["id"] for r in rows if r[4] == "task"]
        self.assertEqual(offered, [gone["id"]])                # MUST-HIT + absence
        self.assertNotIn(busy["id"], offered)
        line = [r[1] for r in rows if r[4] == "task"][0]
        self.assertIn("[assigned: ghost-seat]", line)          # rescue says whose

    def test_a_BROKEN_task_producer_never_silences_the_dispatch_offers(self):
        """Two producers, one list: the second must not be able to take the
        first down. The dispatch backlog is the rung's original and load-bearing
        source, and a ledger that cannot be read is not a reason for an idle
        seat to be told there is no work."""
        seats.join(session="s-tk5", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("cc33dd44ee55ff66", "ghost")
        from helm import tasks
        with mock.patch.object(seats, "_git_project",
                               side_effect=self._proj("helm")), \
             mock.patch.object(tasks, "offer_rows",
                               side_effect=RuntimeError("ledger on fire")):
            rows = seats._offer_rows("ds4pro")
        self.assertEqual([r[0] for r in rows], ["cc33dd44"])

    def test_another_repos_backlog_is_NOT_offered(self):
        """The owner's catch, 2026-07-29 (paraphrased with the project name
        generalised — tests/ is tracked and real incident cites carry private
        names; the guard that flagged this is right): a seat standing in
        ANOTHER project's checkout was offered a HELM backlog item and spent
        a turn declining it — "why does the other-project cwd agent get
        helm-related stophooks". Every row carries repo_id and this rung
        never asked."""
        seats.join(session="s-rp1", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("bb11cc22dd33ee44", "ghost",
                            repo_id="/tmp/estate/some-other-repo/.git")
        with mock.patch.object(seats, "_git_project",
                               side_effect=lambda p: ("helm" if p and "some-other" not in str(p)
                                                      else "some-other-repo")):
            rows = seats._offer_rows("ds4pro")
        self.assertEqual(rows, [], "a foreign repo's backlog reached this seat")

    def test_MY_repos_backlog_still_reaches_a_free_seat(self):
        """The other direction, and the one that matters more: a scoping
        filter that over-excludes silences the rung, and a silent rung is
        indistinguishable from an empty backlog. Same-project work MUST still
        find an idle seat — that is this rung's entire purpose."""
        seats.join(session="s-rp2", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("cc11dd22ee33ff44", "ghost",
                            repo_id="/tmp/estate/helm/.git")
        with mock.patch.object(seats, "_git_project", return_value="helm"):
            rows = seats._offer_rows("ds4pro")
        self.assertEqual([r[0] for r in rows], ["cc11dd22"])

    def test_an_UNKNOWN_repo_on_either_side_stays_offerable(self):
        """Fail OPEN, deliberately: a legacy row with no repo_id, or a seat
        whose cwd names no project, must not be filtered into silence — only
        a PROVEN mismatch excludes."""
        seats.join(session="s-rp3", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("dd11ee22ff334455", "ghost")          # no repo_id
        with mock.patch.object(seats, "_git_project", return_value=None):
            rows = seats._offer_rows("ds4pro")                     # no project
        self.assertEqual([r[0] for r in rows], ["dd11ee22"])

    def test_an_assigned_row_is_offered_but_NAMES_its_recipient(self):
        """Three seats in one afternoon each spent a turn discovering that
        ab0803ad belonged to grok. The row IS offerable — an absent
        recipient's work is the stranded case this rung rescues — but
        presenting it as plain "top of backlog" costs a turn where naming the
        recipient costs a glance. Measured root cause worth keeping visible:
        grok's process was ALIVE while its presence beat was 25h stale, so
        the liveness test called its work strandable."""
        seats.join(session="s-as1", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("ee11ff2233445566", "grok")   # named, not live
        rows = seats._offer_rows("ds4pro")
        self.assertEqual(len(rows), 1)
        self.assertIn("[assigned: grok]", rows[0][1])

    def test_my_own_assigned_row_is_NOT_labelled_as_someone_elses(self):
        """The label must never appear on the seat's OWN work — that would
        read as a takeover of itself."""
        seats.join(session="s-as2", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("ff1122334455aabb", "ds4pro")
        rows = seats._offer_rows("ds4pro")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("[assigned:", rows[0][1])

    def test_dispatch_assigned_to_this_seat_is_offerable(self):
        seats.join(session="s-o1b", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("aa11bb22cc33dd44", "ds4pro")   # assigned to me, unstarted
        rc, _o, err = self.guard({"session_id": "s-o1b"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2, err)
        self.assertIn("you're free", err)
        self.assertIn("helm chat claim dispatch:aa11bb22", err)

    # ── the ACTUATOR (council #1, unanimous): unambiguous offer → self-claim ──
    # The gap these pin: 19 autonomous work-offers in 48h, 0 converted to a
    # self-claim — a sensor array with no muscle. UNAMBIGUOUS (head dispatched
    # TO this idle seat, kind build/review) auto-claims; everything else stays
    # an offer; every uncertainty fails toward the offer.

    def test_autoclaim_fires_for_own_assigned_review_dispatch(self):
        """[i] The conversion itself, on the REAL stop-hook path: a genuinely
        idle seat with a review dispatch addressed TO IT gets the AUTO-CLAIM
        whisper (fp autoclaim:<id8>, keep-working wording — never
        take-it-or-pass) AND the dispatch:<id8> lease is REALLY minted, bound
        to this seat + session."""
        repo = self.repo()
        self.git(repo, "checkout", "-q", "-b", "review-tip")
        with open(os.path.join(repo, "change"), "w") as f:
            f.write("not landed\n")
        self.git(repo, "add", "change")
        self.git(repo, "commit", "-q", "-m", "review tip")
        tip = self.git(repo, "rev-parse", "HEAD")
        self.git(repo, "checkout", "-q", "main")
        seats.join(session="s-ac1", seat="ds4pro", cwd=repo)
        self.plant_dispatch("ac11ac11ac11ac11", "ds4pro", kind="review",
                            repo_id=os.path.join(repo, ".git"), tip=tip, ref=tip)
        rc, _o, err = self.guard({"session_id": "s-ac1"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2, err)
        self.assertIn("[helm stop-whisper]", err)
        self.assertIn("auto-claimed dispatch ac11ac11", err)
        self.assertIn("START it now", err)
        self.assertIn("review the canary", err)      # the lane, glanceable
        self.assertNotIn("you're free", err)         # not the offer wording
        self.assertNotIn("or pass", err)
        row = seats._live_claims().get("dispatch:ac11ac11")
        self.assertIsInstance(row, dict, "the lease was never acquired")
        self.assertEqual(row.get("holder"), "ds4pro")
        self.assertEqual(row.get("session"), "s-ac1")
        self.assertTrue(row.get("lease"))            # a real minted nonce
        # the lease is self-reinforcing: the NEXT stop hits the session-lease
        # BLOCK naming the held dispatch — an idle seat is idling on its own
        # task and gets told so, louder
        rc, _o, err = self.guard({"session_id": "s-ac1"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2, err)
        self.assertIn("dispatch:ac11ac11", err)
        self.assertNotIn("auto-claimed", err)        # once — never re-whispered

    def test_open_review_already_on_trunk_says_close_never_start(self):  # noqa: VACUOUS_ASSERTION — positive controls bind the OPEN row, exact trunk sha, and CLOSING instruction before asserting START/lease absence
        """The live P0: OPEN is not UNFINISHED. A review tip already reachable
        from trunk must surface closure, never mint a lease or instruct START."""
        repo = self.repo()
        tip = self.git(repo, "rev-parse", "HEAD")
        seats.join(session="s-landed", seat="ds4pro", cwd=repo)
        self.plant_dispatch("ac12ac12ac12ac12", "ds4pro", kind="review",
                            repo_id=os.path.join(repo, ".git"), tip=tip, ref=tip)

        rc, _o, err = self.guard({"session_id": "s-landed"},
                                 args=["--seat", "ds4pro"])

        self.assertEqual(rc, 2, err)
        line = next(x for x in err.splitlines() if "stop-whisper" in x)
        self.assertIn("is OPEN", line)
        self.assertIn("appears on trunk at " + tip[:12], line)
        self.assertIn("CLOSING, not starting", line)
        self.assertNotIn("START", line)
        self.assertNotIn("auto-claimed", line)
        self.assertNotIn("dispatch:ac12ac12", seats._live_claims() or {})

    def test_patch_equivalent_review_on_trunk_also_says_close(self):  # noqa: VACUOUS_ASSERTION — positive controls bind the distinct cherry-picked sha and exact trunk/CLOSING output before asserting START/lease absence
        """Normal lands cherry-pick the reviewed tip. The shared predicate's
        patch-identity rung must suppress START when ancestry alone says no."""
        repo = self.repo()
        self.git(repo, "checkout", "-q", "-b", "review-tip")
        with open(os.path.join(repo, "reviewed"), "w") as f:
            f.write("reviewed\n")
        self.git(repo, "add", "reviewed")
        self.git(repo, "commit", "-q", "-m", "reviewed")
        tip = self.git(repo, "rev-parse", "HEAD")
        self.git(repo, "checkout", "-q", "main")
        with open(os.path.join(repo, "drift"), "w") as f:
            f.write("drift\n")
        self.git(repo, "add", "drift")
        self.git(repo, "commit", "-q", "-m", "drift")
        self.git(repo, "cherry-pick", tip)
        trunk = self.git(repo, "rev-parse", "HEAD")
        self.assertNotEqual(tip, trunk)
        seats.join(session="s-patch", seat="ds4pro", cwd=repo)
        self.plant_dispatch("ac14ac14ac14ac14", "ds4pro", kind="review",
                            repo_id=os.path.join(repo, ".git"), tip=tip, ref=tip)

        rc, _o, err = self.guard({"session_id": "s-patch"},
                                 args=["--seat", "ds4pro"])

        self.assertEqual(rc, 2, err)
        line = next(x for x in err.splitlines() if "stop-whisper" in x)
        self.assertIn("appears on trunk at " + trunk[:12], line)
        self.assertIn("CLOSING, not starting", line)
        self.assertNotIn("START", line)
        self.assertNotIn("dispatch:ac14ac14", seats._live_claims() or {})

    def test_open_build_base_on_trunk_is_unknown_never_start(self):  # noqa: VACUOUS_ASSERTION — positive controls bind UNKNOWN, verify-before-starting, and no-lease wording before asserting actuator absence
        """A BUILD ref is its starting base, not produced work. Probing that
        ref would call every fresh build LANDED; using it to START would rebuild
        the confirmed stale-row incident. UNKNOWN is the only truthful state."""
        repo = self.repo()
        base = self.git(repo, "rev-parse", "HEAD")
        seats.join(session="s-build", seat="ds4pro", cwd=repo)
        self.plant_dispatch("ac13ac13ac13ac13", "ds4pro", kind="build",
                            repo_id=os.path.join(repo, ".git"), tip=base, ref=base)

        with mock.patch.object(seats, "_offer_landing_state",
                               wraps=seats._offer_landing_state) as proof:
            rc, _o, err = self.guard({"session_id": "s-build"},
                                     args=["--seat", "ds4pro"])

        self.assertEqual(rc, 2, err)
        proof.assert_called_once()
        line = next(x for x in err.splitlines() if "stop-whisper" in x)
        self.assertIn("landing state is UNKNOWN", line)
        self.assertIn("verify before starting", line)
        self.assertIn("no lease was claimed", line)
        self.assertNotIn("START it now", line)
        self.assertNotIn("auto-claimed", line)
        self.assertNotIn("dispatch:ac13ac13", seats._live_claims() or {})

    def test_stranded_dispatch_stays_an_offer_never_autoclaimed(self):
        """[ii] AMBIGUOUS stays surfaced: a row assigned to an absent OTHER
        seat is the stranded-work case — routing it is a judgment call, so
        the offer (with its [assigned:] tag) fires and NO lease is taken."""
        seats.join(session="s-ac2", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("ac22ac22ac22ac22", "grok", kind="review")
        rc, _o, err = self.guard({"session_id": "s-ac2"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2, err)
        self.assertIn("you're free", err)
        self.assertIn("[assigned: grok]", err)
        self.assertIn("or pass", err)
        self.assertNotIn("auto-claimed", err)
        self.assertNotIn("dispatch:ac22ac22", seats._live_claims() or {})

    def test_no_recipient_pool_row_stays_an_offer(self):
        """[iii] A no-recipient POOL row is ambiguous by definition (any idle
        seat could argue fit). Today's ledger schema requires a recipient, so
        the row is synthesized at the open_rows seam — the structural
        fail-closed (`mine` demands a NON-EMPTY recipient match) must hold
        even if the schema ever relaxes."""
        from helm import dispatches
        seats.join(session="s-ac3", seat="ds4pro", cwd="/tmp/p")
        row = {"id": "ac33ac33ac33ac33", "recipient": None,
               "lane": "pool review", "kind": "review"}
        with mock.patch.object(dispatches, "open_rows", return_value=[row]):
            got = seats._work_offer_candidate("s-ac3", "ds4pro",
                                              None, None, [], False)
        self.assertIsNotNone(got, "the pool row never reached the rung")
        fp, line = got
        self.assertTrue(fp.startswith("offer:"), fp)
        self.assertIn("or pass", line)
        self.assertNotIn("auto-claimed", line)
        self.assertNotIn("dispatch:ac33ac33", seats._live_claims() or {})

    def test_lost_claim_race_yields_silence_never_steals(self):  # noqa: VACUOUS_ASSERTION — the claimed-row holder/session assertions positively bind the competing lease whose silence is the contract
        """[iv] The race, made deterministic: another seat takes the lease
        AFTER _offer_rows snapshots the claim set (patched to the pre-race
        view). Under mint-on-win the autoclaim candidate still WINS selection
        on the stale snapshot; _stop_whisper then tries to MINT and the
        claim() underneath — the REAL flocked mint — refuses. That is
        SILENCE this stop (rc 0, plain allow), NOT a downgraded offer: the
        row is someone else's now, so offering it would invite the very
        takeover the mutex just prevented. The mutex, not the snapshot,
        protects the row; the now-owned row drops out of _offer_rows next
        stop, nothing minted for this seat, nothing latched, no crash."""
        seats.join(session="s-ac4", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("ac44ac44ac44ac44", "ds4pro", kind="review")
        ok, _m, _l = seats.claim("dispatch:ac44ac44", "other", session="s-other")
        self.assertTrue(ok)
        with mock.patch.object(seats, "_live_claims", return_value={}), \
                mock.patch.object(seats, "_offer_landing_state",
                                  return_value=(False, "f" * 40)):
            rc, _o, err = self.guard({"session_id": "s-ac4"},
                                     args=["--seat", "ds4pro"])
        self.assertEqual(rc, 0, err)                 # silence: the plain allow
        self.assertNotIn("stop-whisper", err)        # no whisper of ANY rung
        self.assertNotIn("auto-claimed", err)        # never claims a won mint
        self.assertNotIn("you're free", err)         # and never re-offers the
        self.assertNotIn("or pass", err)             # row another seat holds
        row = seats._live_claims().get("dispatch:ac44ac44")
        self.assertEqual(row.get("holder"), "other")   # never stolen
        self.assertEqual(row.get("session"), "s-other")  # …nor re-sessioned

    def test_non_idle_seat_neither_offers_nor_autoclaims(self):
        """[v] The idle gate is unweakened: a seat mid-claim never reaches
        the rung at all — no offer, no auto-claim, no lease on the planted
        row, even though it is addressed to this seat and review-kind."""
        seats.join(session="s-ac5", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("ac55ac55ac55ac55", "ds4pro", kind="review")
        seats.claim("worktree-x", "ds4pro", ttl=300, session="s-ac5")
        rc, _o, err = self.guard({"session_id": "s-ac5"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2)                      # the lease BLOCK owns it
        self.assertNotIn("you're free", err)
        self.assertNotIn("auto-claimed", err)
        self.assertNotIn("dispatch:ac55ac55", seats._live_claims() or {})

    def test_unrecorded_or_foreign_kind_stays_an_offer(self):
        """[vi] The kind gate fails CLOSED both ways: an unrecorded kind
        (None — the honest UNKNOWN) and an unrecognised kind ("deploy") each
        keep the offer, even on a row addressed to this very seat. Only
        build/review may be self-assigned."""
        seats.join(session="s-ac6", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("ac66ac66ac66ac66", "ds4pro")   # kind UNRECORDED
        rc, _o, err = self.guard({"session_id": "s-ac6"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2, err)
        self.assertIn("you're free", err)
        self.assertIn("helm chat claim dispatch:ac66ac66", err)
        self.assertNotIn("auto-claimed", err)
        self.assertNotIn("dispatch:ac66ac66", seats._live_claims() or {})
        # a junk/foreign kind on a second seat's own head: offer, not actuate
        seats.join(session="s-ac6b", seat="opus9", cwd="/tmp/p")
        self.plant_dispatch("ac77ac77ac77ac77", "opus9", kind="deploy")
        rc, _o, err = self.guard({"session_id": "s-ac6b"}, args=["--seat", "opus9"])
        self.assertEqual(rc, 2, err)
        self.assertIn("you're free", err)
        self.assertIn("helm chat claim dispatch:ac77ac77", err)
        self.assertNotIn("auto-claimed", err)
        self.assertNotIn("dispatch:ac77ac77", seats._live_claims() or {})

    def test_red_gate_outranks_offer_and_mints_no_claim(self):  # noqa: VACUOUS_ASSERTION — gate-ran-RED binds the higher winner before the proof-spy absence is asserted
        """[vii] Own-work-first, REFUTED then closed (codex-family safety
        review): the pre-fix actuator minted the lease inside
        _work_offer_candidate during candidate ENUMERATION, so a seat whose
        red gate was about to outrank the offer STILL acquired a new task's
        lease — a state change with zero surface, locked under a seat being
        told to fix its OWN work. Mint-on-win closes it: a genuinely idle
        seat with a RED gate AND its own assigned review dispatch gets the
        red-gate whisper, and the actuator — having only PROPOSED — mints
        NOTHING."""
        seats.join(session="s-ac8", seat="ds4pro", cwd="/tmp/p")
        p = os.path.join(record.session_dir("s-ac8"), "command-log.jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:                     # a red run, latest per token
            f.write(json.dumps({"ts": int(time.time()), "token": "pytest",
                                "exit": 1, "digest": "aaa"}) + "\n")
        self.plant_dispatch("ac88ac88ac88ac88", "ds4pro", kind="review")
        with mock.patch.object(seats, "_offer_landing_state") as proof:
            rc, _o, err = self.guard({"session_id": "s-ac8"},
                                     args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2, err)
        self.assertIn("gate ran RED", err)           # the higher rung WINS…
        proof.assert_not_called()                    # …so Git proof never runs
        self.assertNotIn("auto-claimed", err)        # …the actuator stays quiet
        self.assertNotIn("dispatch:ac88ac88",        # and minted NOTHING —
                         seats._live_claims() or {})  # proposal is not a lease

    def test_busy_seat_mid_claim_gets_no_offer(self):
        seats.join(session="s-o2", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("b1b2c3d4e5f60022", "ghost")
        seats.claim("worktree-x", "ds4pro", ttl=300, session="s-o2")
        rc, _o, err = self.guard({"session_id": "s-o2"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2)                     # the claim-lease BLOCK fires
        self.assertIn("worktree-x", err)
        self.assertNotIn("you're free", err)        # mid-claim ⇒ not idle ⇒ no offer

    def test_own_dispatch_obligation_outranks_and_suppresses_offer(self):
        seats.join(session="s-o3", seat="ds4pro", cwd="/tmp/p")
        # a NEEDS-CONFIRMATION dispatch is the seat's own obligation (dispatch rung)
        self.plant_dispatch("c1c2c3d4e5f60033", "ghost", observed=False)
        rc, _o, err = self.guard({"session_id": "s-o3"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2)
        self.assertIn("NEEDS CONFIRMATION", err)    # own work first
        self.assertNotIn("you're free", err)

    def test_pending_inbox_suppresses_offer(self):
        seats.join(session="s-o3b", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("cc11dd22ee33ff44", "ghost")
        chat.post("@ds4pro look at this", who="owner")   # an undelivered mention
        rc, _o, err = self.guard({"session_id": "s-o3b"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2)
        self.assertIn("undelivered", err)           # the inbox block owns the stop
        self.assertNotIn("you're free", err)

    def test_all_claimed_backlog_gets_no_offer(self):
        seats.join(session="s-o4", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("d1d2c3d4e5f60044", "ghost")
        # another idle seat already claimed this exact row's key
        seats.claim("dispatch:d1d2c3d4", "other", ttl=300, session="s-other")
        rc, _o, err = self.guard({"session_id": "s-o4"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 0, err)                 # nothing unowned → clean stop
        self.assertNotIn("you're free", err)

    def test_never_poaches_a_row_owned_by_another_live_seat(self):
        seats.join(session="s-a", seat="ds4pro", cwd="/tmp/p")
        seats.join(session="s-b", seat="worker-b", cwd="/tmp/p")   # a LIVE recipient
        self.plant_dispatch("e1e2c3d4e5f60055", "worker-b")
        rc, _o, err = self.guard({"session_id": "s-a"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 0, err)                 # in-flight to a live seat — theirs
        self.assertNotIn("you're free", err)

    def test_offers_the_oldest_backlog_row_first(self):
        seats.join(session="s-r", seat="ds4pro", cwd="/tmp/p")
        now = time.time()
        iso = lambda dt: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(dt))
        # both recent (not overdue), different ts — open_rows sorts oldest first
        self.plant_dispatch("f1110000aaaa1111", "ghost", lane="newer review",
                            ts=iso(now - 60))
        self.plant_dispatch("f2220000bbbb2222", "ghost", lane="older review",
                            ts=iso(now - 600))
        rc, _o, err = self.guard({"session_id": "s-r"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2, err)
        self.assertIn("older review", err)           # oldest ts wins
        self.assertIn("f2220000", err)
        self.assertNotIn("newer review", err)

    def test_no_backlog_no_offer_clean_stop(self):
        seats.join(session="s-e", seat="ds4pro", cwd="/tmp/p")
        rc, _o, err = self.guard({"session_id": "s-e"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 0, err)                 # fail-closed on empty backlog
        self.assertNotIn("you're free", err)
        self.assertIn("inbox clean", err)            # the ordinary clean-idle warn

    def test_unreadable_claims_fail_closed_no_offer(self):
        seats.join(session="s-f", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("a9a9c3d4e5f60099", "ghost")
        chat._ensure_dir()
        with open(seats.claims_path(), "w") as f:
            f.write("not json{{")                    # claims UNKNOWN → unsure
        rc, _o, err = self.guard({"session_id": "s-f"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 0, err)                 # never poach on uncertainty
        self.assertNotIn("you're free", err)

    def test_kill_switch_silences_the_offer(self):
        seats.join(session="s-k", seat="ds4pro", cwd="/tmp/p")
        self.plant_dispatch("b9b9c3d4e5f60088", "ghost")
        os.environ["HELM_STOP_GUARD_WHISPER"] = "0"
        rc, _o, err = self.guard({"session_id": "s-k"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("you're free", err)

    def test_offer_line_stays_within_the_byte_cap(self):
        seats.join(session="s-cap", seat="d" * 40, cwd="/tmp/p")
        self.plant_dispatch("c9c9c3d4e5f60077", "ghost", lane="x" * 80)
        rc, _o, err = self.guard({"session_id": "s-cap"}, args=["--seat", "d" * 40])
        self.assertEqual(rc, 2, err)
        line = next(l for l in err.splitlines() if "stop-whisper" in l)
        self.assertLessEqual(len(line.encode()), seats.STOP_WHISPER_CAP)
        self.assertIn("helm chat claim dispatch:c9c9c3d4", err)   # cmd never clipped

    def test_new_head_after_a_take_offers_once(self):
        seats.join(session="s-n", seat="ds4pro", cwd="/tmp/p")
        now = time.time()
        iso = lambda dt: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(dt))
        self.plant_dispatch("11aa11aa11aa11aa", "ghost", lane="first", ts=iso(now - 600))
        self.plant_dispatch("22bb22bb22bb22bb", "ghost", lane="second", ts=iso(now - 60))
        rc, _o, err = self.guard({"session_id": "s-n"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2, err)
        self.assertIn("first", err)                  # head #1
        # the seat took it: claim its key → head advances to #2, offered once
        seats.claim("dispatch:11aa11aa", "ds4pro", ttl=300, session="s-other2")
        rc, _o, err = self.guard({"session_id": "s-n"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 2, err)
        self.assertIn("second", err)                 # new head, new fp → one offer
        rc, _o, err = self.guard({"session_id": "s-n"}, args=["--seat", "ds4pro"])
        self.assertEqual(rc, 0, err)                 # latched again


class ClaimsTest(SeatsBase):
    def test_lease_is_the_confirmation_token_composite_binding(self):
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
        self.assertIn("the lease id", msg)
        # SELF-RESCUING: the refusal names the surface that reprints the
        # holder's own token. A refusal that states a requirement and leaves
        # the honest holder no route stranded a lane for 3.3h (2026-07-29).
        self.assertIn("helm chat claims", msg)
        self.assertIn("helm work list", msg)
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
        session opens nothing without the lease token."""
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sA"}):
            rc, out, _ = self.cmd("claim", ["port:1", "--seat", "alice"])
        self.assertEqual(rc, 0)
        # B knows sA (roster/API) and even sets it as their ambient session
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sA"}):
            rc, _out, err = self.cmd("release", ["port:1", "--seat", "alice"])
        self.assertEqual(rc, 1)
        self.assertIn("the lease id", err)
        # the old flag is dead: passing it changes nothing
        rc, _out, err = self.cmd("release", ["port:1", "--seat", "alice",
                                             "--session", "sA"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(seats.claims_list()), 1)   # still held
        # the printed lease IS the confirmation token
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
        # claims_list stays the token-free PUBLISH boundary (the web ledger
        # serves this exact row shape). Holders read their own token through
        # seats.own_leases, which the CLI surfaces join in — see
        # tests/test_lease_recovery.py.
        self.assertNotIn("lease", row)
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


class CouncilTest(SeatsBase):
    """The 0.3 council deferral is CASHED (2026-07-24): verdict/reveal are the
    embargoed N-of-M quorum. Design-locked in a live 3-way standup with
    codex-3 + kimi — council RATIFIES (release go/no-go, adopt-this-design);
    it is NOT the per-slice correctness gate, which stays the open
    reviewer-finds-then-FIXES ping-pong (embargoing a found defect would
    strand the hot-context fix)."""

    def test_help_never_advertises_a_deferral_that_shipped(self):
        """A lying help surface costs more than a missing one: chat.py kept
        advertising 'DEFERRED to 0.3' after the council verbs worked, so a
        reviewer grepping for the feature read the stale HELP as proof it had
        never landed and set out to REBUILD it (live 2026-07-24). Pin the
        help to the shipped behaviour."""
        from helm import chat
        for v in ("verdict", "reveal", "council-status", "council-abort"):
            h = chat.HELP[v]
            self.assertNotIn("DEFERRED", h.upper())
            self.assertIn("council", h.lower())

    def test_every_council_verb_is_dispatchable(self):
        """The wiring gap a unit test cannot see (dogfood-caught): the handler
        answered council-status/council-abort while the DISPATCHER's verb
        table did not list them, so the CLI rejected them as unknown. Pin the
        table against the handler's own set."""
        from helm import chat
        for v in ("verdict", "reveal", "council-status", "council-abort"):
            self.assertIn(v, chat.SEAT_VERBS)
            rc, _out, err = self.cmd(v, ["no-such-room"])
            self.assertNotIn("unknown subcommand", err)   # DISPATCHES, always
            if v == "council-status":
                self.assertEqual(rc, 0)     # a QUERY that answered "none here"
            else:
                self.assertEqual(rc, 2)     # an ACTION that could not act

    def test_verdict_on_a_room_with_no_council_refuses(self):
        rc, _out, err = self.cmd("verdict", ["no-such-room", "YES",
                                             "--tip", "abc123"])
        self.assertEqual(rc, 2)
        self.assertIn("no council convened", err)
        # arg validation runs BEFORE the state lookup (cheap first): a signal
        # with no tip is refused on its own terms, not as a missing council
        rc, _out, err = self.cmd("verdict", ["no-such-room", "YES"])
        self.assertEqual(rc, 2)
        # WAS "EXACT artifact", and that expectation was a WRONG-GATE pass:
        # a room with NO COUNCIL used to hit the tip-required refusal before
        # the registry was ever read, so this arm asserted an artifact
        # complaint about a room that has no council at all. The registry
        # check is now the first thing that answers, which is the accurate
        # reason. The arm still proves a verdict on a councilless room is
        # refused — it just names the real gate.
        self.assertIn("no council convened", err)

    def test_reveal_is_embargoed_below_quorum_and_opens_at_it(self):
        from helm import council
        council.convene("cr", ["seat-a", "seat-b", "seat-c"], 1, tip="deadbeef")
        self.assertEqual(council.registry("cr")["threshold"], 2)   # majority
        council.signal("cr", "seat-a", "YES", "deadbeef", "ev-a")
        rows, err = council.reveal("cr")
        self.assertIsNone(rows)                     # nothing below quorum
        self.assertIn("EMBARGOED", err)
        self.assertNotIn("seat-a", err)             # not even WHO signed
        council.signal("cr", "seat-b", "NO", "deadbeef", "ev-b")
        rows, err = council.reveal("cr")
        self.assertIsNone(err)
        self.assertEqual([(r["seat"], r["verdict"]) for r in rows],
                         [("seat-a", "YES"), ("seat-b", "NO")])

    def test_a_damaged_ledger_never_becomes_a_fresh_council(self):
        """codex-3 xrev, closed structurally: the snapshot version read a
        CORRUPT file exactly like a MISSING one, so convene clobbered sealed
        judgments. An append-only ledger cannot be overwritten at all — a
        ledger that exists means a council was convened here, and a damaged
        tail reduces to a refusal rather than to a usable fresh council."""
        from helm import council
        council.convene("crC", ["seat-a", "seat-b"], 1, threshold=2, tip="deadbeef")
        council.signal("crC", "seat-a", "YES", "deadbeef", "sealed")
        with open(council.registry_path("crC"), "a") as fh:
            fh.write("{ torn tail\n")           # a half-written trailing row
        reg, err = council.convene("crC", ["x", "y"], 2, threshold=1, tip="feed")
        self.assertIsNone(reg)
        self.assertIn("already convened", err)   # never a fresh council
        self.assertEqual(council.registry("crC")["members"],
                         ["seat-a", "seat-b"])   # the sealed record survives
        self.assertIn("seat-a", council.registry("crC")["signals"])

    def test_the_record_survives_a_reboot(self):
        """DURABILITY (codex-3's independently-blocking finding, and the one I
        wrongly declined as 'a substrate decision'). A council's whole product
        is a RECORDED verdict; the snapshot lived in /dev/shm, so a reboot
        made reveal answer 'no council' and lost members/tip/verdicts/terminal
        truth irrecoverably. The ledger is on disk under home.global_dir() and
        REPLAYS — simulated here by dropping every in-memory trace and reading
        the record back cold."""
        from helm import council
        council.convene("crD", ["seat-a", "seat-b"], 1, threshold=2, tip="deadbeef")
        council.signal("crD", "seat-a", "YES", "deadbeef", "ran the suite")
        council.signal("crD", "seat-b", "NO", "deadbeef", "perf regressed")
        self.assertTrue(os.path.exists(council.registry_path("crD")))
        replayed = council.registry("crD")        # cold read from the ledger
        self.assertEqual(replayed["tip"], "deadbeef")
        self.assertEqual(replayed["members"], ["seat-a", "seat-b"])
        self.assertEqual(replayed["signals"]["seat-a"]["verdict"], "YES")
        self.assertEqual(replayed["signals"]["seat-b"]["evidence"],
                         "perf regressed")
        rows, err = council.reveal("crD")
        self.assertIsNone(err)
        self.assertEqual(council.outcome(rows)[0][:10], "NOT CARRIE")  # 1-1 tie

    def test_all_members_needed_is_stated_not_discovered(self):
        """Majority of 2 IS 2, so the commonest council (two reviewers) needs
        BOTH — and the embargo hides which one is missing. No threshold fixes
        it (1-of-2 is one seat deciding alone), so the honest move is to say
        so at status time instead of letting the council wedge silently."""
        from helm import council
        council.convene("crU", ["seat-a", "seat-b"], 1, tip="deadbeef")
        self.assertEqual(council.registry("crU")["threshold"], 2)
        out = "\n".join(council.status_lines("crU"))
        self.assertIn("needs EVERY member", out)
        council.convene("crW", ["a", "b", "c"], 1, tip="deadbeef")          # 3 -> majority 2
        self.assertNotIn("needs EVERY member",
                         "\n".join(council.status_lines("crW")))

    def test_reveal_is_terminal_no_late_judgment(self):
        """codex-3 xrev: a judgment cast once the others are VISIBLE is not
        independent — the single property the embargo exists to guarantee."""
        from helm import council
        council.convene("crT", ["seat-a", "seat-b", "seat-c"], 1, threshold=1, tip="deadbeef")
        council.signal("crT", "seat-a", "YES", "deadbeef", "ev")
        council.reveal("crT")
        _r, err = council.signal("crT", "seat-b", "NO", "deadbeef", "ev")
        self.assertIn("REVEALED", err)

    def test_a_council_judges_ONE_artifact(self):
        """codex-3 xrev: signals binding DIFFERENT tips could reach 'quorum'
        over judgments about different code — a tally that means nothing."""
        from helm import council
        council.convene("crM", ["seat-a", "seat-b"], 1, threshold=2,
                        tip="aaaaaaaa")
        council.signal("crM", "seat-a", "YES", "aaaaaaaa", "ev")
        _r, err = council.signal("crM", "seat-b", "YES", "bbbbbbbb", "ev")
        self.assertIn("bound to tip", err)
        n, _k, is_open = council.tally("crM")
        self.assertEqual((n, is_open), (1, False))   # no phantom quorum

    def test_a_signal_emits_NOTHING_public_before_quorum(self):
        """THE EMBARGO LEAK, closed by DELETION not patching. Two rounds of
        publishing progress 'safely' both leaked: as who=signer the row's own
        from field named the seat it promised to hide; as who=convener the
        post still touched the signer's presence cross-seat, and a public
        '1 of 2' identifies the other signer by elimination anyway. A
        guarantee you keep narrowing is not a guarantee — zero WHO is only
        true if nothing is emitted. council-status serves the count on
        demand instead."""
        from helm import chat, council
        os.environ["HELM_CHAT_NAME"] = "voter-a"
        council.convene("crL", ["voter-a", "voter-b"], 1, threshold=2,
                        convener="the-convener", tip="deadbeef")
        rc = seats._cmd_council("verdict", ["crL", "YES", "--tip", "deadbeef"])
        self.assertEqual(rc, 0)                    # the signal IS recorded...
        self.assertIn("voter-a", council.registry("crL")["signals"])
        rows, _t = chat.read("crL")                # ...and says NOTHING public
        self.assertEqual([r for r in rows if "signalled" in (r.get("text") or "")], [])
        self.assertEqual([r for r in rows if r.get("from") == "voter-a"], [])

    def test_quorum_is_a_reveal_bar_not_a_decision(self):
        """codex-3 re-gate: YES+NO reached reveal with no aggregate rule, so
        'quorum reached' could be misread as 'ratified'. A ratification needs
        POSITIVE support; a tie does not carry."""
        from helm import council
        self.assertEqual(council.outcome([{"verdict": "YES"},
                                          {"verdict": "NO"}])[0][:10],
                         "NOT CARRIE")
        self.assertEqual(council.outcome([{"verdict": "YES"},
                                          {"verdict": "YES"},
                                          {"verdict": "NO"}])[0], "CARRIED")
        self.assertEqual(council.outcome([{"verdict": "ABSTAIN"}])[0][:10],
                         "NOT CARRIE")

    def test_terminal_state_is_written_once(self):
        """codex-3 re-gate: abort-after-reveal and a repeated abort both
        overwrote recorded terminal truth."""
        from helm import council
        council.convene("crX", ["seat-a", "seat-b"], 1, threshold=1, tip="deadbeef")
        council.abort("crX", "seat-a", "first reason")
        _r, err = council.abort("crX", "seat-b", "second reason")
        self.assertIn("already ABORTED", err)
        self.assertEqual(council.registry("crX")["abort_reason"], "first reason")
        council.convene("crY", ["seat-a", "seat-b"], 1, threshold=1, tip="deadbeef")
        council.signal("crY", "seat-a", "YES", "deadbeef", "e")
        council.reveal("crY")
        _r, err = council.abort("crY", "seat-a", "too late")
        self.assertIn("already REVEALED", err)     # never retroactively unpublish

    def _plant(self, room, ev):
        import json
        from helm import council
        with open(council.registry_path(room), "a") as fh:
            fh.write(json.dumps(dict({"v": 1, "id": room}, **ev)) + "\n")

    def test_a_planted_row_from_another_council_never_counts(self):
        """codex-3 durability re-gate: a planted row with the right member,
        verdict and tip but room=OTHER / epoch=999 / a bogus digest was folded
        in and REACHED QUORUM. The binding fields rode the digest and nothing
        ever checked them — a binding you never verify is decoration."""
        from helm import council
        council.convene("cx", ["m-a", "m-b"], 7, threshold=2, tip="abc123")
        for ev in ({"event": "signal", "room": "OTHER", "epoch": 7, "seat": "m-a",
                    "verdict": "YES", "tip": "abc123", "digest": "x"},
                   {"event": "signal", "room": "cx", "epoch": 999, "seat": "m-a",
                    "verdict": "YES", "tip": "abc123", "digest": "x"},
                   {"event": "signal", "room": "cx", "epoch": 7, "seat": "m-b",
                    "verdict": "YES", "tip": "abc123", "digest": "BOGUS"}):
            self._plant("cx", ev)
        self.assertEqual(council.tally("cx"), (0, 2, False))
        self.assertIn("EMBARGOED", council.reveal("cx")[1])

    def test_an_outsider_cannot_abort_a_council_by_planting_a_row(self):
        """codex-3 durability re-gate: the VERB checked membership but the
        FOLD did not, so a planted abort row permanently killed any council.
        A guard that lives only in the write path is no guard at all on a file
        anyone can append to."""
        from helm import council
        council.convene("cy", ["m-a", "m-b"], 7, threshold=2, tip="abc123")
        self._plant("cy", {"event": "abort", "room": "cy", "epoch": 7,
                           "seat": "OUTSIDER", "reason": "killed"})
        self.assertEqual(council.registry("cy")["status"], "open")
        self._plant("cy", {"event": "abort", "room": "cy", "epoch": 7,
                           "seat": "m-b", "reason": "a member may"})
        self.assertEqual(council.registry("cy")["status"], "aborted")

    def test_unreadable_is_UNKNOWN_never_no_council_convened(self):
        """codex-3 durability re-gate: 'could not read the ledger' collapsed
        into the same None as 'no council here', so status answered the
        confident lie 'no council convened'. The vacuous-pass class again."""
        from unittest import mock
        from helm import council
        council.convene("cz", ["m-a", "m-b"], 7, threshold=2, tip="abc123")
        with mock.patch("helm.eventledger.checked_events",
                        return_value=(None, "permission denied")):
            state, unavailable = council.read("cz")
            self.assertIsNone(state)
            self.assertEqual(unavailable, "permission denied")
            self.assertIn("UNREADABLE", "\n".join(council.status_lines("cz")))
            _r, err = council.signal("cz", "m-a", "YES", "abc123", "e")
            self.assertIn("UNKNOWN", err)
            self.assertNotIn("no council convened", err)

    def test_a_junk_file_never_squats_a_council_name_forever(self):
        """codex-3 durability re-gate: keying the refusal on the PATH let any
        junk file — an id-less row, a truncated write — squat that council
        name permanently, with no recovery through the API at all. Existence
        is not valid state."""
        import os as _os
        from helm import council
        p = council.registry_path("cw")
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write('{"v":1,"event":"signal","seat":"nobody"}\n')  # no convene
        reg, err = council.convene("cw", ["m-a", "m-b"], 7, threshold=2,
                                   tip="abc123")
        self.assertIsNone(err)                     # the name was FREE
        self.assertEqual(reg["members"], ["m-a", "m-b"])

    def test_a_pre_ledger_ABORT_still_binds(self):
        """codex-3 durability re-gate: councils that ran before the ledger left
        tmpfs snapshots the new code cannot see, so registry() said None and
        convene would hand out that room again — silently resurrecting a
        council somebody had already ABORTED, the one thing an abort exists to
        prevent."""
        from helm import chat, council, pk
        chat._ensure_dir()
        self._legacy("cold")          # a COMPLETE v1 aborted snapshot
        reg, err = council.convene("cold", ["m-a", "m-b"], 9, threshold=2,
                                   tip="newtip")
        self.assertIsNone(reg)
        self.assertIn("already convened", err)     # never resurrected
        old = council.registry("cold")
        self.assertEqual(old["status"], "aborted")
        self.assertIn("found a defect", old["abort_reason"])

    def _legacy(self, _room, **over):
        from helm import chat, council, pk
        chat._ensure_dir()
        # a COMPLETE v1 snapshot — v1 always wrote room, created, and a
        # seat+tip+ts on every signal, so the fixture must too; tests that
        # want a broken field remove or replace it explicitly via **over
        snap = {"v": 1, "room": _room, "members": ["m-a", "m-b"], "threshold": 2,
                "epoch": 3, "convener": "cv", "tip": "oldtip",
                "status": "aborted", "created": "2020-01-01T00:00:00Z",
                "signals": {}, "aborted_by": "m-a",
                "abort_reason": "found a defect"}
        snap.update(over)
        pk.write_json(council._legacy_path(_room), snap)

    def test_a_hostile_legacy_snapshot_never_PARTIALLY_imports(self):
        """codex-3 migration attack: the first version validated as it went and
        wrote in TWO steps, so broken legacy data got half-imported —
        threshold='bogus' raised an uncaught ValueError AFTER the convene had
        already landed, and duplicate members wrote a convene the reducer then
        dropped. Validate the WHOLE snapshot before ANY append."""
        from helm import council
        # INVERTED (codex round 4): this used to ACCEPT a fresh council over a
        # broken TERMINAL snapshot — my own test encoded the resurrection. A
        # snapshot that CLAIMS a terminal status and cannot be parsed is
        # precisely the state we must never convene over: UNKNOWN, not empty.
        for i, bad in enumerate(({"threshold": "bogus"}, {"threshold": "2"},
                                 {"members": ["m-a", "m-a"]},
                                 {"members": []}, {"members": ["m-a", ""]},
                                 {"signals": [{"seat": "m-a"}]},
                                 {"tip": ""}, {"epoch": "bad"})):
            room = "leg%d" % i
            self._legacy(room, **bad)
            reg, err = council.convene(room, ["x", "y"], 9, threshold=2,
                                       tip="newtip")
            self.assertIsNone(reg, bad)           # never opens over it
            self.assertIn("could not migrate", err, bad)
        # a NON-terminal snapshot genuinely has nothing to preserve, so the
        # name is free — the one case where proceeding is right
        self._legacy("legopen", status="open")
        reg, err = council.convene("legopen", ["x", "y"], 9, threshold=2,
                                   tip="newtip")
        self.assertIsNone(err)
        self.assertEqual(reg["members"], ["x", "y"])

    def test_a_revealed_snapshot_that_cannot_show_quorum_is_refused(self):
        """codex round 4: invalid signal entries were silently skipped while
        the status stayed `revealed`, so a 2-of-N snapshot with one good and
        one bad signal replayed as revealed-with-1 and reveal() answered
        EMBARGOED 1/2 — the same label-versus-product contradiction one layer
        down from the round that was supposed to close it."""
        from helm import council
        self._legacy("legshort", status="revealed", threshold=2, signals={
            "m-a": {"seat": "m-a", "verdict": "YES", "evidence": "ok"},
            "m-b": {"seat": "m-b", "verdict": "MAYBE", "evidence": "bad"}})
        reg, err = council.convene("legshort", ["x", "y"], 9, threshold=2,
                                   tip="newtip")
        self.assertIsNone(reg)
        self.assertIn("could not migrate", err)   # refuse, never replay it

    def test_a_planted_import_event_is_atomic_too(self):
        """The reducer had the same hole as the migration that writes one: a
        skipped entry shrank the verdicts behind a label that still said
        revealed."""
        import json
        import os as _os
        from helm import council
        p = council.registry_path("legplant")
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(json.dumps({
                "v": 1, "id": "legplant", "event": "import", "room": "legplant",
                "epoch": 1, "members": ["m-a", "m-b"], "threshold": 2,
                "tip": "t", "status": "revealed",
                "signals": [{"seat": "m-a", "verdict": "YES", "tip": "t",
                             "evidence": "", "digest": "WRONG"}]}) + "\n")
        self.assertIsNone(council.registry("legplant"))   # dropped whole

    def test_an_outsider_aborted_by_cannot_RESURRECT_the_council(self):
        """The ironic one. The import wrote convene-THEN-abort, and this
        module's own actor gate DROPPED an abort whose seat was not a member —
        so the migration written to make resurrection impossible left the
        council OPEN. One atomic import event carries the terminal state."""
        from helm import council
        self._legacy("legout", aborted_by="a-total-stranger")
        reg, err = council.convene("legout", ["x", "y"], 9, threshold=2,
                                   tip="newtip")
        self.assertIsNone(reg)
        self.assertIn("already convened", err)
        self.assertEqual(council.registry("legout")["status"], "aborted")

    def test_a_REVEALED_legacy_council_stays_revealed(self):
        """codex-3 migration attack: every terminal status was imported as an
        ABORT, destroying the exact truth the migration exists to preserve."""
        from helm import council
        # a real revealed council always carries at least its threshold in
        # signals — the earlier fixture asserted an impossible state (revealed
        # with none), which the round-4 rule now correctly refuses
        self._legacy("legrev", status="revealed", signals={
            "m-a": {"seat": "m-a", "verdict": "YES", "evidence": "e", "tip": "oldtip", "ts": "2020-02-02T00:00:00Z"},
            "m-b": {"seat": "m-b", "verdict": "NO", "evidence": "e", "tip": "oldtip", "ts": "2020-03-03T00:00:00Z"}})
        council.convene("legrev", ["x", "y"], 9, threshold=2, tip="newtip")
        self.assertEqual(council.registry("legrev")["status"], "revealed")

    def test_the_FIRST_event_is_bound_to_its_own_ledger(self):
        """codex-3 migration attack: later events were checked against the
        convene; the convene itself was checked against NOTHING, so room=OTHER
        and a string epoch were accepted and permanently squatted the ledger
        they were written into."""
        import json
        import os as _os
        from helm import council
        p = council.registry_path("legbind")
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            for ev in ({"v": 1, "id": "legbind", "event": "convene",
                        "room": "SOMEWHERE-ELSE", "epoch": 1,
                        "members": ["m-a"], "threshold": 1, "tip": "t"},
                       {"v": 1, "id": "legbind", "event": "convene",
                        "room": "legbind", "epoch": "bad",
                        "members": ["m-a"], "threshold": 1, "tip": "t"}):
                fh.write(json.dumps(ev) + "\n")
        self.assertIsNone(council.registry("legbind"))   # neither squats it
        reg, err = council.convene("legbind", ["x", "y"], 9, threshold=2,
                                   tip="newtip")
        self.assertIsNone(err)                           # the name is still FREE
        self.assertEqual(reg["members"], ["x", "y"])

    def test_the_import_is_a_SUPERSET_of_the_snapshot_it_replaces(self):
        """The completeness proof, added instead of waiting for a fourth
        dropped field to be found one at a time.

        The migration DECOMPOSES a 'replay the whole snapshot' idea into a
        structured event, and decomposition is exactly where completeness
        leaks silently: `status` was carried and the recorded verdicts were
        not, so a revealed council replayed as revealed-with-nothing-in-it and
        only a reviewer's probe caught it. This asserts every meaningful field
        of a rich snapshot survives the round trip, so the NEXT field someone
        forgets to carry fails here rather than in a gate — or worse, in a
        council whose verdicts quietly vanished."""
        from helm import council
        snap = {"v": 1, "members": ["m-a", "m-b", "m-c"], "threshold": 2, "epoch": 42,
                "convener": "cv", "tip": "abc1234", "status": "revealed",
                "signals": {
                    "m-a": {"seat": "m-a", "verdict": "YES", "evidence": "e-a",
                            "tip": "abc1234", "ts": "2020-02-02T00:00:00Z"},
                    "m-b": {"seat": "m-b", "verdict": "NO", "evidence": "e-b",
                            "tip": "abc1234", "ts": "2020-03-03T00:00:00Z"},
                    "m-c": {"seat": "m-c", "verdict": "ABSTAIN", "evidence": "",
                            "tip": "abc1234", "ts": "2020-04-04T00:00:00Z"}}}
        self._legacy("legfull", **snap)
        council.convene("legfull", ["x", "y"], 9, threshold=2, tip="newtip")
        got = council.registry("legfull")
        self.assertIsNotNone(got)
        # identity of the council
        self.assertEqual(got["members"], snap["members"])
        self.assertEqual(got["threshold"], snap["threshold"])
        self.assertEqual(got["epoch"], snap["epoch"])
        self.assertEqual(got["convener"], snap["convener"])
        self.assertEqual(got["tip"], snap["tip"])
        self.assertEqual(got["status"], snap["status"])
        # and its PRODUCT — every judgment, with its verdict and evidence
        self.assertEqual(sorted(got["signals"]), sorted(snap["signals"]))
        for seat, old in snap["signals"].items():
            self.assertEqual(got["signals"][seat]["verdict"], old["verdict"], seat)
            self.assertEqual(got["signals"][seat]["evidence"], old["evidence"], seat)
        # and it is USABLE, not merely present: reveal works and the outcome
        # is computed from the real judgments (1 YES, 1 NO, 1 ABSTAIN = tie)
        rows, err = council.reveal("legfull")
        self.assertIsNone(err)
        self.assertEqual(len(rows), 3)
        label, counts = council.outcome(rows)
        self.assertEqual((counts["YES"], counts["NO"], counts["ABSTAIN"]),
                         (1, 1, 1))
        self.assertEqual(label[:10], "NOT CARRIE")

    def test_the_snapshots_own_claims_are_validated_not_rewritten(self):
        """codex round 5, the sharpest migration finding: `old.room` was
        IGNORED and identity taken from the path, and each signal was mapped by
        its dict KEY while the embedded seat and tip were ignored. So a body
        claiming room=OTHER imported as this room, and signals keyed a/b but
        carrying seat b/a on a foreign tip imported as valid a/b judgments on
        ours. Accepting a claim by overwriting it is laundering, not
        validation — the record ends up asserting what its source never
        said."""
        from helm import council
        self._legacy("clm1", **{"room": "SOMEWHERE-ELSE"})
        _r, err = council.convene("clm1", ["x", "y"], 9, threshold=2, tip="n")
        self.assertIn("could not migrate", err)
        self._legacy("clm2", status="revealed", signals={
            "m-a": {"seat": "m-b", "verdict": "YES", "evidence": ""},
            "m-b": {"seat": "m-a", "verdict": "NO", "evidence": ""}})
        _r, err = council.convene("clm2", ["x", "y"], 9, threshold=2, tip="n")
        self.assertIn("could not migrate", err)      # swapped seats refused
        self._legacy("clm3", status="revealed", signals={
            "m-a": {"seat": "m-a", "verdict": "YES", "tip": "OTHER-TIP",
                    "evidence": ""},
            "m-b": {"seat": "m-b", "verdict": "NO", "evidence": ""}})
        _r, err = council.convene("clm3", ["x", "y"], 9, threshold=2, tip="n")
        self.assertIn("could not migrate", err)      # foreign tip refused

    def test_v1_required_fields_are_REQUIRED_not_required_if_present(self):
        """codex round 6: the checks were `if x is not None and x != expected`
        — validate-IF-PRESENT — and the absence was then filled in from the
        path, the dict key, or the top level. v1 ALWAYS wrote a top-level room
        and a seat+tip+ts on every signal, so a snapshot missing them is not a
        lenient case to accommodate, it is one I do not understand.
        Optionalising a required field and inventing its value is how five
        rounds of this bug kept producing a sixth."""
        from helm import chat, council, pk
        chat._ensure_dir()
        base = {"v": 1, "room": "req", "members": ["m-a", "m-b"], "threshold": 2,
                "epoch": 3, "convener": "cv", "tip": "oldtip",
                "created": "2020-01-01T00:00:00Z", "status": "revealed",
                "signals": {
                    "m-a": {"seat": "m-a", "verdict": "YES", "tip": "oldtip",
                            "evidence": "e", "ts": "2020-02-02T00:00:00Z"},
                    "m-b": {"seat": "m-b", "verdict": "NO", "tip": "oldtip",
                            "evidence": "e", "ts": "2020-03-03T00:00:00Z"}}}
        for drop in ("room", "created", "signals"):
            snap = dict(base)
            del snap[drop]
            pk.write_json(council._legacy_path("req"), snap)
            _r, err = council.convene("req", ["x", "y"], 9, threshold=2, tip="n")
            self.assertIn("could not migrate", err, drop)
        for field in ("seat", "tip", "ts"):
            sig = dict(base["signals"]["m-a"])
            del sig[field]
            snap = dict(base, signals=dict(base["signals"], **{"m-a": sig}))
            pk.write_json(council._legacy_path("req"), snap)
            _r, err = council.convene("req", ["x", "y"], 9, threshold=2, tip="n")
            self.assertIn("could not migrate", err, field)

    def test_evidence_falsy_types_are_not_coerced_to_empty_string(self):
        """The SAME erasure as the `or {}` two lines up — which I fixed last
        round and left standing here. `s.get("evidence") or ""` turned [] / 0 /
        False into a valid empty string BEFORE the isinstance meant to catch
        them: the instance, not the class, one line away in one function."""
        from helm import council
        for bad in ([], 0, False, {}):
            self._legacy("ev", status="revealed", signals={
                "m-a": {"seat": "m-a", "verdict": "YES", "tip": "oldtip",
                        "evidence": bad, "ts": "2020-02-02T00:00:00Z"},
                "m-b": {"seat": "m-b", "verdict": "NO", "tip": "oldtip",
                        "evidence": "e", "ts": "2020-03-03T00:00:00Z"}})
            _r, err = council.convene("ev", ["x", "y"], 9, threshold=2, tip="n")
            self.assertIn("could not migrate", err, repr(bad))

    def test_a_planted_import_without_provenance_is_refused(self):
        """The reducer's `created or ev.ts` and `s.ts or ev.ts` fallbacks
        INVENTED missing timestamps as migration time — while the commit
        message that shipped them said absent stays absent."""
        import json
        import os as _os
        from helm import council
        p = council.registry_path("noprov")
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(json.dumps({
                "v": 1, "id": "noprov", "event": "import", "room": "noprov",
                "epoch": 1, "members": ["m-a"], "threshold": 1, "tip": "t",
                "status": "aborted", "signals": []}) + "\n")   # no `created`
        self.assertIsNone(council.registry("noprov"))

    def test_writer_and_reducer_share_ONE_rule(self):
        """Seven rounds and the recurring shape was never the object, it was
        the PAIR: `evidence or ""` fixed on the write path and left on the
        read path, a required ts enforced when writing and INDEXED unchecked
        when reading. Two validators for one contract always drift. This pins
        that they are now literally the same function — anything the writer
        emits, the reducer accepts, and anything the reducer drops, the writer
        refuses to emit."""
        from helm import council
        # the ENVELOPE is part of the event — my fixture omitted v/id exactly
        # as the code did, which is why the gap survived a round
        good = {"v": 1, "id": "r", "ts": "2020-01-01T00:00:00Z",
                "event": "import", "room": "r", "epoch": 1,
                "members": ["m-a", "m-b"], "threshold": 1, "tip": "t",
                "status": "aborted", "created": "2020-01-01T00:00:00Z",
                "aborted_by": "m-a", "reason": "found a defect",
                "signals": []}
        self.assertEqual(council._valid_import(good, room="r")[0], True)
        # THE CASE I FOUND BY ATTACKING MY OWN CLAIM: an event addressed to
        # another ledger. The validator used to accept it (it never saw the
        # room) while the fold dropped it — a disagreement hiding inside a
        # commit message that asserted there were none. The binding now lives
        # IN the shared rule, so both answer alike.
        self.assertEqual(council._valid_import(good, room="OTHER")[0], False)
        self.assertIsNone(council._apply(None, good, room="OTHER"))
        # every mutation the reducer must drop, the writer must also refuse
        for bad in ({"created": []}, {"created": ""}, {"status": "open"},
                    {"epoch": "1"}, {"threshold": 0}, {"tip": ""},
                    {"signals": {}}, {"members": ["m-a", "m-a"]},
                    # envelope + terminal union (codex round 8)
                    {"v": 2}, {"id": "OTHER"}, {"aborted_by": {}},
                    {"aborted_by": ""}, {"reason": []},
                    # round 9: the last fields _emit stamped after validation
                    {"ts": []}, {"ts": ""}, {"convener": {}}):
            ev = dict(good, **bad)
            self.assertEqual(council._valid_import(ev, room="r")[0], False, bad)
            self.assertIsNone(council._apply(None, ev, room="r"), bad)

    def test_a_planted_import_with_falsy_evidence_or_no_ts_is_dropped(self):
        """codex round 7: the reducer still did `str(s.evidence or "")` — the
        erasure the normalizer already refused — and INDEXED s["ts"], so an
        import missing it raised KeyError out of registry() instead of
        dropping the event."""
        import json
        import os as _os
        from helm import council
        for sig in ({"seat": "m-a", "verdict": "YES", "tip": "t",
                     "evidence": [], "ts": "2020-01-01T00:00:00Z"},
                    {"seat": "m-a", "verdict": "YES", "tip": "t",
                     "evidence": ""}):                      # no ts at all
            p = council.registry_path("falsy")
            _os.makedirs(_os.path.dirname(p), exist_ok=True)
            sig = dict(sig, digest=council.evidence_digest(
                "t", "YES", "", room="falsy", epoch=1, seat="m-a"))
            with open(p, "w") as fh:
                fh.write(json.dumps({
                    "v": 1, "id": "falsy", "event": "import", "room": "falsy",
                    "epoch": 1, "members": ["m-a"], "threshold": 1, "tip": "t",
                    "status": "aborted", "created": "2020-01-01T00:00:00Z",
                    "signals": [sig]}) + "\n")
            self.assertIsNone(council.registry("falsy"))     # dropped, no raise

    def test_a_snapshot_without_v1_is_a_schema_I_do_not_know(self):
        """codex round 7: _normalize_legacy never checked old.v, so a missing
        or foreign schema version imported as v1 on faith — and the fixture I
        called COMPLETE omitted it too."""
        from helm import council
        for v in (None, 2, "1"):
            over = {} if v is None else {"v": v}
            self._legacy("ver", **over)
            if v is None:
                from helm import pk
                snap = pk.read_json(council._legacy_path("ver"), None)
                del snap["v"]
                pk.write_json(council._legacy_path("ver"), snap)
            _r, err = council.convene("ver", ["x", "y"], 9, threshold=2, tip="n")
            self.assertIn("could not migrate", err, repr(v))

    def test_an_empty_LIST_of_signals_is_not_an_empty_mapping(self):
        """`old.get("signals") or {}` turned an empty LIST into {}, so
        signals=[] slipped past the exact-container rule."""
        from helm import council
        self._legacy("emptylist", signals=[])
        _r, err = council.convene("emptylist", ["x", "y"], 9, threshold=2,
                                  tip="n")
        self.assertIn("could not migrate", err)

    def test_an_unreadable_legacy_FILE_is_not_an_absent_one(self):
        """The FOURTH appearance of this collapse in one file: a legacy file
        that EXISTS but will not parse is a snapshot I cannot read, not 'no
        snapshot here' — and returning absent let convene open a fresh council
        over it."""
        import os as _os
        from helm import chat, council
        chat._ensure_dir()
        p = council._legacy_path("badjson")
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write("{ definitely not json")
        _r, err = council.convene("badjson", ["x", "y"], 9, threshold=2,
                                  tip="n")
        self.assertIsNone(_r)
        self.assertIn("could not migrate", err)

    def test_imported_verdicts_keep_their_OWN_timestamps(self):
        """codex round 5: per-signal `ts` and the council's `created` were
        replaced by IMPORT time, dating every legacy judgment to the migration
        — rewriting history in the one record whose purpose is preserving
        it."""
        from helm import council
        self._legacy("prov", status="revealed", created="2020-01-01T00:00:00Z",
                     signals={
                         "m-a": {"seat": "m-a", "verdict": "YES", "tip": "oldtip",
                                 "evidence": "e", "ts": "2020-02-02T00:00:00Z"},
                         "m-b": {"seat": "m-b", "verdict": "NO", "tip": "oldtip",
                                 "evidence": "e", "ts": "2020-03-03T00:00:00Z"}})
        council.convene("prov", ["x", "y"], 9, threshold=2, tip="n")
        reg = council.registry("prov")
        self.assertEqual(reg["created"], "2020-01-01T00:00:00Z")
        self.assertEqual(reg["signals"]["m-a"]["ts"], "2020-02-02T00:00:00Z")
        self.assertEqual(reg["signals"]["m-b"]["ts"], "2020-03-03T00:00:00Z")

    def test_junk_in_the_ledger_never_SUPPRESSES_the_migration(self):
        """codex re-gate, and the same lesson one function over: I fixed
        convene() to stop keying on the PATH and left _import_legacy doing
        exactly that. A ledger holding only readable junk suppressed the
        migration, and convene then opened a FRESH council over a snapshot
        that had already been aborted. Fixing the instance and not the class,
        in the same file."""
        import json
        import os as _os
        from helm import council
        self._legacy("legjunk")                       # a real old ABORT
        p = council.registry_path("legjunk")
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:                      # signal-only: no valid state
            fh.write(json.dumps({"v": 1, "id": "legjunk", "event": "signal",
                                 "seat": "nobody"}) + "\n")
        reg, err = council.convene("legjunk", ["x", "y"], 9, threshold=2,
                                   tip="newtip")
        self.assertIsNone(reg)
        self.assertIn("already convened", err)        # never resurrected
        self.assertEqual(council.registry("legjunk")["status"], "aborted")

    def test_a_FAILED_import_blocks_the_convene_it_could_not_replace(self):
        """codex re-gate: the _emit result was ignored, so a failed first
        append looked identical to a successful one and the next convene
        appended a fresh OPEN council over an aborted snapshot."""
        from unittest import mock
        from helm import council
        self._legacy("legfail")
        with mock.patch("helm.council._emit", return_value=False):
            reg, err = council.convene("legfail", ["x", "y"], 9, threshold=2,
                                       tip="newtip")
        self.assertIsNone(reg)
        self.assertIn("could not migrate", err)       # refuses, never opens

    def test_a_REVEALED_import_carries_the_VERDICTS_not_just_the_label(self):
        """codex re-gate: import copied `status` and nothing else, so a
        revealed legacy council replayed as revealed-with-signals-{} — reveal()
        answered EMBARGOED 0 of 2 and the outcome was an empty tie. The label
        survived; the council's actual PRODUCT did not."""
        from helm import council
        self._legacy("legsig", status="revealed", signals={
            "m-a": {"seat": "m-a", "verdict": "YES", "evidence": "suite green", "tip": "oldtip", "ts": "2020-02-02T00:00:00Z"},
            "m-b": {"seat": "m-b", "verdict": "NO", "evidence": "perf", "tip": "oldtip", "ts": "2020-03-03T00:00:00Z"}})
        council.convene("legsig", ["x", "y"], 9, threshold=2, tip="newtip")
        reg = council.registry("legsig")
        self.assertEqual(reg["status"], "revealed")
        self.assertEqual(sorted(reg["signals"]), ["m-a", "m-b"])   # the product
        rows, err = council.reveal("legsig")
        self.assertIsNone(err)                        # not EMBARGOED 0 of 2
        self.assertEqual([(r["seat"], r["verdict"]) for r in rows],
                         [("m-a", "YES"), ("m-b", "NO")])
        self.assertEqual(council.outcome(rows)[0][:10], "NOT CARRIE")  # real tie

    def test_a_hostile_ledger_row_cannot_manufacture_a_quorum(self):
        """codex-3's re-gate probes, now answered structurally rather than by
        a validator bolted onto a snapshot: an OUTSIDER's signal satisfying
        the threshold, a threshold of 0 opening quorum, and a non-object
        signal reaching reveal. The reducer typechecks every event against
        the CONVENED council and DROPS what does not fit, so a corrupt or
        planted tail reduces to nothing instead of to a usable verdict."""
        import json
        from helm import council, eventledger
        council.convene("crS", ["seat-a", "seat-b"], 1, threshold=2, tip="deadbeef")
        path = council.registry_path("crS")
        with open(path, "a") as fh:                # planted directly, no verb
            for ev in ({"v": 1, "id": "crS", "event": "signal", "seat": "stranger",
                        "verdict": "YES", "tip": "deadbeef", "digest": "d"},
                       {"v": 1, "id": "crS", "event": "signal", "seat": "seat-a",
                        "verdict": "MAYBE", "tip": "deadbeef", "digest": "d"},
                       {"v": 1, "id": "crS", "event": "signal", "seat": "seat-a",
                        "verdict": "YES", "tip": "OTHER-TIP", "digest": "d"},
                       {"v": 1, "id": "crS", "event": "convene", "members": [],
                        "threshold": 0, "tip": ""}):
                fh.write(json.dumps(ev) + "\n")
        n, k, is_open = council.tally("crS")
        self.assertEqual((n, k, is_open), (0, 2, False))   # nothing counted
        rows, err = council.reveal("crS")
        self.assertIsNone(rows)
        self.assertIn("EMBARGOED", err)                    # no phantom quorum
        self.assertEqual(council.registry("crS")["members"],
                         ["seat-a", "seat-b"])             # convene not replaced

    def test_the_convener_fixes_the_artifact_not_the_first_signal(self):
        """codex-3 re-gate: a first-signal-selected target lets the fastest
        member choose the question everyone else is judging."""
        from helm import council
        council.convene("crB", ["seat-a", "seat-b"], 1, threshold=2,
                        tip="aaaaaaaa")
        self.assertEqual(council.registry("crB")["tip"], "aaaaaaaa")
        _r, err = council.signal("crB", "seat-a", "YES", "bbbbbbbb", "e")
        self.assertIn("bound to tip", err)

    def test_the_binding_digest_covers_council_epoch_and_member(self):
        """codex-3 re-gate: the digest covered only tip+verdict+evidence, so
        the same judgment digested identically across councils, epochs and
        members — an idempotency key that cannot tell two assertions apart."""
        from helm import council
        base = council.evidence_digest("t", "YES", "e", room="r1",
                                       epoch=1, seat="a")
        self.assertNotEqual(base, council.evidence_digest(
            "t", "YES", "e", room="r2", epoch=1, seat="a"))
        self.assertNotEqual(base, council.evidence_digest(
            "t", "YES", "e", room="r1", epoch=2, seat="a"))
        self.assertNotEqual(base, council.evidence_digest(
            "t", "YES", "e", room="r1", epoch=1, seat="b"))

    def test_a_sealed_judgment_is_never_silently_replaced(self):
        from helm import council
        council.convene("cr2", ["seat-a", "seat-b"], 1, tip="deadbeef")
        council.signal("cr2", "seat-a", "YES", "deadbeef", "ev")
        _r, err = council.signal("cr2", "seat-a", "YES", "deadbeef", "ev")
        self.assertIsNone(err)                      # identical retry: idempotent
        _r, err = council.signal("cr2", "seat-a", "NO", "deadbeef", "ev")
        self.assertIn("DIFFERENT judgment", err)    # conflicting: REFUSED
        _r, err = council.signal("cr2", "stranger", "YES", "deadbeef", "ev")
        self.assertIn("not a member", err)          # actor-gated on the set

    def test_abort_seals_the_embargo_permanently(self):
        from helm import council
        council.convene("cr3", ["seat-a", "seat-b"], 1, threshold=1, tip="deadbeef")
        council.abort("cr3", "seat-a", "must collaborate first")
        _r, err = council.reveal("cr3")
        self.assertIn("ABORTED", err)               # never reveals, ever
        _r, err = council.signal("cr3", "seat-b", "YES", "deadbeef", "e")
        self.assertIn("ABORTED", err)


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

    def test_presence_report_carries_the_shared_ephemeral_tag(self):
        # ONE criterion for every surface that hides review-SAs (the web picker
        # + the fleet-presence 'online' list, owner feature 2026-07-23):
        # agent-<hex> + no home + /tmp -> ephemeral; a real seat never is.
        self.assertTrue(seats._is_ephemeral_sa("agent-deadbeef", None, "/tmp/x"))
        self.assertFalse(seats._is_ephemeral_sa("console-design", None, "/tmp/x"))
        self.assertFalse(seats._is_ephemeral_sa("agent-deadbeef", "main", "/tmp/x"))
        self.assertFalse(seats._is_ephemeral_sa("agent-deadbeef", None, "/home/p"))
        self.assertFalse(seats._is_ephemeral_sa(None, None, None))  # fail-safe
        seats.join(seat="realseat", cwd="/home/p/proj", session="s-r")
        row = next(s for s in seats.presence_report() if s["seat"] == "realseat")
        self.assertIn("ephemeral", row)   # the 'online' list can drop SAs
        self.assertFalse(row["ephemeral"])

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


class ReportNeverMutatesTest(SeatsBase):
    """codex-2 HIGH: roster_report auto-ran the legacy reap_roster, which
    deleted ANY stale row on presence alone — an inactive-but-fully-persisted
    seat lost its row to a 3s web poll, bypassing gc's transcript/process
    evidence and the manual dry-run gate. The legacy path is DELETED, not
    fenced: a report is a READ, cleanup has exactly one owner (gc_roster)."""

    def test_legacy_auto_reap_is_gone(self):
        self.assertFalse(hasattr(seats, "reap_roster"))

    def test_report_keeps_a_stale_seat_row_identical(self):
        """codex-2's exact probe: persisted seat present before
        roster_report(), row byte-identical after — no matter how stale its
        presence is, the report consults NO deletion evidence at all."""
        seats.join(seat="idle-persisted", session="s-idle", cwd="/tmp/p")
        with open(seats._stop_fp_path("main", "idle-persisted", "s-idle"),
                  "w") as f:
            f.write("fp")                       # keyed state must survive too
        old = time.time() - 10 * seats.REAP_S
        os.utime(seats.seen_path("idle-persisted"), (old, old))
        before = seats.roster()["idle-persisted"]
        rep = seats.roster_report("main")
        self.assertEqual(seats.roster()["idle-persisted"], before)
        self.assertIn("idle-persisted", [s["seat"] for s in rep["seats"]])
        names = os.listdir(chat.chat_dir())
        self.assertTrue([n for n in names
                         if seats._seat_key("idle-persisted") in n])

    def test_cli_hides_absent_rows_but_deletes_nothing(self):
        seats.join(seat="live", session="s-l", cwd="/tmp/p")
        seats.join(seat="gone", session="s-g", cwd="/tmp/p")
        old = time.time() - 2 * seats.REAP_S
        os.utime(seats.seen_path("gone"), (old, old))
        rc, out, _ = self.cmd("seats")
        self.assertEqual(rc, 0)
        self.assertIn("live", out)
        self.assertNotIn("gone", out)           # hidden, never deleted
        self.assertIn("hidden", out)
        self.assertIn("seat gc", out)           # the one cleanup owner, named
        rc, out, _ = self.cmd("seats", ["--all"])
        self.assertIn("gone", out)
        self.assertIn("gone", seats.roster())   # the row itself survived

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
        # the endpoint serves through the single-flight TTL cache now, so this
        # test owns its cache state (start clean, leave clean).
        web._ROSTER_REP_CACHE.clear()
        self.addCleanup(web._ROSTER_REP_CACHE.clear)
        # AND it owns the cockpit stamp, for the same reason and a nastier
        # mechanism. _api_chat_roster GRAFTS the owner's row at seats[0] while
        # `_COCKPIT_BEAT` is within OWNER_PRESENCE_TTL (30s), and that beat is a
        # MODULE GLOBAL stamped by every /api/chat/poll — so any web test that
        # polled in the preceding 30 SECONDS OF WALL CLOCK pushed 'alice' to
        # seats[1] and reddened this assertion. Order- AND clock-dependent:
        # green alone, red in the full suite, and it reproduces on main at
        # d4de64c with `unittest tests.test_web_chat tests.test_seats.
        # WebRosterTest`. The owner-row feature has its own coverage in
        # test_fleetnotes.py, which zeroes this same global; this test is about
        # the endpoint's shape and its fail-open, so it starts with no cockpit.
        self.addCleanup(web._COCKPIT_BEAT.__setitem__, 0, web._COCKPIT_BEAT[0])
        web._COCKPIT_BEAT[0] = 0.0
        seats.join(seat="alice", cwd="/tmp/p")
        obj, code = web._api_chat_roster({})
        self.assertEqual(code, 200)
        # LOOK ALICE UP; DO NOT INDEX HER. Index [0] is the OWNER's documented
        # slot, so asserting alice sits there contradicts the endpoint's own
        # contract and makes this test depend on every other test in the suite:
        # it failed as 'p1' != 'alice' when an unrelated file leaked
        # HELM_CELL_PROFILE=p1 and something left the beat fresh. Membership is
        # the property; position was the incidental mechanism.
        self.assertIn("alice", [s["seat"] for s in obj["seats"]])
        with mock.patch.object(seats, "roster_report", side_effect=RuntimeError):
            # WITHIN the TTL a backend failure serves the cached rep — the
            # grace the cache exists for: a transient seats hiccup must not
            # blank the owner's panel (the 2026-07-23 UI-blank class)
            obj, code = web._api_chat_roster({})
            self.assertEqual(code, 200)
            # membership, not position — same reason as above; the cached rep
            # carries whatever [0] held when it was minted
            self.assertIn("alice", [s["seat"] for s in obj["seats"]])
            # PAST the TTL the failure is honest: fail open to unavailable
            at, rep = web._ROSTER_REP_CACHE["main"]
            web._ROSTER_REP_CACHE["main"] = (at - (web._ROSTER_REP_TTL + 1), rep)
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

    def test_rooms_summary_includes_session_consumer_who_never_posted(self):
        seats.join(session="s-quiet", seat="quiet-seat", cwd="/tmp/p")
        chat.post("@quiet-seat ping", who="someone-else")
        seats.deliver(session="s-quiet", seat="quiet-seat")
        self.assertFalse(seats._cursor("main", "quiet-seat")["active"])
        self.assertTrue(seats._cursor(
            "main", "quiet-seat", "s-quiet")["active"])
        rooms = {r["room"]: r for r in web._rooms_summary()}
        names = [s["seat"] for s in rooms["main"]["seats"]]
        self.assertIn("quiet-seat", names)

    def test_rooms_summary_hides_old_room_activity_after_rehome(self):
        seats.join(session="session-old", seat="mover", cwd="/tmp/p",
                   room="old-room", room_explicit=True)
        chat.post("mover spoke before rehome", who="mover", room="old-room")
        chat.post("@mover establish old-room consumption", who="bob",
                  room="old-room")
        seats.deliver(session="session-old", seat="mover", room="old-room")
        self.assertTrue(seats.room_active("old-room", "mover"))
        self.assertTrue(seats.rehome_seat("mover", "new-room")[0])
        rooms = {r["room"]: r for r in web._rooms_summary()}
        names = [s["seat"] for s in rooms["old-room"]["seats"]]
        self.assertNotIn("mover", names)

    def test_rooms_summary_nonzero_eof_baseline_is_not_presence(self):
        # Joining after room traffic creates a nonzero EOF cursor. Offset alone
        # must not imply presence: bob has neither posted nor consumed here.
        chat.post("noise", who="someone-else")
        seats.join(seat="bob", cwd="/tmp/p")
        self.assertGreater(seats._cursor("main", "bob")["off"], 0)
        self.assertFalse(seats._cursor("main", "bob")["active"])
        rooms = {r["room"]: r for r in web._rooms_summary()}
        names = [s["seat"] for s in rooms["main"]["seats"]]
        self.assertNotIn("bob", names)

    def test_rooms_summary_owner_rows_are_not_seats(self):
        chat.post("owner words", who="owner")   # owner rail, not a roster seat
        rooms = {r["room"]: r for r in web._rooms_summary()}
        names = [s["seat"] for s in rooms.get("main", {}).get("seats", [])]
        self.assertNotIn("owner", names)

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


class ReplyWakesParentTest(unittest.TestCase):
    """The owner's WHY was the spec: replies exist so he can stop typing
    @names. A reply is therefore a direct address of the parent's author —
    mention-tier, any room — and only of the parent's author.

    Sits ABOVE the __main__ guard: a class appended after unittest.main()
    is invisible to the direct runner (the guard exits the process), so its
    tests silently never run — the regression this file itself pins below."""

    def _row(self, **kw):
        r = {"text": "a reply", "from": "owner"}
        r.update(kw)
        return r

    def test_a_reply_wakes_the_parent_author_without_a_mention(self):
        self.assertTrue(seats.deliverable(
            self._row(rfrom="opus-integrator"), "opus-integrator", room="main"))

    def test_a_reply_wakes_nobody_else(self):
        self.assertFalse(seats.deliverable(
            self._row(rfrom="opus-integrator"), "codex-3", room="side-room"))

    def test_a_reply_to_your_own_row_does_not_self_wake(self):
        self.assertFalse(seats.deliverable(
            self._row(**{"from": "opus-integrator", "rfrom": "opus-integrator"}),
            "opus-integrator", room="main"))

    def test_mention_tier_means_before_mute(self):
        with mock.patch.object(seats, "seat_scope",
                               return_value={"mute": ["main"], "home": None}):
            self.assertTrue(seats.deliverable(
                self._row(rfrom="opus-integrator"), "opus-integrator", room="main"))

    def test_case_variant_rfrom_still_wakes_the_seat(self):
        # Seat identity is casefold-exact everywhere (roster, mentions, dm) —
        # a case-only rename must not silently lose direct reply delivery:
        # rfrom='kimi' stamped pre-rename must still wake live seat 'Kimi'.
        self.assertTrue(seats.deliverable(
            self._row(rfrom="kimi"), "Kimi", room="side-room"))
        self.assertTrue(seats.deliverable(
            self._row(rfrom="Kimi"), "kimi", room="side-room"))

    def test_case_variant_self_reply_still_does_not_self_wake(self):
        self.assertFalse(seats.deliverable(
            self._row(**{"from": "Kimi", "rfrom": "kimi"}), "Kimi",
            room="main"))

    def test_cross_case_self_reply_does_not_self_wake(self):
        # BOTH reviewers' MED (codex + codex-2 xrev of c2f4856): the test
        # above uses from='Kimi' — exact-case — so it never exercised the
        # real rename path. After a case-only rename kimi -> Kimi, the
        # seat's own pre-rename reply carries from='kimi': the own-post
        # suppression must casefold like rfrom does, or the seat wakes on
        # its own reply — the exact transition this fix targets.
        self.assertFalse(seats.deliverable(
            self._row(**{"from": "kimi", "rfrom": "kimi", "text": "self reply"}),
            "Kimi", room="main"))
        self.assertFalse(seats.deliverable(
            self._row(**{"from": "Kimi", "rfrom": "Kimi", "text": "self reply"}),
            "kimi", room="main"))

    def test_cross_case_own_post_with_self_mention_does_not_self_wake(self):
        # Same normalization beyond replies: a pre-rename own post that
        # happens to @mention the seat's new casing is still its OWN post.
        self.assertFalse(seats.deliverable(
            self._row(**{"from": "kimi", "text": "@Kimi noted"}),
            "Kimi", room="main"))

    def test_empty_rfrom_never_matches_an_empty_seat(self):
        self.assertFalse(seats.deliverable(
            self._row(room="side-room"), "", room="side-room"))

    def test_the_pointer_is_read_the_old_invisibility_law_is_dead(self):
        # The superseded law said deliverable() never reads the pointer. The
        # inversion must be REAL, not vacuous: the same non-mention text flips
        # on rfrom alone.
        row = self._row(rfrom="opus-integrator")
        self.assertTrue(seats.deliverable(row, "opus-integrator",
                                          room="side-room"))
        row.pop("rfrom")
        self.assertFalse(seats.deliverable(row, "opus-integrator",
                                           room="side-room"))

    def test_this_class_is_discovered_by_the_default_loader(self):
        # Pin for the after-main() regression: the loader that both the direct
        # runner and pytest walk must SEE this class's tests.
        names = [str(t) for t in unittest.defaultTestLoader
                 .loadTestsFromTestCase(ReplyWakesParentTest)]
        self.assertGreaterEqual(len(names), 8)
        mod_tests = unittest.defaultTestLoader.loadTestsFromModule(
            sys.modules[__name__])
        flat = str(list(mod_tests))
        self.assertIn("ReplyWakesParentTest", flat)

class HomingOneTruthTest(SeatsBase):
    """The as-prevented roster-scatter law (owner mandate 2026-07-21): a live
    roster held THREE home_room truths for one team — 'main' (a defaulted
    spawn-mirror write), '<project>' (a cwd derivation), '<env room>' (the
    launch seam) — because every writer re-derived the precedence privately.
    Now seats.resolve_homing is the ONE precedence, write_roster the ONE
    enforcement gate, and a derived value can NEVER downgrade an explicit/
    operator home."""

    def _repo(self, name="proj-alpha"):
        import subprocess
        repo = os.path.join(self.tmp, name)
        os.makedirs(repo, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True,
                       capture_output=True)
        state = pk.read_json(home.registry_path(), {"projects": {}})
        state.setdefault("projects", {})[os.path.basename(repo)] = {"path": repo}
        pk.write_json(home.registry_path(), state)
        return repo

    def test_resolve_homing_is_the_one_precedence(self):
        repo = self._repo()
        # cli beats env beats derivation
        os.environ["HELM_CHAT_ROOM"] = "helm-dogfood"
        self.assertEqual(seats.resolve_homing("team-cli", repo),
                         ("team-cli", "explicit"))
        self.assertEqual(seats.resolve_homing(None, repo),
                         ("helm-dogfood", "explicit"))
        # the launch seam's derived marker survives the env hop
        os.environ["HELM_CHAT_ROOM_SOURCE"] = "derived"
        self.assertEqual(seats.resolve_homing(None, repo),
                         ("helm-dogfood", "derived"))
        del os.environ["HELM_CHAT_ROOM"], os.environ["HELM_CHAT_ROOM_SOURCE"]
        # no cli, no env: the project room, derived
        self.assertEqual(seats.resolve_homing(None, repo),
                         ("proj-alpha", "derived"))
        # nothing at all: un-homed
        self.assertEqual(seats.resolve_homing(None, self.tmp), (None, None))

    def _chat_hook(self, args, payload):
        """chat.cmd_chat (the REAL hook entry — the process-cwd pre-resolution
        lives in its prologue) with hook-JSON stdin + FD-1 capture."""
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

    def test_hook_join_homes_from_the_payload_cwd_not_the_hook_process(self):
        """The hook payload's cwd is the SESSION's ground truth; the hook
        PROCESS may run elsewhere (metaharness seam). cmd_chat pre-resolves
        the default room from its own cwd — a derived pre-resolution must be
        re-resolved against the payload cwd, or the seat homes to the hook
        runner's project instead of its own."""
        repo_a, repo_b = self._repo("proj-alpha"), self._repo("proj-beta")
        prior = os.getcwd()
        os.chdir(repo_b)                    # the hook PROCESS cwd: proj-beta
        try:
            rc, _ = self._chat_hook(
                ["join", "--hook-json"],
                json.dumps({"session_id": "s-payload",
                            "cwd": repo_a}).encode("utf-8"))
        finally:
            os.chdir(prior)
        self.assertEqual(rc, 0)
        seat = seats.seat_for_session("s-payload")
        row = seats.roster()[seat]
        self.assertEqual((row["home_room"], row["home_room_source"]),
                         ("proj-alpha", "derived"))

    def test_hook_join_with_projectless_payload_cwd_stays_unhomed(self):
        """A project-less payload cwd must not inherit the hook process's
        derived room either — the seat stays un-homed (legacy all-room)."""
        repo_b = self._repo("proj-beta")
        bare = os.path.join(self.tmp, "no-project")
        os.makedirs(bare)
        prior = os.getcwd()
        os.chdir(repo_b)
        try:
            rc, _ = self._chat_hook(
                ["join", "--hook-json"],
                json.dumps({"session_id": "s-bare",
                            "cwd": bare}).encode("utf-8"))
        finally:
            os.chdir(prior)
        self.assertEqual(rc, 0)
        row = seats.roster()[seats.seat_for_session("s-bare")]
        self.assertIsNone(row.get("home_room"))

    def test_historical_writers_converge_on_the_unified_answer(self):
        """Each historical writer's shape, same context -> ONE answer.
        (a) the hook join (chat.py passes room='main', nothing explicit);
        (b) the launch/env seam (HELM_CHAT_ROOM in the child env);
        (c) the spawn-mirror write_roster (unlabeled home_room)."""
        repo = self._repo()
        # (a) hook join, no env: derives the project room — never 'main'
        seats.join(session="s-hook", seat="w-hook", cwd=repo, room="main")
        row = seats.roster()["w-hook"]
        self.assertEqual((row["home_room"], row["home_room_source"]),
                         ("proj-alpha", "derived"))
        # (b) the env seam agrees with seat.py's add/launch resolver
        os.environ["HELM_CHAT_ROOM"] = "proj-alpha"
        from helm import seat as seat_mod
        self.assertEqual(seat_mod._resolve_homing(None)[0], "proj-alpha")
        seats.join(session="s-env", seat="w-env", cwd=repo, room="proj-alpha")
        row = seats.roster()["w-env"]
        del os.environ["HELM_CHAT_ROOM"]
        self.assertEqual((row["home_room"], row["home_room_source"]),
                         ("proj-alpha", "explicit"))
        # (c) the spawn mirror's unlabeled write fills the same room, as
        # derived — never as a fake 'explicit'
        seats.write_roster("w-mirror", cwd=repo, home_room="proj-alpha")
        row = seats.roster()["w-mirror"]
        self.assertEqual((row["home_room"], row["home_room_source"]),
                         ("proj-alpha", "derived"))

    def test_unlabeled_mirror_write_cannot_downgrade_an_explicit_home(self):
        """THE old hole: _register_spawn's unlabeled write_roster stamped
        itself 'explicit' and clobbered a deliberate home with 'main'."""
        seats.join(session="s-x", seat="pinned", cwd="/tmp/p", room="team-a")
        self.assertEqual(seats.roster()["pinned"]["home_room_source"],
                         "explicit")
        seats.write_roster("pinned", home_room="main")          # old mirror shape
        row = seats.roster()["pinned"]
        self.assertEqual((row["home_room"], row["home_room_source"]),
                         ("team-a", "explicit"))
        seats.write_roster("pinned", home_room="elsewhere",
                           home_room_source="derived")          # labeled derived
        self.assertEqual(seats.roster()["pinned"]["home_room"], "team-a")

    def test_pre_upgrade_unlabeled_home_takes_the_weakest_tier(self):
        """codex's exact probe row: {home_room: 'main', home_room_source:
        None} — write_roster's contract says an unlabeled existing home reads
        as DERIVED, but the old branches only moved a labeled-derived or
        never-homed row, so the stale scattered value froze forever. An
        unlabeled home now follows a derived join and loses to every labeled
        writer."""
        repo = self._repo("proj-b")
        r = seats.roster()
        r["legacy"] = {"home_room": "main"}       # pre-upgrade, no source
        pk.write_json(seats.roster_path(), r)
        seats.join(session="s-l", seat="legacy", cwd=repo)   # a derived join
        row = seats.roster()["legacy"]
        self.assertEqual((row["home_room"], row["home_room_source"]),
                         ("proj-b", "derived"))   # unfrozen, labeled honestly
        # …and the unlabeled tier loses to a labeled explicit writer too
        r = seats.roster()
        r["legacy2"] = {"home_room": "main"}
        pk.write_json(seats.roster_path(), r)
        seats.write_roster("legacy2", home_room="team-e",
                           home_room_source="explicit")
        row = seats.roster()["legacy2"]
        self.assertEqual((row["home_room"], row["home_room_source"]),
                         ("team-e", "explicit"))

    def test_operator_rehome_survives_derived_rejoin_and_mirror(self):
        repo = self._repo()
        seats.join(session="s-op", seat="oper", cwd=repo)
        ok, msg = seats.rehome_seat("oper", "team-z")
        self.assertTrue(ok, msg)
        # a derived re-join (resume in the project cwd) must NOT downgrade
        seats.join(session="s-op2", seat="oper", cwd=repo)
        row = seats.roster()["oper"]
        self.assertEqual((row["home_room"], row["home_room_source"]),
                         ("team-z", "operator"))
        # nor an unlabeled mirror write
        seats.write_roster("oper", home_room="proj-alpha")
        row = seats.roster()["oper"]
        self.assertEqual((row["home_room"], row["home_room_source"]),
                         ("team-z", "operator"))

    def test_explicit_env_rejoin_still_deliberately_moves(self):
        """The precedence's top tier: an explicit HELM_CHAT_ROOM re-join
        outranks the existing explicit/operator value (the deliberate move);
        only DERIVED is forbidden from downgrading."""
        seats.join(session="s-m", seat="mover2", cwd="/tmp/p", room="team-a")
        os.environ["HELM_CHAT_ROOM"] = "team-b"
        seats.join(session="s-m2", seat="mover2", cwd="/tmp/p")
        del os.environ["HELM_CHAT_ROOM"]
        row = seats.roster()["mover2"]
        self.assertEqual((row["home_room"], row["home_room_source"]),
                         ("team-b", "explicit"))


class RosterGcTest(SeatsBase):
    """`helm chat seat gc` — the MANUAL junk-row pruner. Refusal is the
    default: any live evidence (fresh presence, a transcript for any
    remembered session, a live process naming one) keeps the row."""

    def _stale(self, seat):
        old = time.time() - 7200
        os.utime(seats.seen_path(seat), (old, old))

    def _row(self, seat, session=None, stale=True):
        seats.write_roster(seat, session=session)
        if stale:
            self._stale(seat)

    def _empty_dirs(self):
        roots = os.path.join(self.tmp, "no-transcripts")
        proc = os.path.join(self.tmp, "proc")
        os.makedirs(roots, exist_ok=True)
        os.makedirs(proc, exist_ok=True)
        return [roots], proc

    def test_state_unlink_and_move_stop_at_the_key_boundary(self):
        """Substring cross-fire (fable adversarial B3): seat 'foo' (key
        foo-<h1>) must not match the state files of a live seat literally
        NAMED 'foo-<h1>' (its key foo-<h1>-<h2>) — the bare substring test
        let pruning or renaming 'foo' destroy the OTHER seat's delivery
        ground (the gc state cross-fire class, substring flavor)."""
        chat._ensure_dir()
        d = chat.chat_dir()
        key = seats._seat_key("foo")
        okey = seats._seat_key(key)             # the adversarial twin's key
        self.assertTrue(okey.startswith(key))   # the collision shape is real
        own = ["main.cursor.%s" % key, "main.cursor.%s.k1" % key,
               "main.cursor.%s.lock" % key, ".seen.%s" % key]
        twin = ["main.cursor.%s" % okey, "main.cursor.%s.k1" % okey,
                ".seen.%s" % okey]
        for n in own + twin:
            open(os.path.join(d, n), "w").close()
        seats._unlink_seat_state("foo")
        for n in own:
            self.assertFalse(os.path.exists(os.path.join(d, n)), n)
        for n in twin:
            self.assertTrue(os.path.exists(os.path.join(d, n)), n)
        # the rename mover shares the matcher: foo -> bar moves ONLY foo's
        for n in own:
            open(os.path.join(d, n), "w").close()
        seats._move_seat_state("foo", "bar")
        nk = seats._seat_key("bar")
        for n in twin:
            self.assertTrue(os.path.exists(os.path.join(d, n)), n)
        self.assertTrue(
            os.path.exists(os.path.join(d, "main.cursor.%s" % nk)))
        self.assertFalse(
            os.path.exists(os.path.join(d, "main.cursor.%s" % key)))

    def test_move_keeps_a_room_slug_that_embeds_the_key(self):
        """PRE-EXISTING boundary-blind rename substitution (fable adversarial
        probe C10): a room whose slug merely EMBEDS the seat's full key
        ('<key>-updates') had its ROOM segment rewritten by the raw
        str.replace on rename — the cursor silently detached from its room
        (delivery ground lost to an EOF re-baseline, orphan file left).
        _bounded_sub swaps the key only where it fills a whole '.'-field —
        bare, or dm-prefixed for the dm-lane room segment, which carries the
        key TWICE and must still fully move."""
        chat._ensure_dir()
        d = chat.chat_dir()
        ok, nk = seats._seat_key("foo"), seats._seat_key("zed")
        embed = "%s-updates.cursor.%s" % (ok, ok)   # the reviewer's probe
        dmlane = "dm-%s.cursor.%s" % (ok, ok)       # key twice: both move
        for n in (embed, dmlane):
            open(os.path.join(d, n), "w").close()
        seats._move_seat_state("foo", "zed")
        self.assertTrue(os.path.exists(os.path.join(       # room segment kept,
            d, "%s-updates.cursor.%s" % (ok, nk))))        # seat field moved
        self.assertFalse(os.path.exists(os.path.join(
            d, "%s-updates.cursor.%s" % (nk, nk))))
        self.assertTrue(os.path.exists(os.path.join(
            d, "dm-%s.cursor.%s" % (nk, nk))))

    def _proc(self, proc, pid, cmdline=b"", environ=b"", state="S",
              readable=True):
        """One fake /proc/<pid>. `readable=False` mimics an environ we own but
        cannot read — systemd --user on every real host."""
        d = os.path.join(proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stat"), "wb") as f:
            f.write(b"%d (proc) %s 1 1 1\n" % (pid, state.encode()))
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(cmdline)
        p = os.path.join(d, "environ")
        with open(p, "wb") as f:
            f.write(environ)
        if not readable:
            os.chmod(p, 0o000)
        return d

    def test_one_unreadable_SYSTEM_process_no_longer_vetoes_the_whole_scan(self):
        """THE GUARD THAT COULD ONLY EVER SAY KEEP (measured 2026-07-29).

        The scan walks every same-uid process and used to return a keep-reason
        the moment ANY of them had an unreadable environ. `systemd --user` runs
        as us on every host and its environ is never readable, so the probe
        returned "fail closed" every time it was reached — and it is ONLY
        reached for a seat with no presence beat and no transcript, i.e.
        exactly the dead seats gc exists to collect. codex-2 was kept with the
        reason "process 3915 environ unreadable (PermissionError)"; pid 3915 is
        systemd. Nothing had ever been collectable.

        A process whose readable cmdline shows it cannot host an agent carries
        no evidence about a seat and must not be able to veto."""
        roots, proc = self._empty_dirs()
        self._proc(proc, 3915, cmdline=b"/usr/lib/systemd/systemd\0--user\0",
                   readable=False)
        self.assertIsNone(
            seats._live_process_evidence("ghost", ["sid-x"], proc_dir=proc),
            "systemd must not testify about a seat")

    def test_an_unreadable_process_that_COULD_host_an_agent_still_fails_closed(self):
        """The safety half, and the reason the fix is a narrowing rather than a
        removal. Unproven is not absent: if the blind process might be carrying
        the seat's environment, the scan still refuses to testify."""
        roots, proc = self._empty_dirs()
        self._proc(proc, 4242, cmdline=b"/usr/bin/claude\0--resume\0",
                   readable=False)
        why = seats._live_process_evidence("ghost", ["sid-x"], proc_dir=proc)
        self.assertIsNotNone(why)
        self.assertIn("fail closed", why)

    def test_a_ZOMBIE_is_proven_not_live_and_is_skipped(self):
        """A defunct process executes nothing and holds no environment. Its
        cmdline reads empty and its environ is unreadable, which is
        indistinguishable from "cannot examine" — a defunct zypak-sandbox was
        the blind spot still keeping codex-2 uncollectable after systemd was
        handled. Reaped state is evidence of absence, exactly like ENOENT."""
        roots, proc = self._empty_dirs()
        self._proc(proc, 24326, cmdline=b"", state="Z", readable=False)
        self.assertIsNone(
            seats._live_process_evidence("ghost", ["sid-x"], proc_dir=proc))

    def test_a_process_cannot_NAME_ITSELF_into_looking_like_a_zombie(self):
        """kimi's cross-family finding, demonstrated live rather than argued.

        The zombie skip first read `b") Z " in st` — a SUBSTRING search over
        /proc/<pid>/stat. But `comm` sits inside that line and is
        PROCESS-CONTROLLED, so it is a test a process can satisfy about itself:
        prctl(PR_SET_NAME, "agent) Z live") is 13 bytes, inside the 15-byte
        cap, and needs no newline or NUL. A LIVE RUNNING process then parses as
        a zombie, is skipped as proven-not-live, and its seat becomes falsely
        prunable. A process could name itself into being ignored by the reaper.

        comm may legitimately contain ')' too, so the state is the character
        two past the LAST ')' — rfind, never split-on-first."""
        roots, proc = self._empty_dirs()
        d = os.path.join(proc, "7777")
        os.makedirs(d, exist_ok=True)
        # a RUNNING process (state S) whose comm spoofs the old substring test
        with open(os.path.join(d, "stat"), "wb") as f:
            f.write(b"7777 (agent) Z live) S 1 1 1\n")
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"/usr/bin/claude\0")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"HELM_CHAT_NAME=ghost\0")
        why = seats._live_process_evidence("ghost", [], proc_dir=proc)
        self.assertIsNotNone(
            why, "a live process that spoofed ') Z ' in its comm was treated "
                 "as a zombie, so its seat could be pruned out from under it")
        self.assertIn("HELM_CHAT_NAME", why)

    def test_a_comm_containing_a_paren_still_parses(self):
        """The other half of rfind: a legitimate comm with ')' in it must not
        break state detection for a REAL zombie."""
        roots, proc = self._empty_dirs()
        d = os.path.join(proc, "7778")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stat"), "wb") as f:
            f.write(b"7778 (weird)name) Z 1 1 1\n")
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"")
        os.chmod(os.path.join(d, "environ"), 0o000)
        self.assertIsNone(
            seats._live_process_evidence("ghost", ["sid-x"], proc_dir=proc),
            "a real zombie whose comm contains ')' is still proven not-live")

    def test_a_LIVE_seat_process_is_still_found(self):
        """The positive control. Without it every test above passes equally
        well against a probe that has simply stopped looking."""
        roots, proc = self._empty_dirs()
        self._proc(proc, 5150, cmdline=b"/usr/bin/claude\0",
                   environ=b"HELM_CHAT_NAME=ghost\0")
        why = seats._live_process_evidence("ghost", [], proc_dir=proc)
        self.assertIsNotNone(why)
        self.assertIn("HELM_CHAT_NAME", why)

    def test_a_live_process_naming_a_remembered_SESSION_is_still_found(self):
        roots, proc = self._empty_dirs()
        self._proc(proc, 5151, cmdline=b"/usr/bin/claude\0--resume\0sid-keep\0")
        why = seats._live_process_evidence("ghost", ["sid-keep"], proc_dir=proc)
        self.assertIsNotNone(why)
        self.assertIn("remembered session", why)

    def test_gc_refuses_fresh_presence(self):
        self._row("alive", session="sid-alive-1", stale=False)
        roots, proc = self._empty_dirs()
        rows, pruned = seats.gc_roster(apply=True, roots=roots, proc_dir=proc)
        self.assertEqual([r["verdict"] for r in rows], ["keep"])
        self.assertIn("presence beat", rows[0]["why"])
        self.assertEqual(pruned, [])
        self.assertIn("alive", seats.roster())

    def test_gc_refuses_rows_with_a_transcript(self):
        self._row("hist", session="sid-hist-42")
        roots, proc = self._empty_dirs()
        tdir = os.path.join(roots[0], "proj-slug")
        os.makedirs(tdir)
        open(os.path.join(tdir, "sid-hist-42.jsonl"), "w").close()
        rows, pruned = seats.gc_roster(apply=True, roots=roots, proc_dir=proc)
        self.assertEqual([r["verdict"] for r in rows], ["keep"])
        self.assertIn("transcript exists", rows[0]["why"])
        self.assertIn("hist", seats.roster())

    def test_gc_refuses_rows_with_a_live_process(self):
        self._row("busy", session="sid-busy-77")
        roots, proc = self._empty_dirs()
        pdir = os.path.join(proc, "4321")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "cmdline"), "wb") as f:
            f.write(b"claude\x00--resume\x00sid-busy-77\x00")
        rows, pruned = seats.gc_roster(apply=True, roots=roots, proc_dir=proc)
        self.assertEqual([r["verdict"] for r in rows], ["keep"])
        self.assertIn("live process", rows[0]["why"])
        self.assertIn("busy", seats.roster())

    def test_gc_fail_closed_when_proc_table_unreadable(self):
        self._row("junk", session="sid-junk-1")
        roots, _proc = self._empty_dirs()
        rows, pruned = seats.gc_roster(
            apply=True, roots=roots,
            proc_dir=os.path.join(self.tmp, "no-such-proc"))
        self.assertEqual([r["verdict"] for r in rows], ["keep"])
        self.assertEqual(pruned, [])

    def test_gc_dry_run_reports_and_touches_nothing(self):
        self._row("tmp-claude-junk", session="sid-tmp-junk-9")
        roots, proc = self._empty_dirs()
        rows, pruned = seats.gc_roster(roots=roots, proc_dir=proc)
        self.assertEqual([r["verdict"] for r in rows], ["prune"])
        self.assertEqual(pruned, [])
        self.assertIn("tmp-claude-junk", seats.roster())   # dry-run: untouched

    def test_gc_apply_prunes_junk_and_its_state(self):
        self._row("tmp-claude-junk", session="sid-tmp-junk-9")
        self._row("kept-live", session="sid-live-2", stale=False)
        roots, proc = self._empty_dirs()
        seen = seats.seen_path("tmp-claude-junk")
        self.assertTrue(os.path.exists(seen))
        rows, pruned = seats.gc_roster(apply=True, roots=roots, proc_dir=proc)
        self.assertEqual(pruned, ["tmp-claude-junk"])
        self.assertNotIn("tmp-claude-junk", seats.roster())
        self.assertIn("kept-live", seats.roster())
        self.assertFalse(os.path.exists(seen))             # state went with it

    def test_gc_trusts_the_census_for_helm_seat_home_transcripts(self):
        """codex-2 HIGH (finding 1): the old hand-rolled root list omitted
        ~/.helm/_global/seats/**/claude/projects, so an inactive-but-fully-
        persisted proxy seat probed as junk (codex-2's own transcript root
        was missing). roots=None now delegates to session's persistence
        census — the ONE truth owner — which walks the seat homes."""
        from helm import home as _home, transcripts
        self._row("proxy", session="sid-proxy-1")
        proj = os.path.join(_home.global_dir(), "seats", "codex",
                            "instances", "codex-2", "claude", "projects",
                            "slug-x")
        os.makedirs(proj)
        open(os.path.join(proj, "sid-proxy-1.jsonl"), "w").close()
        _roots, proc = self._empty_dirs()
        with mock.patch.object(transcripts, "get_catalog",
                               return_value={"rows": []}):
            rows, pruned = seats.gc_roster(apply=True, roots=None,
                                           proc_dir=proc)
        self.assertEqual([r["verdict"] for r in rows], ["keep"])
        self.assertIn("transcript exists", rows[0]["why"])
        self.assertEqual(pruned, [])
        self.assertIn("proxy", seats.roster())

    def test_gc_incomplete_census_fails_closed(self):
        """A census that could not finish proves nothing — the row stays."""
        from helm import session
        self._row("murky", session="sid-murky-1")
        _roots, proc = self._empty_dirs()
        bad = session._PersistenceCensus({}, complete=False)
        with mock.patch.object(session, "_persisting_sids",
                               return_value=bad):
            rows, pruned = seats.gc_roster(apply=True, roots=None,
                                           proc_dir=proc)
        self.assertEqual([r["verdict"] for r in rows], ["keep"])
        self.assertIn("census incomplete", rows[0]["why"])
        self.assertEqual(pruned, [])
        self.assertIn("murky", seats.roster())

    def test_gc_apply_recheck_keeps_row_when_transcript_lands_late(self):
        """codex-2 HIGH (finding 3): victims were computed before the lock
        and the under-lock recheck was presence-only — a transcript flushing
        between scan and apply still lost the row. The FULL evidence probe
        now re-runs fresh under the roster lock."""
        self._row("late", session="sid-late-9")
        roots, proc = self._empty_dirs()
        tdir = os.path.join(roots[0], "proj-slug")
        os.makedirs(tdir)
        real = seats._flocked

        @contextlib.contextmanager
        def landing(path):
            open(os.path.join(tdir, "sid-late-9.jsonl"), "w").close()
            with real(path):
                yield
        with mock.patch.object(seats, "_flocked", landing):
            rows, pruned = seats.gc_roster(apply=True, roots=roots,
                                           proc_dir=proc)
        self.assertEqual([r["verdict"] for r in rows], ["prune"])  # scan-time
        self.assertEqual(pruned, [])            # the locked recheck refused
        self.assertIn("late", seats.roster())
        self.assertTrue(os.path.exists(seats.seen_path("late")))

    def test_gc_unlink_rides_the_roster_lock_no_rejoin_gap(self):
        """codex-2 HIGH (exact-SHA probe): the row delete committed under the
        roster lock but the derived-state unlink ran AFTER release. A
        SessionStart rejoin slipping into that gap recreated the row plus
        fresh .seen/cursor/DM state — and the old gc invocation then
        unlinked the NEW seat's state (a live seat instantly reading absent,
        its queued DMs and cursors destroyed). Row delete + unlink are now
        ONE locked critical section: a lock-respecting rejoin can only land
        after gc finishes, and everything it creates survives."""
        self._row("phoenix", session="sid-phx-old-1")
        roots, proc = self._empty_dirs()
        key = seats._seat_key("phoenix")
        dm = chat.room_path(chat.DM_PREFIX + key)
        cursor = seats.cursor_path("main", "phoenix", "sid-phx-new-2")

        def rejoin():                 # what SessionStart recreates
            seats.write_roster("phoenix", session="sid-phx-new-2")
            seats.touch_seen("phoenix")
            os.makedirs(os.path.dirname(dm), exist_ok=True)
            with open(dm, "w") as f:  # the freshly queued DM lane
                f.write('{"text": "for the new seat"}\n')
            with open(cursor, "w") as f:
                f.write('{"off": 0}')

        state = {"deferred": False}
        real_unlink = seats._unlink_seat_state

        def racing_unlink(seat):
            # A lock-RESPECTING rejoin racing the unlink boundary: probe the
            # roster flock non-blocking from a second open file description
            # (flock conflicts across OFDs even in one process). Unfixed —
            # unlink after release — the lock is FREE here, the rejoin lands
            # first, then gc destroys its fresh state. Fixed — unlink under
            # the lock — the probe refuses and the rejoin can only land
            # after gc returns.
            import fcntl
            with open(seats.roster_path() + ".lock", "a") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    state["deferred"] = True   # gc still holds the lock
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    rejoin()
            real_unlink(seat)

        with mock.patch.object(seats, "_unlink_seat_state", racing_unlink):
            rows, pruned = seats.gc_roster(apply=True, roots=roots,
                                           proc_dir=proc)
        self.assertEqual(pruned, ["phoenix"])
        self.assertTrue(state["deferred"])     # the boundary was closed
        rejoin()                               # the rejoin lands AFTER gc
        row = seats.roster().get("phoenix")    # …and ALL its state survives
        self.assertIsNotNone(row)
        self.assertEqual(row.get("session"), "sid-phx-new-2")
        self.assertTrue(os.path.exists(seats.seen_path("phoenix")))
        self.assertTrue(os.path.exists(cursor))
        self.assertTrue(os.path.exists(dm))

    def test_gc_process_read_oserror_keeps_the_row(self):
        """codex-2 HIGH (finding 3): a same-uid process whose cmdline/environ
        cannot be read is probe TROUBLE, not absence — the row stays."""
        self._row("murkyproc", session="sid-murkyproc-5")
        roots, proc = self._empty_dirs()
        os.makedirs(os.path.join(proc, "5150", "cmdline"))  # open -> EISDIR
        rows, pruned = seats.gc_roster(apply=True, roots=roots, proc_dir=proc)
        self.assertEqual([r["verdict"] for r in rows], ["keep"])
        self.assertIn("fail closed", rows[0]["why"])
        self.assertEqual(pruned, [])
        self.assertIn("murkyproc", seats.roster())

    def test_gc_exited_process_is_absence_not_trouble(self):
        """A pid that vanished mid-scan (ENOENT) is proven not-live — it must
        NOT fail-close the whole gc into a no-op."""
        self._row("plainjunk", session="sid-plainjunk-2")
        roots, proc = self._empty_dirs()
        os.makedirs(os.path.join(proc, "777"))   # exited: no cmdline/environ
        rows, pruned = seats.gc_roster(apply=True, roots=roots, proc_dir=proc)
        self.assertEqual(pruned, ["plainjunk"])

    def test_gc_keeps_sidless_row_with_live_helm_chat_name(self):
        """codex-2 HIGH (finding 3): a row with NO remembered session had no
        process evidence at all. A live environ carrying HELM_CHAT_NAME=<seat>
        is a live seat, never junk."""
        self._row("envseat")                     # no session remembered
        roots, proc = self._empty_dirs()
        pdir = os.path.join(proc, "6001")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "environ"), "wb") as f:
            f.write(b"PATH=/usr/bin\x00HELM_CHAT_NAME=envseat\x00LANG=C\x00")
        rows, pruned = seats.gc_roster(apply=True, roots=roots, proc_dir=proc)
        self.assertEqual([r["verdict"] for r in rows], ["keep"])
        self.assertIn("HELM_CHAT_NAME=envseat", rows[0]["why"])
        self.assertEqual(pruned, [])
        self.assertIn("envseat", seats.roster())

    def test_gc_cli_dry_run_default(self):
        from helm import session
        self._row("cli-junk", session="sid-cli-junk-3")
        empty = session._PersistenceCensus({}, complete=True)
        with mock.patch.object(session, "_persisting_sids",
                               return_value=empty):
            rc, out, err = self.cmd("seat", ["gc"])
        self.assertEqual(rc, 0, err)
        self.assertIn("dry-run", out)
        self.assertIn("cli-junk", seats.roster())          # verb never applied
        rc, _out, err = self.cmd("seat", ["gc", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)




class DrainInstructionsNameTheRealVerbTest(SeatsBase):
    """AN INSTRUCTION THAT CANNOT BE OBEYED IS WORSE THAN NO INSTRUCTION — it
    IS obeyed, and nothing happens. Both beacon nudges and the stop-guard block
    told agents `helm chat read` would catch them up; chat.consume() clears only
    the owner-unread marker and never touches the seat's delivery cursor, so a
    seat following the instruction exactly drained nothing. ~2,250 rows piled up
    across nine seats and the actuator gate that read that pile never opened for
    anyone (2026-07-30). These pin the strings to verbs that actually work."""

    def test_read_does_not_clear_an_addressed_row_and_we_say_so(self):
        # THE BEHAVIOUR IS CORRECT AND STAYS: an addressed row is an obligation;
        # reading past one does not discharge it. This test exists so nobody
        # "fixes" it by making read silently consume obligations.
        seats.join(session="s-drain", seat="ds4pro", cwd="/tmp/p")
        chat.post("@ds4pro a real ask", who="owner")
        before = len(seats._pending_all("main", "ds4pro", "s-drain",
                                        scan_lane="stop") or [])
        self.assertGreaterEqual(before, 1)
        chat.consume("main", chat.read("main")[1])      # exactly what read does
        after = len(seats._pending_all("main", "ds4pro", "s-drain",
                                       scan_lane="stop") or [])
        self.assertEqual(after, before, "read must not discharge an obligation")

    def test_no_surface_promises_that_read_catches_you_up(self):
        # THE WHOLE SEATS PACKAGE, not seats.py — and the split is what
        # taught this test what it was actually asserting. The invariant is
        # "no SURFACE promises that read catches you up"; pinning it to one
        # FILENAME measured where the sentence lived, not whether it was
        # right. When the beacon drain moved to seats_join.py the assertion
        # went green-then-red for a reason that had nothing to do with the
        # promise it guards.
        d = os.path.dirname(seats.__file__)
        names = sorted(f for f in os.listdir(d)
                       if f == "seats.py" or f.startswith("seats_"))
        # CONTROL: the scan sees the package at all. Without it, a renamed
        # directory makes both assertions below vacuously true.
        self.assertGreaterEqual(len(names), 3, "the seats package scan found "
                                               "almost nothing: %s" % names)
        src = "".join(open(os.path.join(d, f), encoding="utf-8").read()
                      for f in names)
        self.assertNotIn("helm chat read to catch up", src)
        # and the drain that DOES work is the one we name
        self.assertIn("catchup --including-mentions", src)


class NdpStopbookTest(SeatsBase):
    """The NON-DISTRACTION-PROTOCOL gate (owner canon 2026-08-03): a seat
    holding SOLO_LOAD_AT+ open leases with ZERO subagent delegation this
    session is BLOCKED once per load-state, not whispered at. The predicate
    (_solo_load_candidate) is unchanged from its one day as a whisper rung;
    the ESCALATION is the delivery — a gate owns its own emission, where the
    ladder gave the rung one shared slot second from the bottom, behind
    every rung that re-arms faster than it climbs.

    Two live calibration shapes, both measured 2026-08-05, are pinned here
    as arms: the TRUE POSITIVE (a wide queue held eight hours, zero
    subagent calls, three seats near-idle) and the TRUE NEGATIVE (a seat
    holding multiple lane leases that had spawned six delegates that same
    night — the gate must be silent for it, because a blocking rung that
    nags a head-down seat gets disabled by the first seat it wrongly blocks
    and then protects nobody).

    The ladder-order test died with the ladder seat: a gate rides the same
    one-exit-2 emission as every other block, so there is no single slot
    left for throughput to outrank or be outranked in.

    Every fixture identity here is synthetic — these tests are public-bound."""

    SID = "s-ndp"
    SEAT = "quill"

    def hold(self, n, session=None, seat=None, first=0):
        for i in range(first, n):
            ok, msg, _lease = seats.claim(
                "worktree:proj:lane-%d" % i, seat or self.SEAT, ttl=600,
                session=session or self.SID)
            self.assertTrue(ok, msg)

    def gate(self):
        return seats._ndp_gate(self.SID, "main", self.SEAT)

    def cand(self):
        return seats._solo_load_candidate(self.SID, self.SEAT)

    def guard(self, payload=None, args=()):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) \
            else payload
        return self.cmd("stop-guard", ["--hook-json", *args],
                        stdin=stdin or b"{}")

    def plant_red(self):
        p = os.path.join(record.session_dir(self.SID), "command-log.jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(json.dumps({"ts": 1, "token": "pytest", "exit": 1,
                                "digest": "aaa"}) + "\n")

    def activity(self, *keys):
        """One delegation-activity file at the EXACT path the rung scans,
        written through the module's own producers so a rename of either
        naming law fails this fixture instead of silently un-testing it."""
        path = seats._delegation_activity_path(self.SID, "worktree:proj:lane-0")
        base = os.path.basename(path)
        s_hash = seats.hashlib.sha256(self.SID.encode("utf-8")).hexdigest()
        self.assertTrue(base.startswith(".deleg_activity."), base)
        self.assertTrue(base.endswith("." + s_hash), base)
        chat._ensure_dir()
        pk.write_json(path, {"v": 2, "records": {k: {"source": "x"}
                                                for k in keys}})
        return path

    def test_true_positive_a_wide_solo_queue_blocks_with_the_protocol_named(self):
        # THE INCIDENT ARM, owner-measured 2026-08-03: a wide queue held 8h,
        # zero subagent calls, three seats near-idle. Driven END TO END
        # through the stop-guard verb, because the escalation under test is
        # the DELIVERY: this state used to be one whisper line that could
        # trail the state by hours; now it is a block on the stop where the
        # predicate holds.
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(3)
        rc, _o, err = self.guard({"session_id": self.SID})
        self.assertEqual(rc, 2, err)
        self.assertIn("NON-DISTRACTION PROTOCOL", err)
        self.assertIn("3 leases held, 0 subagents spawned this session", err)
        # THE CONCRETE VERBS, not an abstraction — and both of them, because
        # "delegate more" is advice a seat cannot act on at a Stop boundary.
        self.assertIn("spawn a subagent (Agent tool)", err)
        self.assertIn("helm dispatch send", err)
        # the WHY rides the block (a stopbook teaches or it nags)
        self.assertIn("HOT CONTEXT ON THE CRITICAL PATH", err)
        self.assertIn("HELM_STOP_GUARD_NDP=0", err)
        # LATCHED: the same load-state on the next stop passes. The absence
        # is read against a stop the guard provably spoke on — the lease
        # rung's latched warn and/or the beacon-arm line still print.
        rc2, _o2, err2 = self.guard({"session_id": self.SID})
        self.assertEqual(rc2, 0, err2)
        self.assertIn("[helm stop-guard]", err2)
        self.assertNotIn("NON-DISTRACTION", err2)

    def test_the_block_names_the_door_for_a_seat_told_not_to_delegate(self):
        # @kimi's FIX on this lane, and they CONSTRUCTED the shape rather than
        # arguing it: fresh session, three session-bound serial leases, no
        # tombstone -> the gate blocks. It is a TRUE positive of the predicate
        # and a FALSE one about the seat, because a standing operator
        # instruction not to delegate is not a fact helm keeps and the
        # predicate can never see it.
        #
        # THE CURE IS NOT AN EXEMPTION — there is nothing to key one on. It is
        # that the block must NAME the case and point at its own door, because
        # a rung that is wrong about you and offers no exit is a rung you
        # disable permanently, and a disabled rung protects nobody. The switch
        # already existed; the sentence telling a constrained seat it is THEIR
        # switch did not.
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(3)
        rc, _o, err = self.guard({"session_id": self.SID})
        self.assertEqual(rc, 2, err)                  # control: it DID block
        self.assertIn("NON-DISTRACTION PROTOCOL", err)
        self.assertIn("STANDING INSTRUCTION NOT TO DELEGATE", err)
        self.assertIn("HELM_STOP_GUARD_NDP=0", err)
        # AND THE ORDER OF AUTHORITY IS STATED, not implied: a throughput rung
        # must never read as licence to disobey the human who set the
        # constraint. This is the sentence a seat quotes back at its operator.
        self.assertIn("outranks it", err)

    def test_true_negative_a_delegating_multi_lease_holder_is_never_gated(self):
        # THE HEAD-DOWN ARM, measured live 2026-08-05 on the authoring seat
        # itself: multiple lane leases held AND six delegates spawned that
        # night. A blocking rung that fires on this shape gets disabled by
        # the first seat it wrongly blocks, and then it protects nobody.
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(4)
        self.plant_red()
        # ONE SubagentStop tombstone, written by the real producer.
        self.assertTrue(seats._mark_agent_stopped(self.SID, "sub-alpha"))
        # THE WIRE NAME, pinned to a literal on purpose. Reader and writer
        # both go through _STOPPED_PREFIX, so renaming that constant keeps
        # them agreeing with each other and disagreeing with the thousands of
        # tombstones a LIVE tmpfs already holds — every seat that had
        # delegated would start hearing this gate again mid-boot, and no
        # same-constant test can see it.
        self.assertTrue(os.path.basename(
            seats._delegation_stop_path(self.SID, "sub-alpha")).startswith(
                ".deleg_stopped."))
        self.assertTrue(seats._session_delegated(self.SID))
        self.assertEqual(self.gate(), (None, None))
        # …and the tombstone silenced ONLY this gate: the whisper ladder's
        # red-gate rung still stands for the same session.
        fps = [c[0] for c in seats._whisper_candidates(
            self.SID, self.SEAT, [], False)]
        self.assertTrue(any(f.startswith("redgate:") for f in fps), fps)
        # END TO END the absence is read against a stop that provably
        # enumerated these very leases — the claim-lease block names them.
        rc, _o, err = self.guard({"session_id": self.SID})
        self.assertEqual(rc, 2, err)
        self.assertIn("worktree:proj:lane-0", err)
        self.assertNotIn("NON-DISTRACTION", err)

    def test_the_gate_latches_per_state_and_a_doubled_queue_rearms(self):
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(3)
        b1, w1 = self.gate()
        self.assertIsNotNone(b1, "3 held leases + 0 subagents must gate")
        self.assertIsNone(w1)
        self.assertTrue(
            b1.startswith("[helm stop-guard] NON-DISTRACTION PROTOCOL"), b1)
        self.assertIn("3 leases held", b1)
        # the latch lives in its OWN lane — a kind collision with another
        # rung's latch would let that rung's state swallow this block
        self.assertTrue(os.path.exists(seats._stop_fp_path(
            "main", self.SEAT, self.SID, kind=seats.NDP_LATCH)))
        self.assertEqual(seats.NDP_LATCH, "stopndp")
        self.assertEqual(self.gate(), (None, None))    # latched: same state
        self.hold(4, first=3)                          # 3 -> 4 is a drift…
        self.assertEqual(self.gate(), (None, None))    # …never a re-nag
        self.hold(6, first=4)                          # doubled: new bucket
        b2, w2 = self.gate()
        self.assertIsNotNone(b2, "a doubled queue must re-arm the gate")
        self.assertIsNone(w2)
        self.assertIn("6 leases held", b2)
        # the compact line keeps its one-terminal-line discipline at any N
        # it re-fires with — it is also the degrade text (see the warn test)
        for cand in (seats._solo_load_candidate(self.SID, self.SEAT),):
            self.assertLessEqual(len(cand[1].encode("utf-8")),
                                 seats.STOP_WHISPER_CAP)
        # ONE delegation ends it outright — same answer at both altitudes
        self.assertTrue(seats._mark_agent_stopped(self.SID, "sub-omega"))
        self.assertIsNone(seats._solo_load_candidate(self.SID, self.SEAT))
        self.assertEqual(self.gate(), (None, None))

    def test_the_bucket_is_the_fp_and_doubles_before_it_moves(self):
        # the predicate's fp IS the latch key, so this progression is what
        # "a drift never re-nags, a doubling re-arms" reduces to
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(3)
        three = self.cand()
        self.assertIsNotNone(three, "3 held + 0 delegated must produce")
        self.assertEqual(three[0], "solo:1")
        self.hold(5, first=3)                 # 5 held — a drift, same bucket
        five = self.cand()
        self.assertIsNotNone(five)
        self.assertEqual(five[0], "solo:1")
        self.hold(6, first=5)                 # 6 held — doubled, new bucket
        six = self.cand()
        self.assertIsNotNone(six)
        self.assertEqual(six[0], "solo:2")

    def test_an_unwritable_latch_degrades_to_the_compact_warn(self):  # noqa: VACUOUS_ASSERTION — the block's absence (assertIsNone) is paired with an UNCONDITIONAL assertIsNotNone on the warn from the very same call: one (b, w) tuple cannot satisfy both arms vacuously
        # a gate that cannot remember may not block every stop forever —
        # the degrade is the whisper-strength line, and it repeats (a warn
        # cannot wall) until the latch dir heals
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(3)
        chat._ensure_dir()
        os.makedirs(seats._stop_fp_path("main", self.SEAT, self.SID,
                                        kind=seats.NDP_LATCH))
        b, w = self.gate()
        self.assertIsNone(b)
        self.assertIsNotNone(w, "unlatchable must degrade, not vanish")
        self.assertTrue(w.startswith("[helm stop-guard] "), w)
        self.assertIn("spawn a subagent (Agent tool)", w)
        self.assertIn("helm dispatch send", w)
        # …and it STAYS a warn while the latch stays unwritable — a warn
        # cannot wall, so repeating is the correct degraded behavior
        b2, w2 = self.gate()
        self.assertIsNone(b2)
        self.assertEqual(w2, w)
        # END TO END the degrade must actually reach a terminal. Warns ride
        # the ALLOW exit only, so the first stop (lease block, exit 2)
        # discards it; the second stop compresses the lease rung to its
        # latched warn and the NDP degrade line prints beside it.
        rc, _o, err = self.guard({"session_id": self.SID})
        self.assertEqual(rc, 2, err)
        rc2, _o2, err2 = self.guard({"session_id": self.SID})
        self.assertEqual(rc2, 0, err2)
        self.assertIn("3 leases held, 0 subagents spawned this session", err2)

    def test_the_kill_switch_silences_the_gate_before_it_latches(self):  # noqa: VACUOUS_ASSERTION — the switched-off (None, None) is asserted only AFTER an unconditional assertIsNotNone proves the same gate blocks with the switch lifted, so a dead gate fails the control before the absence can pass
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(3)
        os.environ["HELM_STOP_GUARD_NDP"] = "0"
        switched = self.gate()
        os.environ.pop("HELM_STOP_GUARD_NDP")
        # the silence is the SWITCH, not a dead predicate: the same state
        # gates the moment the switch lifts (the unconditional positive
        # control on the same observable) — which also proves the
        # switched-off path never wrote the latch
        b, _w = self.gate()
        self.assertIsNotNone(b, "the predicate must be live under the switch")
        self.assertEqual(switched, (None, None))

    def test_the_whisper_ladder_no_longer_speaks_the_solo_rung(self):  # noqa: VACUOUS_ASSERTION — the absence (no solo: fp) is read off a list proved non-empty by an unconditional redgate assertTrue in the same pass, and an unconditional assertIsNotNone proves the SAME state blocks at the gate: the rung moved, it did not vanish
        # one predicate, ONE mouth — a ladder seat beside the gate would
        # double-speak the state and hold a second latch that drifts
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(3)
        self.plant_red()
        fps = [c[0] for c in seats._whisper_candidates(
            self.SID, self.SEAT, [], False)]
        # positive control: the enumeration provably ran and found the red
        # gate, so the absence below is a silenced RUNG, not an empty list
        self.assertTrue(any(f.startswith("redgate:") for f in fps), fps)
        self.assertFalse(any(f.startswith("solo:") for f in fps), fps)
        # …and the rung MOVED rather than vanished: the same state blocks
        b, _w = self.gate()
        self.assertIsNotNone(b, "the state absent from the ladder must gate")

    def test_a_live_scan_child_is_not_a_delegate_but_a_subagent_record_is(self):  # noqa: VACUOUS_ASSERTION — flip-the-variable: an unconditional assertIsNotNone on the SAME observable (the candidate) runs BEFORE the absence, so the final assertIsNone can only be satisfied by the post_tool_use record it plants, never by a dead enumeration
        # MEASURED ON THE LIVE HOST 2026-08-05: 13 of 14 delegation-activity
        # files carried `live_scan` and nothing else. live_scan is ANY
        # same-uid process whose cwd is the claimed room — a Bash-tool pytest
        # qualifies — so counting the FILE would read a build as a delegate
        # and silence this gate on exactly the seat it exists for.
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(3)
        path = self.activity(seats._activity_record_key("live_scan"))
        self.assertTrue(os.path.exists(path))
        got = self.cand()
        self.assertIsNotNone(got, "a live_scan child is no delegate")
        self.assertEqual(got[0], "solo:1")
        # the SAME file with a documented-subagent record does silence it
        self.activity(seats._activity_record_key("live_scan"),
                      seats._activity_record_key("post_tool_use", "sub-beta"))
        self.assertTrue(seats._session_delegated(self.SID))
        self.assertIsNone(self.cand())

    def test_two_leases_is_under_the_threshold(self):  # noqa: VACUOUS_ASSERTION — the MUST-HIT control is the third lease added AFTER the absence: the same probe on the same claims file goes None -> not-None in one test, so a dead probe fails on the second arm
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(2)
        self.assertIsNone(self.cand())
        self.assertEqual(self.gate(), (None, None))
        self.hold(3, first=2)                 # MUST-HIT: the probe can see one
        self.assertIsNotNone(self.cand())

    def test_unreadable_claims_are_unknown_and_stay_silent(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertIsNotNone on the SAME observable runs before the corruption, so the absence after it measures the corrupted read, not a dead probe
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(3)
        self.assertIsNotNone(self.cand())     # MUST-HIT before the negative
        with open(seats.claims_path(), "w") as f:
            f.write("{not json")
        # UNKNOWN obligations may not accuse a seat of hoarding them
        self.assertIsNone(self.cand())
        self.assertEqual(self.gate(), (None, None))

    def test_a_receiver_counts_leases_connected_by_holder_not_session(self):
        # a claim-on-behalf RECEIVER holds leases minted by ANOTHER session,
        # connected to it by HOLDER — a handed lease is still a lease you
        # hold, and the receiver carrying three alone must hear the gate.
        # The premise is stated POSITIVELY — the minting session is named and
        # the holder rows are enumerated — because "this session holds
        # nothing" is a fact three unrelated fixture bugs would also produce.
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(3, session="s-other")
        rows = seats._live_claims()
        self.assertEqual(sorted((str(v.get("session")), str(v.get("holder")))
                                for r, v in rows.items() if r != "_fence"),
                         [("s-other", self.SEAT)] * 3)
        self.assertTrue(seats._session_holds_claim("s-other"))
        got = self.cand()
        self.assertIsNotNone(got, "a handed lease is still a lease you hold")
        self.assertIn("3 leases held", got[1])
        b, _w = self.gate()
        self.assertIsNotNone(b, "the receiver shape must reach the block")


if __name__ == "__main__":
    unittest.main()
