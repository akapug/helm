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

from tests._tmphome import declaring as _tmp_declaring  # noqa: E402
from tests._tmphome import session_for as _tmp_session_for  # noqa: E402
from tests._tmphome import declare as _tmp_declare  # noqa: E402
from tests._tmphome import corroborate as _tmp_corroborate  # noqa: E402

from helm import (beacons, chat, dispatches, eventledger, home, pk, proxywatch,
                  record, seats, seats_cursor, seats_delivery, seats_receipts,
                  seats_identity, seats_rename, seats_stop_claims,
                  seats_stop_guard, seats_stop_seam, seats_stop_signals,
                  web)  # noqa: E402
from helm import seats_roster as seatmod_roster  # noqa: E402
# THE FACADE, IMPORTED FOR THE CONTRACT AND NOT FOR A CALLER. Nothing in this
# file names `seatmod` any more -- the spawn-register arms that did moved to
# tests/test_seats_rename.py -- but `from helm import seat_lifecycle` below is
# an IMPL import, and tests/test_seat_facade_injection.py requires a helm.seat
# import in the same or an enclosing scope for every one of those. Pruning this
# as unused is what turned that audit red on the whole-suite gate.
from helm import seat as seatmod  # noqa: E402,F401
# The identity-stamp route's three r7 findings are about what the REBIND
# VERB REPORTS, so the arms drive the CLI and the generation owner rather
# than the migration alone.
from helm import seat_lifecycle  # noqa: E402
from helm import seats_incarnation as seatmod_incarnation  # noqa: E402
from helm import seats_stop_timing  # noqa: E402

# Classes this module HANDED AWAY, read by `helm/retired_name_rung.py`.
#
# That rung judges a diff PER FILE: a `-` line removing a top-level class with
# no `+` line adding it back IN THE SAME FILE reads as a retirement, and a move
# between two files in one commit is exactly the shape a size ceiling forces.
# The declaration is the cure it offers, and it is not clearance by itself --
# the rung also requires the named satellite to define the name at column zero,
# so this table cannot vouch for a class nobody wrote.
#
# NO SETATTR REPUBLICATION HERE, AND THAT IS THE DIFFERENCE FROM `helm/`.
# When a production module sheds a name, its consumers still spell
# `owner.NAME`, so the owner must bind it back or every one of them dangles.
# A moved TEST CLASS has no such consumer -- measured, the only spelling of any
# of these names outside this file that a reader RESOLVES is
# `tests/falsify_2463.py`, whose dotted test id moved with the class; every
# other hit is prose in a comment. Binding them back here would be a live
# defect rather than a courtesy: `unittest` collects by walking module
# attributes, so each class would be found twice, once under each module, and
# its arms would run twice under two names.
_OWNER_NAMES = (
    ("test_seats_stop_guard", ("StopGuardTest", "GuardrailTextTrim692Test",
                               "StopGuardGatePendingTest",
                               "StopGuardExemptStateIsPrintedTruthTest",
                               "StopGuardRenewingLeaseTest")),
    ("test_seats_stop_delegation", ("StopGuardDelegationTest",
                                    "DelegationHolderAncestryTest")),
    ("test_seats_stop_room_unfinished", ("StopGuardRoomUnfinishedTest",)),
    ("test_seats_rename", ("RenameTest", "RenameAliasTest",
                           "RenameSpawnRegisterTest")),
    ("test_seats_final_capture", ("_ActPane",
                                  "TheFinalCaptureIsTheLastWordTest")),
)

THREAD_TIMEOUT = 2.0

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_EVENT_DIR", "MELD_CHAT_EVENT_DIR",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_DELIVER",
            "HELM_STOP_TIMING_AFTER",
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
        # THE RUNG TRACE IS BUFFERED IN PRODUCTION and speaks only once a
        # ladder is in danger (seats_stop_timing). Several arms in this module
        # are ABOUT the trace's content, so the module asks for it: 0 streams
        # from the first line. `reset` is what keeps one arm's ladder out of
        # the next one — the state is per-ladder and a suite is one process.
        # The BUFFERING is measured in tests/test_hook_wrapper.py, and the
        # message budget a blocked stop must fit in test_stop_lease_latch.py.
        os.environ["HELM_STOP_TIMING_AFTER"] = "0"
        seats_stop_timing.reset()
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
        os.environ["HELM_CHAT_EVENT_DIR"] = os.path.join(
            self.tmp, "chat-events")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"
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
        # THE STOP GUARD READS A RESIDENT'S FACTS, and a test process has no
        # resident: this stands one in that is exactly up to date at every
        # stop (tests/_stopfacts.py). Freshness is tests/test_stopfacts.py's.
        from tests._stopfacts import always_fresh
        self.fresh_resident = always_fresh(self)

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        seats_stop_timing.reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cmd(self, verb, args=(), stdin=None, room="main"):
        """Plain-verb runner (print-based legs).

        A CONSUMING VERB THAT NAMES A SEAT DECLARES IT (task/994). `wait` and
        `deliver` drain that seat's cursor and beat its presence, so naming one
        is either "I am it" or an on-behalf-of act, and the fail-open that used
        to let an unnamed process do it silently is retired. A pane waits for
        its OWN mail with HELM_CHAT_NAME exported; these fixtures now model
        that instead of the shape no live seat has.

        Only the consuming verbs, and only when `--seat` is present: every
        other verb keeps whatever identity its own arm set up, so an arm about
        the unnamed case still gets one.
        """
        out, err = io.StringIO(), io.StringIO()
        fake = types.SimpleNamespace(buffer=io.BytesIO(stdin)) if stdin is not None else None
        ctx = mock.patch.object(sys, "stdin", fake) if fake else contextlib.nullcontext()
        seat_ctx = (_tmp_declaring(args)
                    if verb in ("wait", "deliver") and "--seat" in list(args)
                    else contextlib.nullcontext())
        with seat_ctx, ctx, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
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
        self.assertFalse(seats.deliverable({"ts": "t", "from": "daria",
                                            "react": "🔥", "tts": "t", "tfrom": "x"},
                                           "alice"))
        self.assertFalse(seats.deliverable(row("x", "ping @alice-2"), "alice"))
        for near in ("@alloy", "@fleet-2", "@everyone_else"):
            self.assertFalse(seats.deliverable(row("x", near), "alice"), near)

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
            row("daria", "@alice go", origin="web"), "alice"),
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
        row = lambda **kw: dict({"ts": "t", "from": "daria",
                                 "text": "no mention"}, **kw)
        self.assertFalse(seats.deliverable(row(), "alice"))              # spoofable CLI
        self.assertFalse(seats.deliverable(row(origin="web"), "alice"))  # owner rail: no wake now
        self.assertFalse(seats.deliverable(row(origin="tui"), "alice"))
        self.assertFalse(seats.deliverable(row(origin="cli"), "alice"))
        # an owner post that @mentions the seat still wakes it
        self.assertTrue(seats.deliverable(
            {"ts": "t", "from": "daria", "text": "@alice go", "origin": "web"},
            "alice"))

    def test_a_captured_None_cwd_is_an_ANSWER_not_a_request_to_resample(self):
        """OMITTED AND None ARE DIFFERENT ANSWERS (the exact probe).

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
        # this box happens to carry. The first version sampled the
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
    then GIT_DIR. Review found each in turn, and the fourth finding ended the
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
        with mock.patch("helm.vcs.backend", return_value=be("Daria Quill")):
            self.assertEqual(seats._git_owner_handle(), "daria")
        with mock.patch("helm.vcs.backend", return_value=be(None)):
            self.assertIsNone(seats._git_owner_handle())
        with mock.patch("helm.vcs.backend", return_value=be("@@!! zap")):
            self.assertIsNone(seats._git_owner_handle())  # not handle-shaped

    def test_seam_posting_default_is_recognized(self):
        """THE COUPLING INVARIANT: whatever the owner surfaces post under by
        default, the rails RECOGNIZE — through ONE resolver on both sides.
        Derivation mocked to a name that is neither 'daria' nor the login, so
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
        report = os.environ.get("SUITE_SEAT_AUTHORITY_REPORT")
        local = os.environ.get("SUITE_SEAT_AUTHORITY_LOCAL_OVERRIDE")
        if local:
            # The subprocess isolation arm plants this NON-HELM variable in its
            # parent, then chooses the real override here AFTER tests/__init__.py
            # has scrubbed inherited authority paths and completed bootstrap.
            os.environ["HELM_SEAT_NAMES"] = local
        if report:
            from helm import seatname_guard
            authority_path, authority_invalid = seatname_guard.authority_path()
            authority_existed = bool(
                authority_path and os.path.exists(authority_path))

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
        delivered = seats.deliver(seat="alice")
        self.assertIn("early word", delivered)

        if report:
            authority, authority_error = seatname_guard.read_authority(
                authority_path)
            with open(report, "w", encoding="utf-8") as fh:
                json.dump({
                    "authority": authority_path,
                    "authority_error": authority_error,
                    "authority_existed_before_join": authority_existed,
                    "authority_invalid": authority_invalid,
                    "authority_names": sorted(authority.names()),
                    "delivered": delivered,
                    "helm_seat_names": os.environ.get("HELM_SEAT_NAMES"),
                    "meld_seat_names": os.environ.get("MELD_SEAT_NAMES"),
                    "roster_row": row,
                    "seat": seat,
                }, fh, sort_keys=True)

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
        self.assertIn("First action", line)
        # the exact beacon-arm command carries the RESOLVED seat name + --follow
        from helm.seats_common import GUIDE_PATH
        self.assertIn(GUIDE_PATH, line, "the re-arm route is the guide")
        self.assertIn("Monitor(", line)
        self.assertIn("timeout_ms: 1800000", line)
        self.assertIn("expires every 30 minutes", line)
        # the honest enforcement note: nothing external can wake a PTY agent
        from helm.seats_common import GUIDE_PATH
        self.assertIn(GUIDE_PATH, line)

    def test_only_a_FOLLOWED_waiter_counts_as_coverage(self):
        """AN INCUMBENT IS NOT A BEACON, and reading it as one is FALSE
        ASSURANCE — the defect a review found in the first cut of this lane.
        `_one_live_incumbent` answers "ONE live committed waiter serves this
        (seat, session)", and it admits an UNREGISTERED SINGLE-SHOT `chat
        wait` through its unregistered path. Production's own words are that
        a single-shot is a DELIVERY, not a beacon: it stops waiting once it
        delivers, so telling a seat it is covered by one is telling it it is
        covered by something that is about to stop.

        `--any` IS THE FLAG THAT LOOKS LIKE WIDER COVERAGE AND IS NOT, and it
        is excluded for a reason the wait loop states twice in its own words:
        "--any never filters at all", "--any stays one room's tap", and it
        "WATCHES A ROOM AND TOUCHES NO CURSOR". A room watch is a DIFFERENT
        INSTRUMENT, not a superset of the seat's own delivery. I argued the
        opposite in the meld and the source refuted me.

        The contract, agreed in review: covered iff one
        live committed same-seat same-session waiter AND a READABLE spec AND
        follow is True AND any is False."""
        sid = "11111111-2222-3333-4444-555555555555"

        def incumbent(spec, sink=beacons.SINK_ADMISSIBLE):
            return lambda *_a, **_kw: {"pid": 4242, "seat": "codex",
                                       "session": sid, "waiter": spec,
                                       "sink": sink}

        # UNCONDITIONAL POSITIVE FIRST, on the same observable: the plain
        # followed beacon DOES suppress the directive, so neither loop below
        # can satisfy this arm by refusing everything or by being empty.
        with mock.patch.object(beacons, "_one_live_incumbent",
                               incumbent({"follow": True, "any": False})):
            _seat, base = seats.join(seat="codex", session=sid, cwd="/tmp/p",
                                     session_source="compact")
        self.assertIn("already armed", base)
        self.assertNotIn("First action", base)

        # THE DECIDING NEGATIVE, and it is the review's witness: a single-shot
        # waiter is a delivery, so the directive must stand.
        for spec, why in (
                ({"follow": False, "any": False}, "single-shot is a delivery"),
                ({"follow": True, "any": True}, "--any is a room watch"),
                ({"follow": False, "any": True}, "neither"),
                (None, "spec unreadable — waiter_spec answers None"),
                ("not-a-dict", "spec is not the shape this reads"),
                ({}, "spec carries neither flag")):
            with mock.patch.object(beacons, "_one_live_incumbent",
                                   incumbent(spec)):
                _seat, line = seats.join(seat="codex", session=sid,
                                         cwd="/tmp/p",
                                         session_source="compact")
            self.assertIn("First action", line, why)
            self.assertNotIn("already armed", line, why)

        # THE POSITIVE POLES, or a predicate that refused everything would
        # satisfy the six above. TIMEOUT IS NOT CONSULTED: a finite followed
        # beacon is truthful point-in-time coverage, and every armed Monitor
        # on this fleet is finite and re-arms. ROOM IS NOT CONSULTED: a
        # seat-follow delivery wakes on DMs and mentions from ANY room.
        for spec, why in (
                ({"follow": True, "any": False}, "the plain beacon"),
                ({"follow": True, "any": False, "timeout": 1800.0},
                 "FINITE follow is still coverage"),
                ({"follow": True, "any": False, "ambient": True},
                 "ambient follow is still coverage"),
                ({"follow": True, "any": False, "room": "another-room"},
                 "a beacon on a DIFFERENT room still covers the seat")):
            with mock.patch.object(beacons, "_one_live_incumbent",
                                   incumbent(spec)):
                _seat, line = seats.join(seat="codex", session=sid,
                                         cwd="/tmp/p",
                                         session_source="compact")
            self.assertIn("already armed", line, why)
            self.assertNotIn("First action", line, why)

    def test_a_waiter_whose_WAKES_GO_NOWHERE_is_not_coverage(self):
        """R1. THE SPEC CANNOT SEE WHERE THE WAKES GO — it is
        rebuilt from ARGV, and a redirection is not in argv, because the shell
        consumes `> file` before the exec. So a waiter can be LIVE, followed,
        not --any, on this exact seat and session, and still have fd 1 pointed
        at a file: it consumes every addressed row and produces no wake. The
        banner then said "re-arm only if that waiter dies", about a process
        that never dies. False assurance in precisely the state that needs
        repair.

        THE SINK MUST BE ADMISSIBLE, NOT MERELY NOT-REFUTED, and the UNKNOWN
        and missing-key poles below are why: unreadable must keep the
        directive, like every other unknown on this path. A seat wrongly told
        to arm loses one tool call; a seat wrongly told it is covered goes
        deaf until somebody notices."""
        sid = "11111111-2222-3333-4444-555555555555"
        beacon = {"follow": True, "any": False}

        def incumbent(sink):
            return lambda *_a, **_kw: {"pid": 4242, "seat": "codex",
                                       "session": sid, "waiter": beacon,
                                       "sink": sink}

        # UNCONDITIONAL POSITIVE FIRST, so no loop below can pass by refusing
        # everything: the same beacon on an admissible sink IS coverage.
        with mock.patch.object(beacons, "_one_live_incumbent",
                               incumbent(beacons.SINK_ADMISSIBLE)):
            _seat, base = seats.join(seat="codex", session=sid, cwd="/tmp/p",
                                     session_source="compact")
        self.assertIn("already armed", base)

        for sink, why in (
                (beacons.SINK_REFUTES, "stdout on a file or /dev/null wakes nobody"),
                (beacons.SINK_UNKNOWN, "an unreadable sink keeps the directive"),
                (None, "an incumbent carrying no sink key at all"),
                ("", "an empty sink value is not an admissible one"),
                ("invented", "an unrecognised sink value is not admissible")):
            with mock.patch.object(beacons, "_one_live_incumbent",
                                   incumbent(sink)):
                _seat, line = seats.join(seat="codex", session=sid,
                                         cwd="/tmp/p",
                                         session_source="compact")
            self.assertIn("First action", line, why)
            self.assertNotIn("already armed", line, why)

    def test_the_covered_banner_states_the_bound_it_actually_proved(self):
        """AN ADMISSIBLE SINK IS NECESSARY, NOT SUFFICIENT, and the banner is
        the only place that bound can reach the one actor who can act on it.
        A socket proves the sink is not a file; it does NOT prove anything
        still holds the far end. So the banner must not say "re-arm only if
        that waiter dies" — a waiter on a dead socket satisfies that condition
        forever. It names the observable the seat can actually check: being
        addressed and not waking."""
        sid = "11111111-2222-3333-4444-555555555555"
        with mock.patch.object(
                beacons, "_one_live_incumbent",
                lambda *_a, **_kw: {"pid": 4242, "seat": "codex",
                                    "session": sid,
                                    "waiter": {"follow": True, "any": False},
                                    "sink": beacons.SINK_ADMISSIBLE}):
            _seat, line = seats.join(seat="codex", session=sid, cwd="/tmp/p",
                                     session_source="compact")
        self.assertIn("already armed", line)
        self.assertIn("addressed and do not wake", line)
        self.assertNotIn("Re-arm only if that waiter dies", line)

    def test_the_real_waiter_spec_carries_the_two_flags_this_reads(self):
        """THE CONTRACT IS WITH THE REAL PRODUCER, not with my fixture's idea
        of it. Every arm above hands the join a hand-built spec dict, so all
        of them would keep passing if `waiter_spec` stopped emitting these
        keys — the predicate would then read None for both and refuse every
        real beacon, silently, with a green suite.

        So this asserts the PRODUCER's own shape: the canonical spec for a
        requested waiter carries `follow` and `any` as keys."""
        spec = beacons.requested_waiter_spec()
        self.assertIn("follow", spec)
        self.assertIn("any", spec)
        self.assertTrue(spec["follow"], "a requested waiter follows")
        self.assertFalse(spec["any"], "and is not a room watch")

    def test_a_COMPACT_covered_by_a_live_beacon_is_not_told_to_arm_one(self):
        """A COMPACTING SUBAGENT SHARES ITS MAIN SESSION, so it reaches this
        banner already holding main's live beacon and is handed MANDATORY
        FIRST ACTION anyway. It cannot comply — the arm guard refuses a
        sidechain — so the directive is noise at the top of the one context
        where noise costs most, and it teaches a seat that helm's own
        MANDATORY can be ignored.

        THE SIDECHAIN BRANCH CANNOT ANSWER THIS: it keys on an agent id, and
        the harness's compact payload carries none. COVERAGE is readable where
        identity is not."""
        sid = "11111111-2222-3333-4444-555555555555"
        seen = []

        def covered(seat, session, *a, **kw):
            seen.append((seat, session))
            return {"pid": 4242, "seat": seat, "session": session,
                    "waiter": {"follow": True, "any": False},
                    "sink": beacons.SINK_ADMISSIBLE}

        with mock.patch.object(beacons, "_one_live_incumbent", covered):
            _seat, line = seats.join(seat="codex", session=sid, cwd="/tmp/p",
                                     session_source="compact")
        self.assertEqual([("codex", sid)], seen,
                         "MUST-HIT: the coverage probe was actually reached")
        self.assertNotIn("First action", line)
        self.assertIn("already armed", line)
        self.assertIn("4242", line, "it names the waiter it found")
        self.assertIn("nothing is owed", line)
        # AND IT STAYS USEFUL: identity, the catch-up verb, and a ROUTE to
        # re-arming if that waiter dies. A banner that only says "do nothing"
        # is worse than the directive it replaced.
        #
        # THE ROUTE IS THE GUIDE, NOT THE ARGV REPEATED. This seat holds a
        # live beacon; the arming command is what the OTHER banner exists to
        # deliver, and a seat whose beacon has since died is no longer
        # covered and gets that banner on its next start. Spending the bytes
        # here is the shape that made the armed banner longer than the one
        # asking for work.
        from helm.seats_common import GUIDE_PATH
        self.assertIn("seat 'codex'", line)
        self.assertIn("helm chat read", line)
        self.assertIn("re-arm", line)
        self.assertIn(GUIDE_PATH, line)

    def test_a_compact_NOT_covered_still_gets_the_MANDATORY_directive(self):
        """THE OPPOSITE POLE, and the fail direction that matters. A seat
        wrongly told to arm loses one tool call; a seat wrongly told it is
        covered goes DEAF. `_one_live_incumbent` answers None for every
        unreadable case — an unlistable process table, a GHOST or UNKNOWN
        waiter, a different session, two live waiters — so UNKNOWN keeps the
        directive by construction."""
        sid = "11111111-2222-3333-4444-555555555555"
        for answer in (None, False):
            with mock.patch.object(beacons, "_one_live_incumbent",
                                   lambda *a, **kw: answer):
                _seat, line = seats.join(seat="codex", session=sid,
                                         cwd="/tmp/p",
                                         session_source="compact")
            self.assertIn("First action", line, repr(answer))
            from helm.seats_common import GUIDE_PATH
        self.assertIn(GUIDE_PATH, line)
        # AND A RAISING PROBE IS NOT COVERAGE EITHER: an exception is the most
        # unreadable answer there is.
        def boom(*_a, **_kw):
            raise RuntimeError("process table unreadable")
        with mock.patch.object(beacons, "_one_live_incumbent", boom):
            _seat, line = seats.join(seat="codex", session=sid, cwd="/tmp/p",
                                     session_source="compact")
        self.assertIn("First action", line)

    def test_a_FRESH_start_never_pays_for_the_coverage_probe(self):  # noqa: VACUOUS_ASSERTION — the call RESULT is asserted, just not
        # through the spy's return: the double answers COVERED, and the
        # banner still carries MANDATORY FIRST ACTION, which is only
        # possible if the probe was never consulted. The empty call list
        # and the MANDATORY assertion are two readings of one fact, and
        # the compact case below proves the same double DOES get called.
        """THE COST BOUND, pinned as behaviour rather than left in a comment.
        The probe reads /proc and the credential homes — 43ms median against an
        empty registry when this was written — and EVERY seat join renders this
        banner. A fresh SessionStart cannot have a live beacon for its own
        session, because the session is new, so the probe buys nothing there
        and is not run."""
        sid = "11111111-2222-3333-4444-555555555555"
        calls = []

        def counted(*a, **kw):
            calls.append(a)
            return {"pid": 1, "seat": "codex", "session": sid,
                    "waiter": {"follow": True, "any": False}}

        for source in (None, "startup", "resume", "clear"):
            with mock.patch.object(beacons, "_one_live_incumbent", counted):
                _seat, line = seats.join(seat="codex", session=sid,
                                         cwd="/tmp/p", session_source=source)
            self.assertIn("First action", line, repr(source))
        self.assertEqual([], calls,
                         "a non-compact arrival must not pay for the probe")
        # MUST-HIT beside it: the SAME double under "compact" IS called, or
        # this arm would pass against a probe that is never wired at all.
        with mock.patch.object(beacons, "_one_live_incumbent", counted):
            seats.join(seat="codex", session=sid, cwd="/tmp/p",
                       session_source="compact")
        self.assertEqual(1, len(calls),
                         "the compact arrival DOES reach the probe")

    def test_seat_joins_roster_under_its_family_name(self):
        # launch_line exports HELM_CHAT_NAME=<family>; the join hook keys the
        # roster on it (derive_seat) — so @codex reaches the seat, not agent-xxxx
        os.environ["HELM_CHAT_NAME"] = "codex"
        os.environ["CLAUDE_SESSION_ID"] = "sess-abcdef12"
        seat, _line = seats.join(cwd="/tmp/p")     # no explicit --seat
        self.assertEqual(seat, "codex")
        self.assertIn("codex", seats.roster())
        self.assertNotIn("agent-sess-abc", " ".join(seats.roster()))


class JoinHookSidechainTest(SeatsBase):
    """task/2542 door (2): the join hook must not tell a subagent it is the seat.

    THE FAILURE MODE (task/2542, measured on a live seat): a background
    subagent auto-compacts, this hook fires inside the sidechain, and its
    banner ("seat X in room R ... First action: arm your idle-wake
    beacon") is the subagent's first context after compaction. The subagent
    arms a Monitor, then re-arms with --replace and takes the seat's wake
    route.

    THE FIELD IS UNMEASURED FOR THIS EVENT. The bytes of Claude Code 2.1.270
    build the SessionStart payload without agent_id, from a subagent and from
    the main thread alike. So this door is keyed on the DOCUMENTED field
    (agent_id: present only inside a subagent) and the payload without it
    must produce exactly today's bytes."""

    SID = "s-side"

    def payload(self, **extra):
        d = {"hook_event_name": "SessionStart", "source": "compact",
             "session_id": self.SID, "cwd": self.tmp,
             "transcript_path": os.path.join(self.tmp, self.SID + ".jsonl")}
        d.update(extra)
        return json.dumps(d).encode()

    def test_a_sidechain_session_start_is_told_it_is_not_the_seat(self):  # noqa: VACUOUS_ASSERTION — no roster row is the door; the byte-identical twin below proves the same lookup finds the main thread's join
        os.environ["HELM_CHAT_NAME"] = "zed"
        rc, out = self.cmd_fd("join", ["--hook-json"],
                              stdin=self.payload(agent_id="a1b2",
                                                 agent_type="general-purpose"))
        self.assertEqual(rc, 0)
        hso = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        text = hso["additionalContext"]
        self.assertIn("you are a subagent inside seat 'zed'", text)
        self.assertIn("never arm, replace or stop a helm chat wait beacon",
                      text)
        self.assertIn("report to your parent", text)
        self.assertNotIn("First action", text)
        self.assertNotIn("Monitor(", text)
        # the sidechain registers nothing in the seat's name either: the
        # session is the main thread's, and its own join already did that
        self.assertIsNone(seats.seat_for_session(self.SID))

    def test_without_agent_id_the_banner_is_byte_identical(self):
        """THE CONTROL. The hook output for a payload with no agent_id (and
        with an empty one: missing evidence is not a sidechain) must be the
        exact bytes the unchanged join produces for the same session."""
        os.environ["HELM_CHAT_NAME"] = "zed"
        rc, out = self.cmd_fd("join", ["--hook-json"], stdin=self.payload())
        rc2, out2 = self.cmd_fd("join", ["--hook-json"],
                                stdin=self.payload(agent_id=""))
        _seat, line = seats.join(session=self.SID, cwd=self.tmp, seat=None,
                                 room="main", room_explicit=False,
                                 room_source=None, session_source="compact")
        self.assertIn("First action", line)
        expected = json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": line}}) + "\n"
        self.assertEqual((rc, out), (0, expected))
        self.assertEqual((rc2, out2), (0, expected))
        self.assertEqual(seats.seat_for_session(self.SID), "zed")

    def test_a_compact_join_reaches_NO_CREDENTIAL_HOME_when_nothing_is_live(self):
        """N1, and it is a class I cured one lane earlier at a
        different door.

        `_one_live_incumbent` fetched `live_sessions()` UNCONDITIONALLY, above
        the loop that is its ONLY consumer. That reaches `sessions.cred_homes`,
        which expands `~` and opens session records under the operator's REAL
        `~/.claude`, `~/.claude-homes` and `~/.helm/_global/seats/*`.
        `SeatsBase` plants HELM_HOME and HELM_PROC but does NOT replace HOME,
        so adding the compact branch silently gave every unmocked join control
        in this class a transitive read of operator credential homes. The six
        mocked poles could not see it: they replace the helper that sits ABOVE
        this one, so the reach happens only on the paths they do not take.

        THE TRIPWIRE RECORDS AND DOES NOT RAISE. `live_sessions` wraps its
        probe in a broad `except Exception`, so an AssertionError raised in
        here is SWALLOWED and the arm could never go red. I learned that on
        task/2549, where a raising tripwire was structurally incapable of
        failing and I read its silence as a pass.

        IT ALSO RETURNS AN EMPTY LIST RATHER THAN DELEGATING, so that even
        when this arm FAILS it has not itself read the operator's homes.

        THE MUST-HIT AT THE END IS NOT DECORATION: a tripwire wired to nothing
        reports exactly the same clean silence as a cured call path."""
        from helm import sessions as sessions_mod
        from helm import beacons as beacons_mod

        seen = []

        def recording_cred_homes():
            seen.append(True)
            return []

        # THE SECOND SEAM IS CONTROLLED TOO, AND NOT AS BELT-AND-BRACES.
        # `beacons.live_sessions` is left replaced by a MagicMock by an
        # earlier module in this suite, so in DISCOVERY ORDER the wrapper
        # never reaches `cred_homes` at all — and this arm would then pass
        # against the UNCURED eager fetch, proving nothing. The must-hit
        # caught exactly that. Pinning the wrapper to its real body makes the
        # arm's verdict independent of module order; without it the control
        # is vacuous whenever test_beacons has run first.
        def real_live_sessions():
            try:
                return dict(sessions_mod.live_sids())
            except Exception:                        # noqa: BLE001 — as shipped
                return None

        sid = "3c3c3c3c-4d4d-5e5e-6f6f-707070707070"
        with mock.patch.object(sessions_mod, "cred_homes", recording_cred_homes), \
                mock.patch.object(beacons_mod, "live_sessions",
                                  real_live_sessions):
            _seat, line = seats.join(session=sid, cwd=self.tmp, seat="zed",
                                     room="main", room_explicit=False,
                                     room_source=None, session_source="compact")
            self.assertEqual(
                seen, [],
                "a compact join with no live candidate reached credential-home "
                "discovery; the fixture's HOME is not the operator's")
            # MUST-HIT. Without this the arm above passes for a tripwire that
            # was never connected to the discovery path at all.
            beacons_mod.live_sessions()
            self.assertTrue(
                seen,
                "the tripwire never fires, so its silence above proves nothing")
        # and the join still did its job on the way past
        self.assertIn("First action", line)


class DeliverTest(SeatsBase):
    def seat_up(self, seat="alice"):
        seats.join(session="s-" + seat, cwd="/tmp/p", seat=seat)

    @contextlib.contextmanager
    def incomplete_cursor_commit(self):
        real_write, calls = seats_cursor._durable_text, []

        def fail_after_first(path, raw):
            calls.append(path)
            if len(calls) >= 2:
                raise OSError("cursor write/rollback failed")
            return real_write(path, raw)

        with mock.patch("helm.seats_cursor._durable_text",
                        side_effect=fail_after_first):
            yield

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
        chat.post("course correction", who="daria", origin="web")
        self.assertIsNone(seats.deliver(seat="alice"))
        chat.post("@alice course correction", who="daria", origin="web")
        self.assertIn("course correction", seats.deliver(seat="alice"))

    def test_cli_owner_name_does_not_owner_deliver(self):
        self.seat_up()
        chat.post("i am totally the owner", who="daria")   # no rail stamp
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

    def test_addressing_only_session_never_inherits_standing_runtime(self):  # noqa: VACUOUS_ASSERTION — the transient sid is positively retained for addressing before exact runtime absence is asserted
        seats.write_roster("shared", runtime={
            "family": "codex", "agent_harness": "claude", "backend": "proxy"})
        seats.write_roster("shared", session="transient-hook", identity=False)
        row = seats.roster()["shared"]
        self.assertIn("transient-hook", row["sessions"])
        runtime, verified = seats.runtime_for_session(row, "transient-hook")
        self.assertIsNone(runtime)
        self.assertFalse(verified)

    def test_malformed_exact_session_records_contribute_no_runtime(self):
        row = {"session": "current", "runtime_sessions": ["not", "a", "map"],
               "runtime": {"family": "claude", "backend": "native"},
               "runtime_verified": True}
        self.assertIsNone(seats.runtime_entry_for_session(row, "current"))
        self.assertEqual(seats.runtime_for_session(row, "current"), (None, False))
        row["runtime_sessions"] = {"current": {
            "runtime": row["runtime"], "verified": False,
            "source": "runtime-conflict"}}
        self.assertEqual(seats.runtime_for_session(row, "current"), (None, False))

    def test_lifecycle_proxy_binding_requires_complete_launch_testimony(self):
        seats.write_roster("shared")
        entry, err = seats.bind_lifecycle_runtime(
            "shared", "proxy-session",
            {"agent_harness": "claude", "backend": "proxy"})
        self.assertIsNone(entry)
        self.assertIn("stamp is malformed", err)

    def test_lifecycle_proxy_binding_refuses_verified_native_identity(self):
        native = {"family": "claude", "agent_harness": "claude",
                  "backend": "native"}
        seats.write_roster("shared", session="native-session", runtime=native)
        entry, err = seats.bind_lifecycle_runtime(
            "shared", "native-session",
            {"family": "codex", "agent_harness": "claude",
             "backend": "proxy"})
        self.assertIsNone(entry)
        self.assertIn("contradicts verified native runtime", err)
        self.assertEqual(seats.roster()["shared"]["session"], "native-session")

    def test_lifecycle_never_downgrades_same_session_verified_native_evidence(self):
        native = {"family": "claude", "agent_harness": "claude",
                  "backend": "native"}
        proxy = {"family": "codex", "agent_harness": "claude",
                 "backend": "proxy"}
        os.environ["HELM_CHAT_NAME"] = "shared"
        seats.write_roster("shared", session="same-session", runtime=native)
        rows = seats.roster()
        rows["shared"]["runtime"] = proxy
        rows["shared"]["runtime_verified"] = True
        pk.write_json(seats.roster_path(), rows)
        entry, err = seats.bind_lifecycle_runtime(
            "shared", "same-session", proxy)
        self.assertIsNone(entry)
        self.assertIn("contradicts verified native runtime", err)
        exact = seats.runtime_entry_for_session(
            seats.roster()["shared"], "same-session")
        self.assertEqual(exact["runtime"], native)
        self.assertTrue(exact["verified"])

    def test_lifecycle_never_downgrades_legacy_current_native_summary(self):
        native = {"family": "claude", "agent_harness": "claude",
                  "backend": "native"}
        proxy = {"family": "codex", "agent_harness": "claude",
                 "backend": "proxy"}
        os.environ["HELM_CHAT_NAME"] = "shared"
        seats.write_roster("shared", session="legacy-session", runtime=native)
        rows = seats.roster()
        rows["shared"].pop("runtime_sessions", None)
        pk.write_json(seats.roster_path(), rows)
        entry, err = seats.bind_lifecycle_runtime(
            "shared", "legacy-session", proxy)
        self.assertIsNone(entry)
        self.assertIn("contradicts verified native runtime", err)
        row = seats.roster()["shared"]
        self.assertNotIn("runtime_sessions", row)
        self.assertEqual(row["runtime"], native)
        self.assertTrue(row["runtime_verified"])

    def test_exact_proxy_session_outranks_a_stale_native_row_summary(self):
        native = {"family": "claude", "agent_harness": "claude",
                  "backend": "native"}
        proxy = {"family": "codex", "agent_harness": "claude",
                 "backend": "proxy"}
        os.environ["HELM_CHAT_NAME"] = "shared"
        seats.write_roster("shared", session="proxy-session", runtime=proxy)
        rows = seats.roster()
        rows["shared"]["runtime"] = native
        rows["shared"]["runtime_verified"] = True
        pk.write_json(seats.roster_path(), rows)
        entry, err = seats.bind_lifecycle_runtime(
            "shared", "proxy-session", proxy)
        self.assertIsNone(err, err)
        self.assertEqual(entry["runtime"], proxy)
        runtime, verified = seats.runtime_for_session(
            seats.roster()["shared"], "proxy-session")
        self.assertTrue(verified)
        self.assertEqual(runtime, proxy)

    def test_lifecycle_history_prunes_runtime_authority_with_session_history(self):
        proxy = {"family": "codex", "agent_harness": "claude",
                 "backend": "proxy"}
        seats.write_roster("shared")
        total = seats.SESSIONS_KEPT + 4
        for i in range(total):
            entry, err = seats.bind_lifecycle_runtime(
                "shared", "proxy-session-%02d" % i, proxy)
            self.assertIsNone(err, err)
            self.assertEqual(entry["runtime"], proxy)
        row = seats.roster()["shared"]
        runtimes = row.get("runtime_sessions") or {}
        final = "proxy-session-%02d" % (total - 1)
        self.assertEqual(len(row["sessions"]), seats.SESSIONS_KEPT)
        self.assertEqual(row["session"], final)
        self.assertEqual(len(runtimes), seats.SESSIONS_KEPT)
        self.assertNotIn("proxy-session-00", row["sessions"])
        self.assertNotIn("proxy-session-00", runtimes)
        self.assertIn(final, row["sessions"])
        self.assertIn(final, runtimes)
        self.assertEqual(set(runtimes), set(row["sessions"]))

    def test_session_eviction_removes_the_evicted_runtime_authority(self):
        proxy = {"family": "codex", "agent_harness": "claude",
                 "backend": "proxy"}
        seats.write_roster("old", session="old-current")
        rows = seats.roster()
        rows["old"]["sessions"].append("moving-session")
        rows["old"]["runtime_sessions"] = {"moving-session": {
            "runtime": proxy, "verified": True, "source": "lifecycle"}}
        pk.write_json(seats.roster_path(), rows)
        os.environ["HELM_CHAT_NAME"] = "new"
        seats.write_roster("new", session="moving-session", runtime=proxy)
        old = seats.roster()["old"]
        self.assertEqual(old["session"], "old-current")
        self.assertEqual(old["sessions"], ["old-current"])
        self.assertNotIn("moving-session", old.get("runtime_sessions") or {})

    def test_handwritten_proxywatch_source_is_not_runtime_authority(self):
        proxy = {"family": "costume", "agent_harness": "claude",
                 "backend": "proxy"}
        os.environ["HELM_CHAT_NAME"] = "shared"
        seats.write_roster("shared", session="proxy-session", runtime=proxy)
        rows = seats.roster()
        rows["shared"]["runtime_sessions"]["proxy-session"] = {
            "runtime": proxy, "verified": True, "source": "proxywatch",
            "proxy_proof": {"session": "proxy-session"}}
        pk.write_json(seats.roster_path(), rows)
        self.assertEqual(seats.runtime_for_session(
            seats.roster()["shared"], "proxy-session"), (None, False))
        seats.write_roster("shared", session="proxy-session")
        entry = seats.runtime_entry_for_session(
            seats.roster()["shared"], "proxy-session")
        self.assertFalse(entry and entry.get("source") == "proxywatch")

    def test_valid_proxywatch_proof_survives_a_presence_write(self):
        proxy = {"family": "codex", "agent_harness": "claude",
                 "backend": "proxy"}
        proof = {"session": "proxy-session", "proof": "validated"}
        os.environ["HELM_CHAT_NAME"] = "shared"
        seats.write_roster("shared", session="proxy-session", runtime=proxy)
        rows = seats.roster()
        rows["shared"]["runtime_sessions"]["proxy-session"] = {
            "runtime": proxy, "verified": True, "source": "proxywatch",
            "proxy_proof": proof}
        pk.write_json(seats.roster_path(), rows)
        with mock.patch.object(proxywatch, "_proxy_proof_runtime",
                               return_value=(proxy, None)):
            seats.write_roster("shared", session="proxy-session")
            entry = seats.runtime_entry_for_session(
                seats.roster()["shared"], "proxy-session")
            self.assertEqual(entry["source"], "proxywatch")
            self.assertEqual(entry["proxy_proof"], proof)

    def test_dark_transition_between_room_scan_and_delivery_consumes_nothing(self):  # noqa: VACUOUS_ASSERTION — moved[done] proves the interleaving fired; byte-identical cursor is the forbidden-consumption observable
        seats.join(session="s-codex", seat="codex", cwd="/tmp/p")
        chat.post("@codex race retained", who="owner")
        pk.write_json(proxywatch._state_path(), {"ts": time.time(),
                      "upstream": {"codex": {"state": "HEALTHY",
                                               "dark": False}}})
        cur = dict(seats._cursor("main", "codex", "s-codex"))
        real = seats._room_dirty
        moved = {"done": False}
        def dark_before_delivery(room, seat, session=None, beacon=False):
            dirty = real(room, seat, session, beacon=beacon)
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
        chat.post("@alice steer", who="daria", origin="web")  # a mention delivers
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

    def test_room_rotation_broadcast_and_unknown_delivery_do_not_cycle(self):
        """The former room -> state -> roster -> room three-way cycle closes."""
        self.seat_up()
        chat.post("seed", who="bob")
        path = chat.room_path("main")
        results = []
        with chat._room_lock("main"):
            workers = [
                threading.Thread(target=lambda: results.append(
                    chat._rotate(path, cap=0, room="main"))),
                threading.Thread(target=lambda: results.append(
                    chat.post("@all census", who="owner"))),
                threading.Thread(target=lambda: results.append(seats.deliver(
                    session="s-new", seat="alice", room="main"))),
            ]
            for worker in workers:
                worker.start()
            time.sleep(0.1)
        for worker in workers:
            worker.join(THREAD_TIMEOUT)
            self.assertFalse(worker.is_alive(), "three-way lock cycle")
        self.assertEqual(len(results), 3)

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

    def test_commit_failure_never_emits_or_records_the_occurrence(self):
        self.seat_up()
        row = chat.post("@all precious", who="bob")
        emit = mock.Mock()
        with mock.patch("helm.seats_delivery._commit_cursor_updates",
                        return_value=seats_cursor.CursorCommitResult(False)):
            self.assertIsNone(seats.deliver(
                session="s-alice", seat="alice", emit=emit, channel="hook"))
        emit.assert_not_called()
        self.assertEqual(seats_receipts.delivery_receipts(row["id"]), [])
        self.assertIn("precious", seats.deliver(
            session="s-alice", seat="alice"))

    def test_emit_failure_after_commit_never_replays_and_stays_unknown(self):
        self.seat_up()
        row = chat.post("@all precious", who="bob")
        with self.assertRaises(RuntimeError):
            seats.deliver(session="s-alice", seat="alice", channel="hook",
                          emit=mock.Mock(side_effect=RuntimeError("killed")))
        self.assertIsNone(seats.deliver(session="s-alice", seat="alice"))
        self.assertEqual(seats_receipts.delivery_receipts(row["id"]), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                seats_receipts.render_delivery_receipts(row["id"]), 0)
        self.assertIn("hook_delivered=UNKNOWN", out.getvalue())

    def test_incomplete_hit_rollback_is_recovered_before_retry_emit(self):
        self.seat_up()
        row = chat.post("@all rollback debt", who="bob")
        emit = mock.Mock()
        with self.incomplete_cursor_commit():
            self.assertIsNone(seats.deliver(
                session="s-alice", seat="alice", emit=emit, channel="hook"))
        emit.assert_not_called()
        pair = seats_cursor._cursor_pair("main", "alice", "s-alice")
        self.assertTrue(any(seats_cursor._cursor_transaction_pending(path)
                            for path in pair))
        self.assertIn("rollback debt", seats.deliver(
            session="s-alice", seat="alice", emit=lambda _line: None,
            channel="hook"))
        self.assertEqual(len(seats_receipts.delivery_receipts(row["id"])), 1)
        self.assertFalse(any(seats_cursor._cursor_transaction_pending(path)
                             for path in pair))

    def test_old_partial_transaction_cannot_overwrite_later_room_commit(self):
        self.seat_up("alice")
        self.seat_up("bob")
        row = chat.post("@all shared room transaction", who="owner")
        with self.incomplete_cursor_commit():
            self.assertIsNone(seats.deliver(
                session="s-alice", seat="alice", channel="hook",
                emit=lambda _line: None))
        self.assertTrue(seats_cursor._cursor_transaction_pending(
            seats.cursor_path("main", "alice", "s-alice")))
        self.assertIn("shared room transaction", seats.deliver(
            session="s-bob", seat="bob", channel="hook",
            emit=lambda _line: None))
        self.assertIn("shared room transaction", seats.deliver(
            session="s-alice", seat="alice", channel="hook",
            emit=lambda _line: None))
        self.assertEqual(len(seats_receipts.delivery_receipts(row["id"])), 2)
        self.assertIsNone(seats.deliver(session="s-bob", seat="bob"))

    def test_persistent_prepare_fsync_failure_retains_recovery_map(self):
        self.seat_up()
        chat.post("@alice fsync debt", who="bob")
        emit = mock.Mock()
        with mock.patch("helm.seats_cursor._fsync_dir", return_value=False):
            self.assertIsNone(seats.deliver(
                session="s-alice", seat="alice", emit=emit, channel="hook"))
        emit.assert_not_called()
        path = seats.cursor_path("main", "alice", "s-alice")
        journal = seats_cursor._cursor_journal(
            seats_cursor._cursor_txn_pointer(path))
        self.assertEqual(journal["state"], "prepared")
        self.assertIn(os.path.basename(path), journal["before"])
        self.assertIn("fsync debt", seats.deliver(
            session="s-alice", seat="alice", channel="hook",
            emit=lambda _line: None))

    def test_cursor_and_rollback_images_require_directory_durability(self):
        self.seat_up()
        chat.post("@alice durable images", who="bob")
        calls = []

        def fsync_dir(_path):
            calls.append(len(calls) + 1)
            return len(calls) not in (3, 4)  # second after-image, first rollback

        emit = mock.Mock()
        with mock.patch("helm.seats_cursor._fsync_dir", side_effect=fsync_dir):
            self.assertIsNone(seats.deliver(
                session="s-alice", seat="alice", emit=emit, channel="hook"))
        emit.assert_not_called()
        path = seats.cursor_path("main", "alice", "s-alice")
        journal = seats_cursor._cursor_journal(
            seats_cursor._cursor_txn_pointer(path))
        self.assertEqual(journal["state"], "prepared")
        self.assertIn(os.path.basename(path), journal["after"])
        self.assertIn("durable images", seats.deliver(
            session="s-alice", seat="alice", channel="hook",
            emit=lambda _line: None))

    def test_commit_witness_does_not_replace_a_directory_entry(self):
        self.seat_up()
        chat.post("@alice durable witness", who="bob")
        calls = []

        def fsync_dir(_path):
            calls.append(len(calls) + 1)
            return len(calls) <= 3       # prepared map + two cursor after-images

        emitted = []
        with mock.patch("helm.seats_cursor._fsync_dir", side_effect=fsync_dir):
            line = seats.deliver(session="s-alice", seat="alice",
                                 emit=emitted.append, channel="hook")
        self.assertIn("durable witness", line)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(len(calls), 3)
        path = seats.cursor_path("main", "alice", "s-alice")
        journal_path = seats_cursor._cursor_txn_pointer(path)
        with open(journal_path, encoding="utf-8") as f:
            records = [json.loads(raw) for raw in f if raw.strip()]
        self.assertEqual([row["state"] for row in records],
                         ["prepared", "committed"])
        self.assertEqual(seats_cursor._cursor_journal(journal_path)["state"],
                         "committed")
        self.assertIsNone(seats.deliver(session="s-alice", seat="alice"))

    def test_lockfree_cursor_reads_reject_pointer_publication_race(self):
        self.seat_up()
        chat.post("@alice still owed", who="bob")
        path = seats.cursor_path("main", "alice", "s-alice")
        real = pk.read_json

        def raced(target, default=None):
            row = real(target, default)
            if target == path:
                seats_cursor._durable_json(
                    seats_cursor._cursor_txn_pointer(path),
                    {"state": "committed", "tx": "publication-race"})
                row = dict(row, off=os.path.getsize(chat.room_path("main")))
            return row

        with mock.patch("helm.seats_delivery.pk.read_json", side_effect=raced):
            self.assertIsNone(seats._cursor("main", "alice", "s-alice"))
            self.assertTrue(seats._room_dirty("main", "alice", "s-alice"))
        with mock.patch("helm.seats_cursor._cursor_transaction_epoch",
                        side_effect=[(("old",), False), (("new",), False)]):
            cur, fault = seats_cursor._cursor_checked(
                "main", "alice", "s-alice")
        self.assertIsNone(cur)
        self.assertEqual(fault.reason, "transaction changed")

    def test_incomplete_quiet_rollback_restores_crossed_ground(self):
        self.seat_up()
        before = seats._cursor("main", "alice", "s-alice")["off"]
        chat.post("unaddressed noise", who="bob")
        with self.incomplete_cursor_commit():
            self.assertIsNone(seats.deliver(session="s-alice", seat="alice"))
        pair = seats_cursor._cursor_pair("main", "alice", "s-alice")
        self.assertTrue(seats_cursor._cursor_transaction_pending(pair[0]))
        with seats_cursor._cursor_locks(pair, estate=False):
            pass
        self.assertEqual(seats._cursor("main", "alice", "s-alice")["off"], before)

    def test_incomplete_unrenderable_rollback_recovers_before_skip(self):
        self.seat_up()
        seats.dm("alice", "baseline", who="bob")
        self.assertIn("baseline", seats.deliver_any(
            session="s-alice", seat="alice"))
        lane = seats.dm_lane("alice")
        with open(chat.room_path(lane), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "t", "from": "bob", "dm": "alice",
                                "id": "bad-debt", "text": 123}) + "\n")
            f.write(json.dumps({"ts": "t", "from": "bob", "dm": "alice",
                                "id": "good", "text": "after debt"}) + "\n")
        with self.incomplete_cursor_commit():
            self.assertIsNone(seats.deliver(
                session="s-alice", seat="alice", room=lane))
        self.assertIsNone(seats.deliver(
            session="s-alice", seat="alice", room=lane))
        self.assertIn("after debt", seats.deliver(
            session="s-alice", seat="alice", room=lane))

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

    def test_a_timeout_shorter_than_one_poll_is_honoured(self):  # noqa: VACUOUS_ASSERTION — the observable is the elapsed wall, asserted POSITIVELY from both sides (>= the timeout, < 1 s); None is only the documented timeout return
        """`--timeout` bounds the WAIT, not the first poll after it. The loop
        checked its deadline and then slept a whole poll, so a 0.05 s wait on
        the 2 s cadence returned after 2 s. Single-shot and --follow share the
        loop; --follow must still return only AT its timeout, never before."""
        seats.join(seat="alice", cwd="/tmp/p")
        for follow in (False, True):
            with self.subTest(follow=follow):
                t0 = time.time()
                line = seats.wait(seat="alice", timeout=0.05, poll=2.0,
                                  follow=follow,
                                  emit=[].append if follow else None)
                elapsed = time.time() - t0
                self.assertIsNone(line)
                self.assertGreaterEqual(elapsed, 0.05)
                self.assertLess(elapsed, 1.0)

    def test_cli_duplicate_follow_exits_cleanly_with_incumbent_pid(self):
        already = {"pid": 4242, "session": "same-session",
                   "why": "held by live pid 99"}
        report = {"seat": "alice", "pid": 5252, "stopped": [], "kept": [],
                  "pruned": [], "registered": False, "already_live": already}
        with mock.patch("helm.beacons.arm", return_value=report) as arm, \
                mock.patch("helm.seats_cli.wait") as wait:
            rc, out, err = self.cmd("wait", ["--seat", "alice", "--follow"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("already armed", err)
        self.assertIn("pid 4242", err)
        wait.assert_not_called()
        # THE SESSION THE CALL RAN UNDER, NOT THE ONE THIS LINE CAN SEE. The
        # fixture declares an identity for the duration of `cmd` and restores
        # it on the way out, so `_env_session()` here answers about the state
        # AFTER the subject finished -- None -- and the arm would be asserting
        # that the CLI passed a session it never had.
        # reap=True IS THE DECLARED IDENTITY'S ANSWER. The single authority
        # door now decides the reap leg too, and this arm's session is a
        # DECLARED one, so the answer is the unchanged pre-cure behaviour.
        arm.assert_called_once_with(
            "alice", session=_tmp_session_for("alice"), replace=False,
            reap=True, waiter=beacons.requested_waiter_spec("main"))

    def test_case_variant_seat_assertion_arms_the_declared_identity(self):
        """`--seat kimi` is a valid assertion by a process declared `Kimi`, but
        argv casing must not become a second actuator namespace."""
        os.environ["HELM_CHAT_NAME"] = "Kimi"
        already = {"pid": 4242, "session": "same-session"}
        report = {"seat": "Kimi", "pid": 5252, "stopped": [], "kept": [],
                  "pruned": [], "registered": False, "already_live": already}
        with mock.patch("helm.seats_cli._beacon_identity_refusal",
                               return_value=None), \
                mock.patch("helm.beacons.arm", return_value=report) as arm, \
                mock.patch("helm.seats_cli.wait") as wait:
            rc, _out, err = self.cmd("wait", ["--seat", "kimi", "--follow"])
        self.assertEqual(rc, 0, err)
        arm.assert_called_once_with(
            "Kimi", session=_tmp_session_for("Kimi"), replace=False,
            reap=True, waiter=beacons.requested_waiter_spec("main"))
        wait.assert_not_called()

    def test_cli_surfaces_degraded_arm_and_keeps_the_waiter_running(self):
        report = {"seat": "alice", "pid": 5252, "stopped": [], "kept": [],
                  "pruned": [], "registered": False, "already_live": None,
                  "conflict": None, "error": "arming marker was not written"}
        with mock.patch("helm.beacons.arm", return_value=report), \
                mock.patch("helm.seats_cli.wait", return_value=None) as wait:
            rc, _out, err = self.cmd(
                "wait", ["--seat", "alice", "--follow", "--timeout", "0.01"])
        self.assertEqual(rc, 0)
        self.assertIn("arm DEGRADED", err)
        self.assertIn("stopped=[]", err)
        self.assertIn("registered=False", err)
        wait.assert_called_once()

    def test_cli_scope_conflict_requires_replace_without_entering_wait(self):
        conflict = {"pid": 4242, "session": "same-session",
                    "waiter": {"ambient": True}}
        report = {"seat": "alice", "pid": 5252, "stopped": [], "kept": [],
                  "pruned": [], "registered": False, "already_live": None,
                  "conflict": conflict}
        with mock.patch("helm.beacons.arm", return_value=report) as arm, \
                mock.patch("helm.seats_cli.wait") as wait:
            rc, _out, err = self.cmd("wait", ["--seat", "alice", "--follow"])
        self.assertEqual(rc, 2)
        self.assertIn("different wait behavior", err)
        self.assertIn("--replace", err)
        wait.assert_not_called()
        arm.assert_called_once()

    def test_cli_replace_requests_deliberate_rotation(self):
        report = {"seat": "alice", "pid": 5252, "stopped": [4242],
                  "kept": [], "pruned": [], "registered": False,
                  "already_live": None}
        with mock.patch("helm.beacons.arm", return_value=report) as arm, \
                mock.patch("helm.seats_cli.wait", return_value=None):
            rc, _out, _err = self.cmd(
                "wait", ["--seat", "alice", "--follow", "--replace",
                         "--timeout", "0.01"])
        self.assertEqual(rc, 0)
        arm.assert_called_once_with(
            "alice", session=_tmp_session_for("alice"), replace=True,
            reap=True,
            waiter=beacons.requested_waiter_spec("main", timeout=0.01))

    def test_replace_without_follow_is_refused(self):
        with mock.patch("helm.beacons.arm") as arm, \
                mock.patch("helm.seats_cli.wait") as wait:
            rc, _out, err = self.cmd(
                "wait", ["--seat", "alice", "--replace", "--timeout", "0.01"])
        self.assertEqual(rc, 2)
        self.assertIn("--replace requires --follow", err)
        arm.assert_not_called()
        wait.assert_not_called()

    def test_identity_disagreement_is_refused_before_destructive_arm(self):
        with mock.patch("helm.seats_cli._beacon_identity_refusal",
                               return_value="REFUSING identity test"), \
                mock.patch("helm.beacons.arm") as arm, \
                mock.patch("helm.seats_cli.wait") as wait:
            rc, out, _err = self.cmd(
                "wait", ["--seat", "alice", "--follow", "--replace"])
        self.assertEqual(rc, 0)
        self.assertIn("REFUSING identity test", out)
        arm.assert_not_called()
        wait.assert_not_called()

    def test_invalid_timeout_is_refused_before_destructive_arm(self):
        with mock.patch("helm.beacons.arm") as arm, \
                mock.patch("helm.seats_cli.wait") as wait:
            rc, _out, err = self.cmd(
                "wait", ["--seat", "alice", "--follow", "--replace",
                         "--timeout", "not-a-number"])
        self.assertEqual(rc, 2)
        self.assertIn("--timeout wants a number", err)
        arm.assert_not_called()
        wait.assert_not_called()

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


class BeaconFileSinkRefusalTest(SeatsBase):
    """A --follow beacon whose stdout is a regular FILE wakes nobody: armed
    as a background shell task it would consume rows into an unread sink
    (three #playapal seats in one night, 2026-08-21). wait() must refuse at
    arm time, INTO that file, and consume nothing; a pipe stdout (the
    Monitor shape) streams the wake, and HELM_BEACON_FILE_SINK_OK=1 keeps a
    deliberate capture working."""

    class _PassDone(Exception):
        pass

    def _armed_wait(self, stdout_obj, env_ok=False):
        patches = {"HELM_BEACON_FILE_SINK_OK": "1"} if env_ok else {}
        with mock.patch.dict(os.environ, patches, clear=False):
            with mock.patch.object(sys, "stdout", stdout_obj):
                with mock.patch("time.sleep", side_effect=self._PassDone):
                    try:
                        return seats.wait(seat="alice", follow=True,
                                          poll=0.01)
                    except self._PassDone:
                        return "_looped"

    def test_file_stdout_refuses_into_the_file_and_consumes_nothing(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice pending row", who="bob")
        with tempfile.NamedTemporaryFile("w+", delete=True) as sink:
            out = self._armed_wait(sink)
            sink.flush()
            sink.seek(0)
            body = sink.read()
        self.assertIsNone(out)
        self.assertIn("REFUSING this beacon", body)
        from helm import seats_advice
        self.assertIn("Arm it as a Monitor", body)
        self.assertIn(seats_advice.beacon_monitor("<you>"), body)
        # THE ROW SURVIVED THE REFUSED ARM: a later real drain delivers it.
        captured = []
        with mock.patch("time.sleep", side_effect=self._PassDone):
            with self.assertRaises(self._PassDone):
                seats.wait(seat="alice", follow=True, poll=0.01,
                           emit=captured.append)
        self.assertTrue(any("pending row" in c for c in captured))

    def test_pipe_stdout_streams_the_wake_unrefused(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice pending row", who="bob")
        r, w = os.pipe()
        writer = os.fdopen(w, "w")
        try:
            out = self._armed_wait(writer)
        finally:
            writer.close()
        with os.fdopen(r) as reader:
            streamed = reader.read()
        self.assertEqual(out, "_looped")
        self.assertNotIn("REFUSING", streamed)
        self.assertIn("pending row", streamed)

    def test_env_opt_out_lets_a_deliberate_file_capture_run(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice pending row", who="bob")
        with tempfile.NamedTemporaryFile("w+", delete=True) as sink:
            out = self._armed_wait(sink, env_ok=True)
            sink.flush()
            sink.seek(0)
            body = sink.read()
        self.assertEqual(out, "_looped")
        self.assertNotIn("REFUSING", body)
        self.assertIn("pending row", body)


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
        chat.post("side-room note", who="daria", origin="web", room="announce")
        self.assertIsNone(seats.deliver_any(session="s-o", seat="oz"))
        chat.post("all hands", who="daria", origin="web")   # main: no longer wakes un-homed
        self.assertIsNone(seats.deliver_any(session="s-o", seat="oz"))
        chat.post("@oz ping", who="daria", origin="web")    # …but a mention does
        self.assertIn("ping", seats.deliver_any(session="s-o", seat="oz"))
        # …and a seat HOMED to the side room hears the owner there
        seats.join(session="s-an", seat="anna", cwd="/tmp/p", room="announce")
        chat.post("announce word", who="daria", origin="web", room="announce")
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
        # A SEAT DELIVERING ITS OWN MAIL DECLARES ITSELF (task/994): these
        # calls go straight to chat.cmd_chat rather than through self.cmd, so
        # they carry their own declaration. `deliver --seat tm` from a process
        # that is nobody drains tm's cursor, and that is what stopped being
        # allowed. ENV_KEYS restores HELM_CHAT_NAME in tearDown.
        os.environ["HELM_CHAT_NAME"] = "tm"
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

    def test_cursor_gc_keeps_clean_catchup_ground_across_compaction(self):
        """A waiter keeps its launch-time session across compaction. GC must not
        remove either that exact cursor or the seat admission baseline and make
        a side-room follow replay parked mentions one row per boundary."""
        room, seat, session = "archive", "parked", "oldbeef"
        chat.post("@parked historical one", who="bob", room=room)
        chat.post("@parked historical two", who="bob", room=room)
        seats.join(session=session, seat=seat, cwd="/tmp/p")
        seats.catchup(seat, room=room, apply=True, include_addressed=True)
        before = seats._cursor(room, seat, session)
        self.assertEqual(before["off"], before["base"])

        chat._reap_for_test(live={"newbeef"}, waiters={session})

        self.assertTrue(os.path.exists(seats.cursor_path(room, seat)))
        self.assertTrue(os.path.exists(seats.cursor_path(room, seat, session)))
        self.assertIsNone(seats.deliver_any(
            session=session, seat=seat, room="archive-r2", ambient=False))
        self.assertIsNone(seats.deliver_any(
            session=session, seat=seat, room="archive-r2", ambient=False))
        after = seats._cursor(room, seat, session)
        self.assertEqual((after["off"], after["rid"], after["base"]),
                         (before["off"], before["rid"], before["base"]))

    def test_pending_primary_still_backfills_addressed_side_room(self):
        chat.post("primary ground", who="owner")
        state = seats._baseline_state("main")
        pair = seats_cursor._cursor_pair("main", "ghost", "s-g")
        for beacon in (False, True):
            seats._write_cursor("main", "ghost", *state, session="s-g",
                                beacon=beacon, base=state[2])
        updates = {path: dict(pk.read_json(path), active=True) for path in pair}
        real_write, calls = seats_cursor._durable_text, []

        def fail_after_first(path, raw):
            calls.append(path)
            if len(calls) >= 2:
                raise OSError("primary write/rollback failed")
            return real_write(path, raw)

        with seats_cursor._cursor_locks(pair), mock.patch(
                "helm.seats_cursor._durable_text", side_effect=fail_after_first):
            result = seats_cursor._commit_cursor_updates(updates)
        self.assertFalse(result)
        self.assertFalse(result.rollback_complete)
        chat.post("@ghost pre-recovery side ask", who="owner", room="side")
        self.assertIn("pre-recovery side ask", seats.deliver_any(
            session="s-g", seat="ghost"))

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
        # the block no longer names the row/room (task 692 dropped samples);
        # the runnable park verb proves the full inbox block is what fired
        self.assertIn("helm chat catchup --including-mentions --apply", err)
        self.assertIn("[helm stop-guard timing] BEGIN response", err)
        self.assertIn("[helm stop-guard timing] DONE response elapsed=", err)
        # the gate never consumed it — the lane still delivers the cross-room
        # row afterwards (POSITIVE control that the gate did not eat it)
        self.assertIn("review the team-x branch",
                      seats.deliver_any(session="s-sg", seat="sg"))

    def test_response_stage_is_named_before_block_publication(self):  # noqa: VACUOUS_ASSERTION — BEGIN response is the must-hit on the interrupted stage; DONE must be absent because publication raised inside it
        seats.join(session="s-response", seat="response", cwd="/tmp/p")
        chat.post("@response inspect", who="bob", room="team-response")
        payload = json.dumps({"session_id": "s-response"}).encode()
        fake = types.SimpleNamespace(buffer=io.BytesIO(payload))
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", fake), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(seats_stop_seam, "emit_blocks",
                                  side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            seats.cmd("stop-guard", ["--hook-json", "--seat", "response"],
                      "main")
        rendered = err.getvalue()
        self.assertIn("[helm stop-guard timing] BEGIN response", rendered)
        self.assertNotIn("[helm stop-guard timing] DONE response", rendered)

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
    the allowlist starved a seat of its own mention in #helm-dogfood):
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
        chat.post("owner direction for team b", who="daria", origin="web",
                  room="team-b")
        chat.post("team b chatter", who="bob", room="team-b")
        self.assertIsNone(seats.deliver_any(session="s-ta", seat="ta"))
        # …but a DIRECT @mention crosses any room, always (THE codex-2 bug:
        # a homed seat never saw its own mention posted in #helm-dogfood)
        chat.post("@ta foreign mention", who="bob", room="team-b")
        self.assertIn("foreign mention",
                      seats.deliver_any(session="s-ta", seat="ta"))
        # home room + main + owner-in-home DO land (primary room first:
        # main's mention outranks team-a's, one nudge per boundary)
        chat.post("@ta home word", who="bob", room="team-a")
        chat.post("@ta main word", who="bob")
        chat.post("owner in team a", who="daria", origin="web", room="team-a")
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
        chat.post("team b owner note", who="daria", origin="web", room="team-b")
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
        # the ONLY @gate mention is the stranded row, so a fired inbox block
        # proves roster observation did not steal its scan slot (task 692:
        # the block no longer echoes the row body "stranded")
        self.assertIn("undelivered message(s)", "\n".join(blocks))

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
    posts/@all never fleet-wide; (d) mute gates idle waking only — never hook
    delivery, pending, or stop obligations."""

    def test_the_repro_foreign_room_mention_wakes_main_homed_beacon(self):
        """THE live bug: a seat homed to #main never saw its own mention
        posted in #helm-dogfood — the homing allowlist starved the beacon."""
        os.environ["HELM_CHAT_ROOM"] = "main"
        seats.join(session="s-c2", seat="codex-2", cwd="/tmp/p")
        del os.environ["HELM_CHAT_ROOM"]
        self.assertEqual(seats.roster()["codex-2"]["home_room"], "main")
        chat.post("@codex-2 please pick this up", who="daria",
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
        chat.post("owner steering team b", who="daria", origin="web",
                  room="team-b")
        chat.post("un-homed main chatter", who="bob")   # main ≠ home either
        self.assertIsNone(seats.deliver_any(session="s-n", seat="nn"))

    def test_mute_is_wake_only_and_coordination_is_at_most_once(self):
        seats.join(session="s-m", seat="mu", cwd="/tmp/p", room="team-a")
        rc, out, _err = self.cmd("seat", ["mute", "team-a", "--seat", "mu"])
        self.assertEqual(rc, 0)
        self.assertIn("muted", out)
        self.assertEqual(seats.mutes("mu"), ["team-a"])

        # Exact-token broadcast + ambient chatter are wake-ineligible while
        # muted. The beacon scans both without emitting, but its separate wake
        # cursor may not consume the hook's delivery obligation.
        chat.post("@all muted standup", who="bob", room="team-a")
        chat.post("home chatter", who="bob", room="team-a")
        captured = []
        seats.wait(seat="mu", session="s-m", follow=True, timeout=0.05,
                   poll=0.01, emit=captured.append, ambient=True)
        self.assertEqual(captured, [])
        self.assertIn("muted standup",
                      seats.deliver_any(session="s-m", seat="mu"))
        self.assertIn("home chatter",
                      seats.deliver_any(session="s-m", seat="mu"))

        # Direct addresses stay wake-tier even in a muted room, and a successful
        # beacon attempt remains deduplicated from the later hook reader.
        direct = chat.post("@all @mu direct word", who="bob", room="team-a")
        captured = []
        seats.wait(seat="mu", session="s-m", follow=True, timeout=0.05,
                   poll=0.01, emit=captured.append)
        self.assertEqual(len(captured), 1)
        self.assertIn("direct word", captured[0])
        receipt = seats_receipts.delivery_receipts(direct["id"], "team-a")
        self.assertEqual([r["effect"] for r in receipt], ["wake-attempted"])
        self.assertIsNone(seats.deliver_any(session="s-m", seat="mu"))

    def test_mute_preserves_stop_guard_obligation(self):
        seats.join(session="s-sg", seat="mg", cwd="/tmp/p", room="team-a")
        seats.set_mute("mg", "team-a")
        chat.post("noise while muted", who="bob", room="team-a")
        blocks, _w = seats.stop_guard(session="s-sg", seat="mg")
        self.assertTrue(blocks)              # delivery is still owed
        self.assertIn("noise while muted",
                      seats.deliver_any(session="s-sg", seat="mg"))


class PairedCursorCoordinationTest(SeatsBase):
    def _raw(self, room, rows):
        chat._ensure_dir()
        path = chat.room_path(room)
        with open(path, "ab") as f:
            for row in rows:
                f.write(json.dumps(row).encode("utf-8") + b"\n")

    def test_hook_first_suppresses_later_beacon_duplicate(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@all hook first", who="owner")
        self.assertIn("hook first", seats.deliver(
            session="s-a", seat="alice", emit=lambda _line: None,
            channel="hook"))
        captured = []
        seats.wait(seat="alice", session="s-a", follow=True, timeout=0.03,
                   poll=0.01, emit=captured.append)
        self.assertEqual(captured, [])

    def test_muted_idless_and_duplicate_ids_remain_distinct_obligations(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p", room="team")
        seats.set_mute("alice", "team")
        rows = [
            {"ts": "t", "from": "owner", "text": "@all idless"},
            {"ts": "t", "from": "owner", "text": "@all idless"},
            {"ts": "t", "from": "owner", "text": "@all duplicate one",
             "id": "duplicate"},
            {"ts": "t", "from": "owner", "text": "@all duplicate two",
             "id": "duplicate"},
        ]
        self._raw("team", rows)
        captured = []
        seats.wait(seat="alice", session="s-a", follow=True, timeout=0.03,
                   poll=0.01, emit=captured.append, ambient=True)
        self.assertEqual(captured, [])
        delivered = [seats.deliver(
            session="s-a", room="team", seat="alice") for _ in rows]
        self.assertEqual(sum("idless" in line for line in delivered), 2)
        self.assertIn("duplicate one", delivered[2])
        self.assertIn("duplicate two", delivered[3])

    def test_waiting_lookahead_is_pure_until_row_is_crossed(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p", room="team")
        seats.set_mute("alice", "team")
        chat.post("@alice wake now", who="owner", room="team")
        chat.post("@all hold later", who="owner", room="team")
        self.assertIn("wake now", seats.deliver(
            session="s-a", room="team", seat="alice", emit=lambda _line: None,
            channel="beacon", ambient=True))
        cur = seats._cursor("team", "alice", "s-a", beacon=True)
        self.assertEqual(cur.get("held") or [], [],
                         "waiting-only scan mutated a future obligation")

    def test_rotation_remaps_retained_muted_obligations(self):
        chat.post("dropped by the cut", who="owner", room="team")  # task/2931
        seats.join(session="s-a", seat="alice", cwd="/tmp/p", room="team")
        seats.set_mute("alice", "team")
        chat.post("@all held one", who="owner", room="team")
        chat.post("@all held two", who="owner", room="team")
        self.assertIsNone(seats.deliver(
            session="s-a", room="team", seat="alice", channel="beacon",
            ambient=True))
        self.assertTrue(chat._rotate(chat.room_path("team"), cap=1, room="team"))
        self.assertIn("held one", seats.deliver(
            session="s-a", room="team", seat="alice"))
        self.assertIn("held two", seats.deliver(
            session="s-a", room="team", seat="alice"))

    def test_missing_exact_beacon_is_dirty_despite_clean_conamed_fallback(self):
        seats.join(session="s-one", seat="shared", cwd="/tmp/p")
        seats.join(session="s-two", seat="shared", cwd="/tmp/p")
        os.remove(seats.beacon_cursor_path("main", "shared", "s-two"))
        chat.post("@all exact session", who="owner")
        self.assertTrue(seats._room_dirty(
            "main", "shared", "s-two", beacon=True))
        self.assertIn("exact session", seats.deliver(
            session="s-two", seat="shared", emit=lambda _line: None,
            channel="beacon"))

    def test_direct_and_indexed_activity_include_beacon_cursors(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        delivery = seats.cursor_path("main", "alice", "s-a")
        wake = seats.beacon_cursor_path("main", "alice", "s-a")
        row = pk.read_json(wake, {})
        row["active"] = True
        pk.write_json(wake, row)
        os.remove(delivery)
        self.assertTrue(seats.room_active("main", "alice"))
        self.assertTrue(seats.room_active(
            "main", "alice", seats._cursor_path_index()))

    def test_baseline_and_late_init_share_one_lock_order(self):
        entered, resume = threading.Event(), threading.Event()
        real = seats._baseline_state

        def delayed(*args, **kwargs):
            state = real(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                self.assertTrue(resume.wait(THREAD_TIMEOUT))
            return state

        chat._ensure_dir()
        with mock.patch("helm.seats_delivery._baseline_state",
                        side_effect=delayed):
            baseline = threading.Thread(
                target=seats._baseline_room_cursors,
                args=("main", "alice", ["late-session"]))
            baseline.start()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            initializing = threading.Thread(
                target=seats._init_cursor,
                args=("main", "alice", "other-session"))
            initializing.start()
            initializing.join(0.05)
            self.assertTrue(initializing.is_alive())
            resume.set()
            baseline.join(THREAD_TIMEOUT)
            initializing.join(THREAD_TIMEOUT)
        self.assertFalse(baseline.is_alive())
        self.assertFalse(initializing.is_alive())

    def test_missing_exact_delivery_never_borrows_clean_seat_cursor(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row = chat.post("@alice exact owed", who="owner")
        state = seats._baseline_state("main")
        seats._write_cursor("main", "alice", *state, base=state[2])
        os.remove(seats.cursor_path("main", "alice", "s-a"))
        pending = seats._pending_rows("main", "alice", session="s-a")
        self.assertEqual([item["id"] for item in pending], [row["id"]])

    def test_beacon_crossed_row_is_not_a_false_stop_obligation(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice wake once", who="owner")
        self.assertIn("wake once", seats.deliver(
            session="s-a", seat="alice", channel="beacon",
            emit=lambda _line: None))
        self.assertEqual(seats._pending_rows(
            "main", "alice", session="s-a"), [])

    # A PAUSED SEAT HOLDS THE ROTATION CUT AT THE OFFSET IT WOULD RESUME FROM.
    # That is what `_init_cursor` gives a session with no exact delivery
    # cursor: its own wake cursor, else its seat's cursor, else an EOF
    # baseline. A session with none of the three has read nothing and is owed
    # nothing below EOF, so it holds nothing (task/2930: kimi's cursorless
    # session pinned #helm's cut at 0 and every post rewrote 9.4 MB).
    def _pause(self, seat="alice"):
        return mock.patch.object(
            proxywatch, "delivery_pause",
            side_effect=lambda name, **_kw: (
                ({"state": "PROXY-COOLDOWN"}, None) if name == seat
                else (None, None)))

    def _forget_cursors(self, seat, *sessions):
        for session in sessions:
            os.remove(seats.cursor_path("main", seat, session))
            os.remove(seats.beacon_cursor_path("main", seat, session))

    def _filler(self, n, tag):
        for i in range(n):
            chat.post("%s %02d %s" % (tag, i, "x" * 200), who="owner")

    def _first_row(self):
        with open(chat.room_path("main"), "rb") as f:
            return json.loads(f.readline())

    def test_paused_missing_exact_cursor_holds_at_its_wake_cursor(self):
        self._filler(5, "before")
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice retained", who="owner")
        state = seats._baseline_state("main")    # the seat read on past it
        seats._write_cursor("main", "alice", *state, base=state[2])
        wake = seats._cursor("main", "alice", "s-a", beacon=True)["off"]
        os.remove(seats.cursor_path("main", "alice", "s-a"))
        st = os.stat(chat.room_path("main"))
        self.assertGreater(wake, 0)
        with self._pause():
            self.assertEqual(seats.rotation_hold_offset(
                "main", st.st_dev, st.st_ino, []), wake)

    def test_cursorless_paused_seat_lets_the_room_rotate_once(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        self._forget_cursors("alice", "s-a", None)
        self._filler(40, "filler")
        path = chat.room_path("main")
        cap = os.path.getsize(path) * 3 // 4
        with self._pause():
            self.assertTrue(chat._rotate(path, cap=cap, room="main"))
            chat.post("after the cut", who="owner")
            self.assertFalse(chat._rotate(path, cap=cap, room="main"))
            st = os.stat(path)
            self.assertIsNone(seats.rotation_hold_offset(
                "main", st.st_dev, st.st_ino, []))
        self.assertIsNone(seats.deliver(
            session="s-a", seat="alice", channel="hook",
            emit=lambda _line: None))       # fresh EOF baseline, no flood
        chat.post("@alice fresh after the cut", who="owner")
        self.assertIn("fresh after the cut", seats.deliver(
            session="s-a", seat="alice", channel="hook",
            emit=lambda _line: None) or "")

    def test_paused_seat_with_a_real_low_cursor_still_holds_the_cut(self):
        self._filler(10, "before")
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        owed = chat.post("@alice owed", who="owner")
        self._filler(30, "after")
        path = chat.room_path("main")
        low = seats._cursor("main", "alice", "s-a")["off"]
        with self._pause():
            st = os.stat(path)
            self.assertEqual(seats.rotation_hold_offset(
                "main", st.st_dev, st.st_ino, []), low)
            self.assertTrue(chat._rotate(path, cap=0, room="main"))
        self.assertEqual(self._first_row()["id"], owed["id"])
        self.assertEqual(seats._cursor("main", "alice", "s-a")["off"], 0)

    # A HOLD THAT FORCES THE CUT TO 0 DROPS NOTHING, SO IT ROTATES NOTHING
    # (task/2931). Before this, _rotate wrote the same bytes to a new inode and
    # remapped every cursor in the room, under the delivery-state guard, on
    # every post over the cap: about 4s a post on live #helm.
    def _pin_alice_at_zero(self):
        self._filler(40, "filler")
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        seats.join(session="s-b", seat="bob", cwd="/tmp/p")
        dev, ino, _off, _rid = seats._baseline_state("main", at_start=True)
        seats._write_cursor("main", "alice", dev, ino, 0, None,
                            session="s-a")
        return chat.room_path("main")

    def _room_and_cursors(self):
        """{name: (inode, mtime_ns, bytes)} for the room and every cursor."""
        root, out = chat.chat_dir(), {}
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if path != chat.room_path("main") and not seats.parse_cursor_path(
                    path):
                continue
            st = os.stat(path)
            with open(path, "rb") as f:
                out[name] = (st.st_ino, st.st_mtime_ns, f.read())
        return out

    def test_a_hold_pinned_at_zero_leaves_the_room_and_cursors_untouched(self):
        path = self._pin_alice_at_zero()
        with self._pause():
            st = os.stat(path)
            self.assertEqual(seats.rotation_hold_offset(
                "main", st.st_dev, st.st_ino, []), 0)
            before = self._room_and_cursors()
            self.assertGreater(len(before), 3)       # room + several cursors
            self.assertFalse(chat._rotate(path, cap=0, room="main"))
            self.assertEqual(self._room_and_cursors(), before)
        self.assertFalse(os.path.exists(
            seats_cursor.rotation_journal_path("main")))
        self.assertFalse(os.path.exists(
            seats_cursor.rotation_temp_path("main")))

    def test_a_room_over_the_cap_with_a_pinned_hold_is_never_rewritten(self):
        path = self._pin_alice_at_zero()
        with self._pause(), mock.patch.object(chat, "SIZE_CAP", 1):
            ino = os.stat(path).st_ino
            for i in range(3):
                size = os.path.getsize(path)
                row = chat.post("post %d over the cap" % i, who="owner")
                st = os.stat(path)
                self.assertEqual(st.st_ino, ino)
                self.assertGreater(st.st_size, size)   # appended, not cut
                with open(path, "rb") as f:
                    self.assertEqual(
                        json.loads(f.read().splitlines()[-1])["id"],
                        row["id"])
            self.assertEqual(seats._cursor("main", "alice", "s-a")["off"], 0)

    def test_a_real_cut_still_rotates_and_every_cursor_keeps_its_row(self):
        self._filler(30, "before")
        for seat in ("alice", "bob", "carol"):
            seats.join(session="s-" + seat, seat=seat, cwd="/tmp/p")
            self._filler(4, "between " + seat)
        path = chat.room_path("main")
        with open(path, "rb") as f:
            st, old = os.fstat(f.fileno()), f.read()

        def row_at(data, off):
            return json.loads(data[off:data.index(b"\n", off)])["id"]

        before = {}
        for name in os.listdir(chat.chat_dir()):
            full = os.path.join(chat.chat_dir(), name)
            item = seats.parse_cursor_path(full)
            if not item or item["room"] != "main":
                continue
            cur = pk.read_json(full, None)
            self.assertEqual((cur["dev"], cur["ino"]), (st.st_dev, st.st_ino))
            if cur["off"] < len(old):
                before[name] = (cur["off"], row_at(old, cur["off"]))
        self.assertGreaterEqual(len(before), 3)
        self.assertTrue(chat._rotate(path, cap=0, room="main"))
        with open(path, "rb") as f:
            new_st, new = os.fstat(f.fileno()), f.read()
        self.assertNotEqual(new_st.st_ino, st.st_ino)
        self.assertLess(len(new), len(old))
        cut = len(old) - len(new)
        self.assertEqual(old[cut:], new)
        self.assertLess(cut, min(off for off, _rid in before.values()))
        for name, (off, rid) in before.items():
            cur = pk.read_json(os.path.join(chat.chat_dir(), name), None)
            self.assertEqual((cur["dev"], cur["ino"], cur["off"]),
                             (new_st.st_dev, new_st.st_ino, off - cut), name)
            self.assertEqual(row_at(new, cur["off"]), rid, name)

    def test_returning_cursorless_session_reads_what_its_seat_owes(self):
        self._filler(10, "before")
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        self._forget_cursors("alice", "s-a")     # the seat's own pair stays
        owed = chat.post("@alice owed across the cut", who="owner")
        self._filler(30, "after")
        path = chat.room_path("main")
        with self._pause():
            self.assertTrue(chat._rotate(path, cap=0, room="main"))
        self.assertEqual(self._first_row()["id"], owed["id"])
        self.assertIn("owed across the cut", seats.deliver(
            session="s-a", seat="alice", channel="hook",
            emit=lambda _line: None) or "")

    def test_rotation_remap_failure_preserves_room_cursor_and_receipts(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row = chat.post("@all keep", who="owner")
        seats.deliver(session="s-a", seat="alice", channel="hook",
                      emit=lambda _line: None)
        path = chat.room_path("main")
        cursor = seats.cursor_path("main", "alice", "s-a")
        with open(path, "rb") as f:
            room_before, inode = f.read(), os.fstat(f.fileno()).st_ino
        with open(cursor, "rb") as f:
            cursor_before = f.read()
        self.assertTrue(seats_receipts.delivery_receipts(row["id"]))
        with mock.patch("helm.seats_cursor._durable_text",
                        side_effect=OSError("injected")):
            self.assertFalse(chat._rotate(path, cap=0, room="main"))
        with open(path, "rb") as f:
            self.assertEqual((f.read(), os.fstat(f.fileno()).st_ino),
                             (room_before, inode))
        with open(cursor, "rb") as f:
            self.assertEqual(f.read(), cursor_before)
        self.assertTrue(seats_receipts.delivery_receipts(row["id"]))

    def test_rotation_room_install_observes_committed_cursor_marker(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice retained", who="owner")
        seats.deliver(session="s-a", seat="alice")
        path = chat.room_path("main")
        cursor = seats.cursor_path("main", "alice", "s-a")
        prior = seats_cursor._cursor_journal(
            seats_cursor._cursor_txn_pointer(cursor))["tx"]
        real, observed = seats.remap_rotated_cursors, []

        def checked(*args, **kwargs):
            install = kwargs["install"]

            def wrapped():
                row = seats_cursor._cursor_journal(
                    seats_cursor._cursor_txn_pointer(cursor))
                observed.append((row.get("state"), row.get("tx")))
                return install()

            return real(*args, **dict(kwargs, install=wrapped))

        with mock.patch("helm.seats.remap_rotated_cursors",
                        side_effect=checked):
            self.assertTrue(chat._rotate(path, cap=0, room="main"))
        self.assertEqual(observed[0][0], "committed")
        self.assertNotEqual(observed[0][1], prior)

    def test_rotation_marker_failure_keeps_room_and_recovery_map(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice retained", who="owner")
        seats.deliver(session="s-a", seat="alice")
        path = chat.room_path("main")
        before = os.stat(path).st_ino
        with mock.patch("helm.seats_cursor._publish_cursor_outcome",
                        side_effect=OSError("marker denied")):
            self.assertFalse(chat._rotate(path, cap=0, room="main"))
        self.assertEqual(os.stat(path).st_ino, before)
        self.assertTrue(os.path.exists(
            seats_cursor.rotation_journal_path("main")))
        self.assertTrue(seats.recover_room_rotation("main"))
        self.assertEqual(os.stat(path).st_ino, before)
        self.assertFalse(os.path.exists(
            seats_cursor.rotation_journal_path("main")))

    def test_rotation_install_fsync_failure_retains_recovery_map(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice install durability", who="owner")
        seats.deliver(session="s-a", seat="alice")
        path = chat.room_path("main")
        with mock.patch("helm.chat._fsync_directory",
                        side_effect=OSError("directory denied")):
            self.assertFalse(chat._rotate(path, cap=0, room="main"))
        self.assertTrue(os.path.exists(
            seats_cursor.rotation_journal_path("main")))
        self.assertTrue(seats.recover_room_rotation("main"))
        cur = seats._cursor("main", "alice", "s-a")
        st = os.stat(path)
        self.assertEqual((cur["dev"], cur["ino"]), (st.st_dev, st.st_ino))

    def test_rotation_retirement_fsync_failure_keeps_map_evidence(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice retirement durability", who="owner")
        seats.deliver(session="s-a", seat="alice")
        path = chat.room_path("main")
        active = seats_cursor.rotation_journal_path("main")
        retired = seats_cursor._rotation_retired_path("main")
        real = seats_cursor._fsync_dir

        def fsync_dir(root):
            if os.path.exists(retired) and not os.path.exists(active):
                return False
            return real(root)

        with mock.patch("helm.seats_cursor._fsync_dir", side_effect=fsync_dir):
            self.assertFalse(chat._rotate(path, cap=0, room="main"))
        self.assertFalse(os.path.exists(active))
        self.assertTrue(os.path.exists(retired))
        with open(retired, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["room"], "main")
        chat.post("@alice the next rotation", who="owner")
        self.assertTrue(chat._rotate(path, cap=0, room="main"))

    def test_malformed_rotation_journal_blocks_recovery_delivery_and_replace(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice malformed map debt", who="owner")
        path = chat.room_path("main")
        journal = seats_cursor.rotation_journal_path("main")
        with open(journal, "w", encoding="utf-8") as f:
            f.write("{malformed\n")
        emit = mock.Mock()
        self.assertFalse(seats.recover_room_rotation("main"))
        self.assertIsNone(seats.deliver(
            session="s-a", seat="alice", emit=emit, channel="hook"))
        emit.assert_not_called()
        self.assertFalse(chat._rotate(path, cap=0, room="main"))
        with open(journal, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{malformed\n")
        os.remove(journal)
        self.assertIn("malformed map debt", seats.deliver(
            session="s-a", seat="alice"))

    def test_unreadable_rotation_journal_blocks_recovery_delivery_and_replace(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice unreadable map debt", who="owner")
        path = chat.room_path("main")
        st = os.stat(path)
        seats_cursor.prepare_rotation_journal(
            "main", st.st_dev, st.st_ino, st.st_dev, st.st_ino, 0, [])
        journal = seats_cursor.rotation_journal_path("main")
        real = seats_cursor._strict_json

        def unreadable(target):
            if target == journal:
                raise OSError("read denied")
            return real(target)

        emit = mock.Mock()
        with mock.patch("helm.seats_cursor._strict_json",
                        side_effect=unreadable):
            self.assertFalse(seats.recover_room_rotation("main"))
            self.assertIsNone(seats.deliver(
                session="s-a", seat="alice", emit=emit, channel="hook"))
            self.assertFalse(chat._rotate(path, cap=0, room="main"))
        emit.assert_not_called()
        os.remove(journal)
        self.assertIn("unreadable map debt", seats.deliver(
            session="s-a", seat="alice"))

    def test_rotation_recovers_partial_cursor_writes_before_room_install(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row = chat.post("@all retained", who="owner")
        seats.deliver(session="s-a", seat="alice", channel="hook",
                      emit=lambda _line: None)
        path = chat.room_path("main")
        with open(seats.cursor_path("main", "alice", "s-a"), "rb") as f:
            cursor_before = f.read()
        real_write, writes = seats_cursor._durable_text, []

        def crash_after_one(target, raw):
            if writes:
                raise SystemExit("crash")
            writes.append(target)
            return real_write(target, raw)

        with mock.patch("helm.seats_cursor._durable_text",
                        side_effect=crash_after_one):
            with self.assertRaises(SystemExit):
                chat._rotate(path, cap=0, room="main")
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(seats_receipts.delivery_receipts(row["id"])), 1)
        self.assertTrue(seats.recover_room_rotation("main"))
        with open(seats.cursor_path("main", "alice", "s-a"), "rb") as f:
            self.assertEqual(f.read(), cursor_before)
        self.assertFalse(os.path.exists(
            seats_cursor.rotation_journal_path("main")))

    def test_rotation_recovers_after_room_install_before_sidecar_finalize(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row = chat.post("@all retained", who="owner")
        seats.deliver(session="s-a", seat="alice", channel="hook",
                      emit=lambda _line: None)
        path = chat.room_path("main")
        with mock.patch(
                "helm.seats_receipts.finish_room_receipt_remap",
                side_effect=OSError("crash after install")):
            self.assertFalse(chat._rotate(path, cap=0, room="main"))
        self.assertTrue(os.path.exists(
            seats_cursor.rotation_journal_path("main")))
        self.assertTrue(seats.recover_room_rotation("main"))
        self.assertFalse(os.path.exists(
            seats_cursor.rotation_journal_path("main")))
        cur = seats._cursor("main", "alice", "s-a")
        st = os.stat(path)
        self.assertEqual((cur["dev"], cur["ino"]), (st.st_dev, st.st_ino))
        self.assertEqual(len(seats_receipts.delivery_receipts(row["id"])), 1)

    def test_rotation_recovery_unions_target_after_partial_source_prune(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        start = os.path.getsize(chat.room_path("main"))
        row = chat.post("@all retained", who="owner")
        for channel in ("hook", "beacon"):
            seats_receipts.record_delivery_receipt(
                row, "main", "alice", "s-a", channel,
                occurrence=seats._occurrence(
                    os.stat(chat.room_path("main")).st_dev,
                    os.stat(chat.room_path("main")).st_ino, start))

        def crash_during_prune(room, retained):
            row_id, occurrence, _new = retained[0]
            root = seats_receipts._occurrence_dir(room, row_id, occurrence)
            os.remove(os.path.join(root, sorted(os.listdir(root))[0]))
            raise SystemExit("rotation source prune crash")

        with mock.patch("helm.seats_receipts.finish_room_receipt_remap",
                        side_effect=crash_during_prune):
            with self.assertRaises(SystemExit):
                chat._rotate(chat.room_path("main"), cap=0, room="main")
        self.assertTrue(seats.recover_room_rotation("main"))
        receipts = seats_receipts.delivery_receipts(row["id"], "main")
        self.assertEqual({item["effect"] for item in receipts},
                         {"hook-delivered", "wake-attempted"})

    def test_rotation_topology_blocks_rename_until_cursor_install_finishes(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice retained", who="owner")
        self.assertIn("retained", seats.deliver(
            session="s-a", seat="alice", channel="hook",
            emit=lambda _line: None))
        entered, release = threading.Event(), threading.Event()
        real = seats_delivery._commit_cursor_updates
        results = []

        def delayed(updates, finish=None):
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            return real(updates, finish=finish)

        with mock.patch("helm.seats_delivery._commit_cursor_updates",
                        side_effect=delayed):
            rotating = threading.Thread(target=lambda: results.append(
                chat._rotate(chat.room_path("main"), cap=0, room="main")))
            rotating.start()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            renaming = threading.Thread(target=lambda: results.append(
                seats.rename_seat("alice", "renamed")))
            renaming.start()
            renaming.join(0.05)
            self.assertTrue(renaming.is_alive(),
                            "rename moved a path snapshotted by rotation")
            release.set()
            rotating.join(THREAD_TIMEOUT)
            renaming.join(THREAD_TIMEOUT)
        self.assertFalse(rotating.is_alive())
        self.assertFalse(renaming.is_alive())
        cur = seats._cursor("main", "renamed", "s-a")
        st = os.stat(chat.room_path("main"))
        self.assertEqual((cur["dev"], cur["ino"]), (st.st_dev, st.st_ino))

    def test_stale_initializer_after_rename_cannot_recreate_old_cursor(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        ok, _message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok)
        self.assertFalse(seats._init_cursor("main", "alice", "s-a"))
        self.assertFalse(os.path.exists(seats.cursor_path(
            "main", "alice", "s-a")))
        self.assertTrue(os.path.exists(seats.cursor_path(
            "main", "renamed", "s-a")))

    def test_hook_first_coordination_does_not_grow_done_tokens(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        self._raw("main", [
            {"id": "n%d" % i, "ts": "t", "from": "alice",
             "text": "noise %d" % i} for i in range(40)])
        chat.post("@alice final", who="owner")
        self.assertIn("final", seats.deliver(
            session="s-a", seat="alice", channel="hook"))
        wake = seats._cursor("main", "alice", "s-a", beacon=True)
        self.assertEqual(wake.get("done") or [], [])

    def test_initializer_preserves_existing_wake_hold_when_delivery_is_missing(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice muted obligation", who="owner")
        path = chat.room_path("main")
        st = os.stat(path)
        token = seats._occurrence(st.st_dev, st.st_ino, 0)
        end = os.path.getsize(path)
        seats._write_cursor("main", "alice", st.st_dev, st.st_ino, end,
                            "wake-rid", session="s-a", beacon=True,
                            held=[token], base=0)
        os.remove(seats.cursor_path("main", "alice", "s-a"))
        self.assertTrue(seats._init_cursor("main", "alice", "s-a"))
        delivery = seats._cursor("main", "alice", "s-a")
        wake = seats._cursor("main", "alice", "s-a", beacon=True)
        self.assertEqual(delivery["off"], 0)
        self.assertEqual(wake.get("held"), [token])

    def test_cursor_transaction_reports_incomplete_reverse_write(self):
        paths = seats_cursor._cursor_pair("main", "alice", "s-a")
        for path in paths:
            pk.write_json(path, {"off": 0})
        real_write, calls = seats_cursor._durable_text, []

        def partial(path, raw):
            calls.append(path)
            if len(calls) >= 2:
                raise OSError("write/reverse write failed")
            return real_write(path, raw)

        with mock.patch("helm.seats_cursor._durable_text", side_effect=partial):
            result = seats_cursor._commit_cursor_updates(
                {path: {"off": 1} for path in paths})
        self.assertFalse(result)
        self.assertFalse(result.rollback_complete)

    def test_incomplete_cursor_rollback_retains_rotation_journal(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice retained", who="owner")
        result = seats_cursor.CursorCommitResult(False, False)
        with mock.patch("helm.seats_delivery._commit_cursor_updates",
                        return_value=result):
            self.assertFalse(chat._rotate(
                chat.room_path("main"), cap=0, room="main"))
        self.assertTrue(os.path.exists(
            seats_cursor.rotation_journal_path("main")))

    def test_rename_recovers_stale_rotation_before_moving_cursor_paths(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice retained", who="owner")
        seats.deliver(session="s-a", seat="alice", channel="hook")
        real_write, written = seats_cursor._durable_text, []

        def crash(target, raw):
            if written:
                raise SystemExit("rotation crash")
            written.append(target)
            return real_write(target, raw)

        with mock.patch("helm.seats_cursor._durable_text", side_effect=crash):
            with self.assertRaises(SystemExit):
                chat._rotate(chat.room_path("main"), cap=0, room="main")
        ok, message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok, message)
        self.assertFalse(os.path.exists(seats.cursor_path(
            "main", "alice", "s-a")))
        cur = seats._cursor("main", "renamed", "s-a")
        st = os.stat(chat.room_path("main"))
        self.assertEqual((cur["dev"], cur["ino"]), (st.st_dev, st.st_ino))

    def test_rotation_hold_census_freezes_beacon_path_against_rename(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice retained", who="owner")
        path = seats.beacon_cursor_path("main", "alice", "s-a")
        entered, release, results = threading.Event(), threading.Event(), []
        real_read = seats_delivery._strict_json

        def blocked(target):
            if target == path:
                entered.set()
                self.assertTrue(release.wait(THREAD_TIMEOUT))
            return real_read(target)

        st = os.stat(chat.room_path("main"))
        with mock.patch("helm.seats_delivery._strict_json", side_effect=blocked):
            holding = threading.Thread(target=lambda: results.append(
                seats.rotation_hold_offset("main", st.st_dev, st.st_ino, [])))
            holding.start()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            renaming = threading.Thread(target=lambda: results.append(
                seats.rename_seat("alice", "renamed")))
            renaming.start()
            renaming.join(0.05)
            self.assertTrue(renaming.is_alive())
            release.set()
            holding.join(THREAD_TIMEOUT)
            renaming.join(THREAD_TIMEOUT)
        self.assertFalse(holding.is_alive())
        self.assertFalse(renaming.is_alive())

    def test_rotation_hold_fails_closed_when_beacon_census_is_unreadable(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice retained", who="owner")
        st = os.stat(chat.room_path("main"))
        with mock.patch("helm.seats_delivery.os.listdir",
                        side_effect=OSError("census denied")):
            self.assertEqual(seats.rotation_hold_offset(
                "main", st.st_dev, st.st_ino, []), 0)

    def test_rotation_hold_fails_closed_when_beacon_record_is_unreadable(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice retained", who="owner")
        path = seats.beacon_cursor_path("main", "alice", "s-a")
        st = os.stat(chat.room_path("main"))
        real = seats_delivery._strict_json

        def unreadable(target):
            if target == path:
                raise OSError("beacon denied")
            return real(target)

        with mock.patch("helm.seats_delivery._strict_json",
                        side_effect=unreadable):
            self.assertEqual(seats.rotation_hold_offset(
                "main", st.st_dev, st.st_ino, []), 0)

    def test_stale_rotation_temp_is_reaped_before_next_rotation(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("row", who="owner")
        tmp = seats_cursor.rotation_temp_path("main")
        with open(tmp, "wb") as f:
            f.write(b"stale full room copy")
        self.assertTrue(chat._rotate(chat.room_path("main"), cap=0, room="main"))
        self.assertFalse(os.path.exists(tmp))


class BroadcastReceiptTest(SeatsBase):
    def _census(self, *rows):
        return {"seats": list(rows)}

    def test_receipts_is_dispatchable_and_helped(self):
        self.assertIn("receipts", chat.SEAT_VERBS)
        self.assertIn("wake_succeeded", chat.HELP["receipts"])
        rc, out, err = self.cmd("receipts", ["does-not-exist"])
        self.assertEqual(rc, 1)
        self.assertNotIn("unknown subcommand", err)
        self.assertIn("no message matches", err)
        with contextlib.redirect_stdout(io.StringIO()) as outbuf:
            self.assertEqual(chat.cmd_chat(["receipts", "--help"]), 0)
        self.assertIn("recipient-session census", outbuf.getvalue())

    def test_broadcast_census_freezes_roster_through_the_physical_append(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        entered, release = threading.Event(), threading.Event()
        real = seats_receipts.broadcast_census
        posted, joined = [], []

        def census(row, room):
            value = real(row, room)
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            return value

        with mock.patch.object(seats_receipts, "broadcast_census",
                               side_effect=census):
            writer = threading.Thread(
                target=lambda: posted.append(chat.post(
                    "@all serialized", who="owner")))
            writer.start()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            joiner = threading.Thread(target=lambda: joined.append(
                seats.join(session="s-b", seat="bob", cwd="/tmp/p")))
            joiner.start()
            joiner.join(0.05)
            self.assertTrue(joiner.is_alive(),
                            "roster changed between census and append")
            release.set()
            writer.join(THREAD_TIMEOUT)
            joiner.join(THREAD_TIMEOUT)
        self.assertFalse(writer.is_alive())
        self.assertFalse(joiner.is_alive())
        frozen = posted[0]["delivery_census"]["recipients"]
        self.assertEqual({item["seat"] for item in frozen}, {"alice"})
        self.assertEqual(len(joined), 1)

    def test_hostile_row_and_room_ids_are_fixed_alphabet_paths(self):
        row_id, room = "../../outside/\x1b[31m", "../room/../../x"
        row = {"id": row_id, "text": "@all hello"}
        seats_receipts.record_delivery_receipt(
            row, room, "alice", "session-a", "hook")
        path = seats_receipts._receipt_path(room, row_id)
        self.assertEqual(os.path.dirname(path), seats_receipts._receipt_dir())
        self.assertRegex(os.path.basename(path), r"^[0-9a-f]{64}$")
        self.assertEqual(len(seats_receipts.delivery_receipts(row_id, room)), 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "outside")))

    def test_unsafe_receipt_parent_never_rolls_back_delivery(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row = chat.post("@all hello", who="owner")
        outside = os.path.join(self.tmp, "outside-receipts")
        os.makedirs(outside)
        os.symlink(outside, seats_receipts._receipt_dir())
        emitted = []
        self.assertIn("hello", seats.deliver(
            session="s-a", seat="alice", emit=emitted.append,
            channel="hook"))
        self.assertEqual(seats_receipts.delivery_receipts(row["id"]), [])
        self.assertIsNone(seats.deliver(session="s-a", seat="alice"),
                          "best-effort evidence failure rolled back cursor")

    def test_concurrent_repeats_deduplicate_and_conamed_sessions_fan_out(self):
        row = {"id": "concurrent", "text": "@fleet hello"}
        gate = threading.Barrier(8)
        def write():
            gate.wait()
            seats_receipts.record_delivery_receipt(
                row, "main", "same", "session-a", "hook")
        threads = [threading.Thread(target=write) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        seats_receipts.record_delivery_receipt(
            row, "main", "same", "session-a", "hook")
        seats_receipts.record_delivery_receipt(
            row, "main", "same", "session-b", "hook")
        got = seats_receipts.delivery_receipts("concurrent")
        self.assertEqual({r["session"] for r in got}, {"session-a", "session-b"})
        self.assertEqual(len(got), 2)

    def test_conamed_hook_deliveries_each_mint_their_own_receipt(self):
        seats.join(session="s-one", seat="shared", cwd="/tmp/p")
        seats.join(session="s-two", seat="shared", cwd="/tmp/p")
        row = chat.post("@fleet fan out", who="owner")
        for session in ("s-one", "s-two"):
            emitted = []
            self.assertIn("fan out", seats.deliver(
                session=session, seat="shared", emit=emitted.append,
                channel="hook"))
        got = seats_receipts.delivery_receipts(row["id"])
        self.assertEqual({r["session"] for r in got}, {"s-one", "s-two"})
        self.assertEqual({r["effect"] for r in got}, {"hook-delivered"})

    def test_no_sink_and_unknown_effect_fields_reach_human_surface(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row = chat.post("@everyone check in", who="owner")
        # A manual/no-sink delivery returns text but mints no effect receipt.
        self.assertIn("check in", seats.deliver(
            session="s-a", seat="alice", emit=None, channel=None))
        deaf = {"seat": "alice", "verdict": beacons.DEAF, "live": []}
        with mock.patch.object(beacons, "census", return_value=self._census(deaf)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(seats_receipts.render_delivery_receipts(row["id"]), 0)
        text = out.getvalue()
        self.assertIn("sink=NONE", text)
        self.assertIn("effects=UNKNOWN", text)
        self.assertIn("wake_succeeded=UNKNOWN", text)
        self.assertIn("turn_executed=UNKNOWN", text)

    def test_paused_muted_then_healthy_receipt_census_is_live_not_generic_absence(self):
        runtime = {"family": "codex", "agent_harness": "pi",
                   "backend": "proxy"}
        seats.join(session="s-c", seat="codex", cwd="/tmp/p", runtime=runtime)
        seats.set_mute("codex", "main")
        row = chat.post("@all status", who="owner")
        pk.write_json(proxywatch._state_path(), {"ts": time.time(), "upstream": {
            "codex": {"state": "RATE-LIMITED", "dark": True}}})
        live = {"seat": "codex", "verdict": beacons.COVERED,
                "live": [{"pid": 1, "session": "s-c"}]}
        with mock.patch.object(beacons, "census", return_value=self._census(live)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(seats_receipts.render_delivery_receipts(row["id"]), 0)
            dark = out.getvalue()
            self.assertIn("delivery=PAUSED(RATE-LIMITED)", dark)
            self.assertIn("muted=YES", dark)
            self.assertIn("wake=INELIGIBLE", dark)
            self.assertIn("sink=LIVE", dark)
            self.assertIn("effects=UNKNOWN", dark)

            pk.write_json(proxywatch._state_path(), {"ts": time.time(),
                          "upstream": {"codex": {
                              "state": "HEALTHY", "dark": False}}})
            emitted = []
            self.assertIn("status", seats.deliver(
                session="s-c", seat="codex", emit=emitted.append,
                channel="hook"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(seats_receipts.render_delivery_receipts(row["id"]), 0)
        healthy = out.getvalue()
        self.assertIn("delivery=ACTIVE", healthy)
        self.assertIn("effects=hook-delivered", healthy)
        self.assertIn("hook_delivered=YES", healthy)
        self.assertIn("wake_succeeded=UNKNOWN", healthy)
        self.assertIn("turn_executed=UNKNOWN", healthy)

    def test_historical_census_ignores_post_join_and_survives_roster_churn(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row = chat.post("@all frozen", who="owner")
        seats.join(session="s-b", seat="bob", cwd="/tmp/p")
        pk.write_json(seats.roster_path(), {})
        out = io.StringIO()
        with mock.patch.object(beacons, "census", return_value=self._census()), \
                contextlib.redirect_stdout(out):
            self.assertEqual(seats_receipts.render_delivery_receipts(row["id"]), 0)
        text = out.getvalue()
        self.assertIn("2 historical recipient sessions", text)
        self.assertNotIn("@bob", text)

    def test_frozen_census_keeps_sessionless_and_exact_recipients(self):
        seats.join(session="s-a", seat="shared", cwd="/tmp/p")
        seats.join(session="s-b", seat="shared", cwd="/tmp/p")
        row = chat.post("@all every process shape", who="owner")
        sessions = {item["session"] for item in
                    row["delivery_census"]["recipients"]}
        self.assertEqual(sessions, {None, "s-a", "s-b"})
        frozen = next(item["recipient"] for item in
                      row["delivery_census"]["recipients"]
                      if item["session"] is None)
        seats_receipts.record_delivery_receipt(
            row, "main", "shared", None, "hook")
        got = seats_receipts.delivery_receipts(row["id"])
        self.assertEqual([item["recipient"] for item in got], [frozen])

    def test_receipt_selector_accepts_valid_row_above_eventledger_cap(self):
        text = "@all " + "x" * (eventledger.MAX_EVENT_BYTES + 1024)
        row = chat.post(text, who="owner")
        found, room, _occurrence, err = seats_receipts._locate_receipt_row(
            row["id"])
        self.assertIsNone(err)
        self.assertEqual((found["id"], room), (row["id"], "main"))

    def test_receipt_docs_refuse_an_idless_selector_contract(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "docs", "VERBS.md")
        with open(path, encoding="utf-8") as f:
            docs = f.read()
        self.assertIn("Id-less rows have no sender-observable receipt selector",
                      docs)
        self.assertNotIn("disambiguates duplicate IDs and id-less rows", docs)

    def test_occurrence_ordinals_are_stable_across_unsorted_dm_census(self):
        row_id = "same-physical-id"
        lanes = [chat.DM_PREFIX + "z-lane", chat.DM_PREFIX + "a-lane"]
        for lane in lanes:
            path = chat.room_path(lane)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"id": row_id, "text": "private"}) + "\n")
        seen = []
        for order in (lanes, list(reversed(lanes))):
            with mock.patch("helm.seats_ack._all_lanes",
                            return_value=(order, [])):
                _row, room, occurrence, err = \
                    seats_receipts._locate_receipt_row(row_id + "@1")
            self.assertIsNone(err)
            seen.append((room, occurrence))
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(seen[0][0], sorted(lanes)[0])

    def test_dm_gc_keeps_transcript_when_receipt_prune_fails(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row, err = seats.dm("alice", "private", who="owner")
        self.assertIsNone(err)
        room = chat.DM_PREFIX + seats._seat_key("alice")
        with mock.patch("helm.seats_receipts.prune_room_receipts",
                        return_value=False):
            seats._unlink_seat_state("alice")
        self.assertTrue(os.path.exists(chat.room_path(room)))
        self.assertEqual(chat.read(room)[0][0]["id"], row["id"])

    def test_retained_dm_gc_preserves_consumed_cursor_for_same_name_rejoin(self):
        seats.join(session="s-old", seat="alice", cwd="/tmp/p")
        row, err = seats.dm("alice", "consumed private", who="owner")
        self.assertIsNone(err)
        self.assertIn("consumed private", seats.deliver_any(
            session="s-old", seat="alice"))
        room = chat.DM_PREFIX + seats._seat_key("alice")
        cursors = [seats.cursor_path(room, "alice"),
                   seats.beacon_cursor_path(room, "alice"),
                   seats.cursor_path(room, "alice", "s-old"),
                   seats.beacon_cursor_path(room, "alice", "s-old")]
        self.assertTrue(all(os.path.exists(path) for path in cursors))
        current = seats.roster()
        current["alice"]["last_seen"] = 0
        pk.write_json(seats.roster_path(), current)
        os.remove(seats.seen_path("alice"))
        roots = [os.path.join(self.tmp, "empty-transcripts")]
        proc = os.path.join(self.tmp, "empty-proc")
        os.makedirs(roots[0])
        os.makedirs(proc)
        with mock.patch("helm.seats_receipts.prune_room_receipts",
                        return_value=False):
            _rows, pruned = seats.gc_roster(
                apply=True, roots=roots, proc_dir=proc)
        self.assertEqual(pruned, ["alice"])
        self.assertNotIn("alice", seats.roster())
        self.assertTrue(os.path.exists(chat.room_path(room)))
        self.assertTrue(all(os.path.exists(path) for path in cursors))
        seats.join(session="s-new", seat="alice", cwd="/tmp/p")
        self.assertIsNone(seats.deliver_any(session="s-new", seat="alice"))
        self.assertEqual(chat.read(room)[0][0]["id"], row["id"])

    def test_receipt_attribution_survives_seat_rename(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        incarnation = seats_cursor.seat_incarnation("alice", session="s-a")
        row = chat.post("@all rename", who="owner")
        ok, _msg = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok)
        self.assertIn("rename", seats.deliver(
            session="s-a", seat="renamed", emit=lambda _line: None,
            channel="hook"))
        receipts = seats_receipts.delivery_receipts(row["id"])
        frozen = next(item for item in
                      row["delivery_census"]["recipients"]
                      if item["session"] == "s-a")
        self.assertEqual(receipts[0]["recipient"], frozen["recipient"])
        self.assertEqual(receipts[0]["incarnation"], incarnation)
        self.assertEqual(frozen["incarnation"], incarnation)
        self.assertEqual(
            seats_cursor.seat_incarnation("renamed", session="s-a"),
            incarnation)

    def test_conamed_live_and_dead_sinks_are_session_specific(self):
        seats.join(session="s-live", seat="shared", cwd="/tmp/p")
        seats.join(session="s-dead", seat="shared", cwd="/tmp/p")
        row = chat.post("@all sinks", who="owner")
        census = {"seat": "shared", "verdict": beacons.COVERED,
                  "live": [{"pid": 1, "session": "s-live"}]}
        with mock.patch.object(beacons, "census", return_value=self._census(census)), \
                mock.patch.object(beacons, "live_sessions",
                                  return_value={"s-live": 1}), \
                mock.patch.object(beacons, "holder_records", return_value={}), \
                mock.patch.object(beacons, "session_state",
                                  side_effect=lambda sid, **_k: (
                                      "live" if sid == "s-live" else "dead", "")):
            recipients = seats_receipts._broadcast_recipients(row, "main")
        sinks = {item["session"]: item["sink"] for item in recipients}
        self.assertEqual(sinks, {
            None: "LIVE", "s-live": "LIVE", "s-dead": "NONE"})

    def test_terminal_scrubs_hostile_row_and_session_ids(self):
        session = "sid\x1b[31m"
        seats.join(session=session, seat="alice", cwd="/tmp/p")
        row = {"id": "row\x1b[2J", "ts": "t", "from": "owner",
               "text": "@all hostile"}
        row["delivery_census"] = seats_receipts.broadcast_census(row, "main")
        chat._ensure_dir()
        with open(chat.room_path("main"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(seats_receipts.render_delivery_receipts(row["id"]), 0)
        self.assertNotIn("\x1b", out.getvalue())

    def test_short_receipt_write_is_unlinked_and_unknown(self):
        row = {"id": "short-row", "text": "@all short"}
        with mock.patch("helm.seats_receipts.os.write", return_value=1):
            seats_receipts.record_delivery_receipt(
                row, "main", "alice", "s-a", "hook")
        self.assertEqual(seats_receipts.delivery_receipts(row["id"]), [])

    def test_canonical_room_aliases_share_receipt_storage(self):
        row = {"id": "alias-row", "text": "@all alias"}
        seats_receipts.record_delivery_receipt(
            row, "../Main", "alice", "s-a", "hook")
        self.assertEqual(len(seats_receipts.delivery_receipts(
            "alias-row", "main")), 1)

    def test_late_dropped_write_and_malformed_marker_stay_bounded_unknown(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row = chat.post("@all rotate", who="owner")
        chat.post("@all retained past the cut", who="owner")   # task/2931
        path = chat.room_path("main")
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
        occurrence = seats._occurrence(st.st_dev, st.st_ino, 0)
        self.assertTrue(chat._rotate(path, cap=0, room="main"))
        seats_receipts.record_delivery_receipt(
            row, "main", "alice", "s-a", "hook", occurrence=occurrence)
        self.assertEqual(seats_receipts.delivery_receipts(row["id"]), [])
        root = seats_receipts._receipt_path("main", row["id"])
        os.makedirs(os.path.join(root, "0" * 64), exist_ok=True)
        with open(os.path.join(root, "0" * 64, "bad.json"), "wb") as f:
            f.write(b'{"id":"torn"')
        receipts, fault = seats_receipts._read_receipts(row["id"], "main")
        self.assertEqual(receipts, [])
        self.assertIsNotNone(fault)

    def test_dm_rename_rewrites_marker_room_identity(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row, err = seats.dm("alice", "@all private", who="owner")
        self.assertIsNone(err)
        old_room = chat.DM_PREFIX + seats._seat_key("alice")
        seats_receipts.record_delivery_receipt(
            row, old_room, "alice", "s-a", "hook")
        ok, _message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok)
        new_room = chat.DM_PREFIX + seats._seat_key("renamed")
        receipts, fault = seats_receipts._read_receipts(row["id"], new_room)
        self.assertIsNone(fault)
        self.assertEqual([item["room"] for item in receipts], [new_room])

    def test_dm_rename_failure_rolls_back_every_moved_artifact(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row, err = seats.dm("alice", "@all private", who="owner")
        self.assertIsNone(err)
        old_room = chat.DM_PREFIX + seats._seat_key("alice")
        new_room = chat.DM_PREFIX + seats._seat_key("renamed")
        self.assertIn("private", seats.deliver_any(
            session="s-a", seat="alice", channel="hook",
            emit=lambda _line: None))
        old_cursor = seats.cursor_path(old_room, "alice", "s-a")
        with mock.patch("helm.seats_roster.pk.atomic_write",
                        side_effect=OSError("redirect write failed")):
            ok, _message = seats.rename_seat("alice", "renamed")
        self.assertFalse(ok)
        self.assertIn("alice", seats.roster())
        self.assertNotIn("renamed", seats.roster())
        self.assertTrue(os.path.exists(old_cursor))
        self.assertTrue(os.path.exists(chat.room_path(old_room)))
        self.assertFalse(os.path.exists(chat.room_path(new_room)))
        self.assertEqual(len(seats_receipts.delivery_receipts(
            row["id"], old_room)), 1)
        self.assertEqual(seats_receipts.delivery_receipts(
            row["id"], new_room), [])

    def test_dm_gc_keeps_transcript_when_receipt_census_is_malformed(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row, err = seats.dm("alice", "@all private", who="owner")
        self.assertIsNone(err)
        room = chat.DM_PREFIX + seats._seat_key("alice")
        with open(chat.room_path(room), "ab") as f:
            f.write(b'{"id":"torn"')
        seats_receipts.record_delivery_receipt(
            row, room, "alice", "s-a", "hook")
        seats._unlink_seat_state("alice")
        self.assertTrue(os.path.exists(chat.room_path(room)))
        self.assertTrue(seats_receipts.delivery_receipts(row["id"], room))

    def test_dm_roster_gc_prunes_exact_receipt_sidecars(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row, err = seats.dm("alice", "@all private", who="owner")
        self.assertIsNone(err)
        room = chat.DM_PREFIX + seats._seat_key("alice")
        seats_receipts.record_delivery_receipt(
            row, room, "alice", "s-a", "hook")
        self.assertTrue(seats_receipts.delivery_receipts(row["id"], room))
        seats._unlink_seat_state("alice")
        self.assertEqual(seats_receipts.delivery_receipts(row["id"], room), [])

    def test_duplicate_ids_require_and_obey_occurrence_selector(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        first = chat.post("@all first", who="owner")
        second = {"id": first["id"], "ts": "t", "from": "owner",
                  "text": "@all second",
                  "delivery_census": first["delivery_census"]}
        with open(chat.room_path("main"), "ab") as f:
            start = f.tell()
            f.write(json.dumps(second).encode() + b"\n")
            st = os.fstat(f.fileno())
        seats_receipts.record_delivery_receipt(
            second, "main", "alice", "s-a", "hook",
            occurrence=seats._occurrence(st.st_dev, st.st_ino, start))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(seats_receipts.render_delivery_receipts(first["id"]), 1)
        self.assertIn("physical occurrences", err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(seats_receipts.render_delivery_receipts(
                first["id"] + "@2"), 0)
        self.assertIn("effects=hook-delivered", out.getvalue())

    def test_occurrence_selector_refuses_a_prefix_even_when_ordinal_is_valid(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        rows = []
        for suffix in ("11111111", "22222222"):
            row = chat.post("@all " + suffix, who="owner")
            row["id"] = "abcd" + suffix
            rows.append(row)
        path = chat.room_path("main")
        with open(path, encoding="utf-8") as f:
            stored = [json.loads(line) for line in f]
        for stored_row, row in zip(stored, rows):
            stored_row["id"] = row["id"]
        with open(path, "w", encoding="utf-8") as f:
            for stored_row in stored:
                f.write(json.dumps(stored_row) + "\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(
                seats_receipts.render_delivery_receipts("abcd@1"), 1)
        self.assertIn("exact full message id", err.getvalue())

    def test_a_deaf_in_effect_recipient_keeps_its_PHYSICAL_live_sink(self):
        """task/2463 FIX10 finding 9. DEAF-IN-EFFECT only ever REFINES
        covered, so it is unreachable without a proven live beacon: the row
        physically arrives. The sessionless map named COVERED, DEAF and VACANT
        and fell through to UNKNOWN for it, throwing away a proven fact to
        avoid claiming a different one — whether a TURN consumed it is
        `wake_succeeded`/`turn_executed`, which stay UNKNOWN on their own."""
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row = chat.post("@all die", who="owner")
        for verdict, want in ((beacons.DEAF_IN_EFFECT, "LIVE"),
                              (beacons.COVERED, "LIVE"),
                              (beacons.DEAF, "NONE")):
            with mock.patch.object(
                    beacons, "census",
                    return_value={"seats": [{"seat": "alice",
                                             "verdict": verdict,
                                             "live": []}]}):
                got = seats_receipts._broadcast_recipients(row, "main")
            sinks = {item["sink"] for item in got
                     if not item.get("session")}
            self.assertEqual(sinks, {want},
                             "verdict %s mapped to %s" % (verdict, sinks))

    def test_current_roster_failure_preserves_frozen_census(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row = chat.post("@all frozen", who="owner")
        with mock.patch("helm.seats_roster.roster_checked",
                        return_value=({}, True)):
            recipients = seats_receipts._broadcast_recipients(row, "main")
        self.assertEqual(len(recipients), 2)
        self.assertTrue(all(item["sink"] == "UNKNOWN" for item in recipients))
        self.assertTrue(all(item["delivery_state"] == "UNKNOWN"
                            for item in recipients))

    def test_room_rotation_retires_only_exact_dropped_occurrence_receipts(self):
        seats.join(session="session-a", seat="alice", cwd="/tmp/p")
        old = chat.post("@all old", who="owner")
        new = chat.post("@all new", who="owner")
        for row in (old, new):
            self.assertIsNotNone(seats.deliver(
                session="session-a", seat="alice", emit=lambda _line: None,
                channel="hook"))
            self.assertEqual(len(seats_receipts.delivery_receipts(row["id"])), 1)
        self.assertTrue(chat._rotate(chat.room_path("main"), cap=1, room="main"))
        self.assertEqual(seats_receipts.delivery_receipts(old["id"]), [])
        self.assertEqual(len(seats_receipts.delivery_receipts(new["id"])), 1)

    def test_sessionless_receipt_keeps_frozen_attribution_after_rename(self):
        seats.write_roster("alice", session=None, cwd="/tmp/p")
        row = chat.post("@all sessionless", who="owner")
        frozen = row["delivery_census"]["recipients"][0]["recipient"]
        ok, message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok, message)
        seats_receipts.record_delivery_receipt(
            row, "main", "renamed", None, "hook")
        receipts = seats_receipts.delivery_receipts(row["id"])
        self.assertEqual([item["recipient"] for item in receipts], [frozen])

    def test_selector_refuses_hit_when_another_lane_is_unreadable(self):
        row = chat.post("@all visible", who="owner")
        os.makedirs(chat.room_path("bad"))
        with mock.patch("helm.seats_ack._all_lanes",
                        return_value=(["main", "bad"], [])):
            _row, _room, _occurrence, err = seats_receipts._locate_receipt_row(
                row["id"])
        self.assertIn("cannot prove a unique", err)

    def test_selector_finds_a_receipt_after_the_old_256_lane_bound(self):
        target = "zz-receipt-target"
        row = chat.post("@all visible", room=target, who="owner")
        lanes = ["lane-%03d" % value
                 for value in range(16 * seats.ROOM_SCAN_CAP)]
        for lane in lanes:
            pk.atomic_write(chat.room_path(lane), "")
        lanes.append(target)
        self.assertEqual(len(lanes), 257)
        old_budget = 16 * seats.ROOM_SCAN_CAP \
            * seats_receipts._RECEIPT_LANE_COST
        with mock.patch("helm.seats_ack._all_lanes",
                        return_value=(lanes, [])), \
                mock.patch("helm.seats_receipts._RECEIPT_SCAN_BYTES",
                           old_budget):
            _row, _room, _occurrence, old_err = \
                seats_receipts._locate_receipt_row(row["id"])
        self.assertIn("bounded census", old_err)
        with mock.patch("helm.seats_ack._all_lanes",
                        return_value=(lanes, [])):
            found, room, occurrence, err = seats_receipts._locate_receipt_row(
                row["id"])
        self.assertIsNone(err)
        self.assertEqual(found["id"], row["id"])
        self.assertEqual(room, target)
        self.assertTrue(occurrence)

    def test_selector_refuses_a_lane_census_over_the_explicit_bound(self):
        cost = seats_receipts._RECEIPT_LANE_COST
        lanes = ["lane-%d" % value for value in range(4)]
        with mock.patch("helm.seats_ack._all_lanes",
                        return_value=(lanes, [])), \
                mock.patch("helm.seats_receipts._RECEIPT_SCAN_BYTES",
                           3 * cost):
            _row, _room, _occurrence, err = seats_receipts._locate_receipt_row(
                "deadbeef")
        self.assertIn("bounded census", err)

    def test_selector_census_holds_rename_topology_until_scan_finishes(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row, err = seats.dm("alice", "private", who="owner")
        self.assertIsNone(err)
        entered, release, selected, renamed = (
            threading.Event(), threading.Event(), [], [])
        from helm import seats_ack
        real = seats_ack._all_lanes

        def blocked():
            lanes = real()
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            return lanes

        with mock.patch("helm.seats_ack._all_lanes", side_effect=blocked):
            selector = threading.Thread(target=lambda: selected.append(
                seats_receipts._locate_receipt_row(row["id"])))
            selector.start()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            renamer = threading.Thread(target=lambda: renamed.append(
                seats.rename_seat("alice", "renamed")))
            renamer.start()
            renamer.join(0.05)
            self.assertTrue(renamer.is_alive())
            release.set()
            selector.join(THREAD_TIMEOUT)
            renamer.join(THREAD_TIMEOUT)
        self.assertFalse(selector.is_alive())
        self.assertFalse(renamer.is_alive())
        self.assertIsNone(selected[0][3])
        self.assertEqual(selected[0][0]["id"], row["id"])
        self.assertTrue(renamed[0][0], renamed[0][1])

    def test_receipt_snapshot_holds_room_lock_against_rotation(self):
        chat.post("dropped by the cut", who="owner")   # a real cut (task/2931)
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row = chat.post("@all stable snapshot", who="owner")
        entered, release, rotated = threading.Event(), threading.Event(), []
        real = seats_receipts._read_receipts

        def blocked(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            return real(*args, **kwargs)

        with mock.patch("helm.seats_receipts._read_receipts", side_effect=blocked):
            rendering = threading.Thread(target=lambda:
                seats_receipts.render_delivery_receipts(row["id"]))
            rendering.start()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            rotating = threading.Thread(target=lambda: rotated.append(
                chat._rotate(chat.room_path("main"), cap=0, room="main")))
            rotating.start()
            rotating.join(0.05)
            self.assertTrue(rotating.is_alive())
            release.set()
            rendering.join(THREAD_TIMEOUT)
            rotating.join(THREAD_TIMEOUT)
        self.assertFalse(rendering.is_alive())
        self.assertEqual(rotated, [True])


class DMTest(SeatsBase):
    """The 1:1 lane (premise exact-token-addressee-match): session/seat-keyed,
    exactly one recipient, zero room fanout, renders as a DM, signs like a
    post."""

    def test_stale_sender_follows_five_acyclic_rename_edges(self):
        lanes = [chat.DM_PREFIX + "edge-%d" % value for value in range(6)]
        chat._ensure_dir()
        for old, new in zip(lanes, lanes[1:]):
            pk.atomic_write(chat._dm_redirect_path(old), new)
        row = {"id": "four-edges", "ts": "t", "from": "owner",
               "dm": "target", "text": "private"}
        self.assertEqual(chat._append(dict(row), lanes[0])["id"], "four-edges")
        self.assertEqual(chat.read(lanes[-1])[0][0]["text"], "private")

    def test_stale_sender_refuses_a_true_redirect_cycle(self):
        lanes = [chat.DM_PREFIX + "cycle-a", chat.DM_PREFIX + "cycle-b"]
        chat._ensure_dir()
        pk.atomic_write(chat._dm_redirect_path(lanes[0]), lanes[1])
        pk.atomic_write(chat._dm_redirect_path(lanes[1]), lanes[0])
        with self.assertRaisesRegex(OSError, "redirect cycle"):
            chat._append({"id": "cycle", "ts": "t", "from": "owner",
                          "dm": "target", "text": "private"}, lanes[0])

    def test_stale_sender_refuses_redirect_depth_exhaustion(self):
        from helm.seats_common import ROOM_SCAN_CAP
        lanes = [chat.DM_PREFIX + "depth-%d" % value
                 for value in range(ROOM_SCAN_CAP + 1)]
        chat._ensure_dir()
        for old, new in zip(lanes, lanes[1:]):
            pk.atomic_write(chat._dm_redirect_path(old), new)
        with self.assertRaisesRegex(OSError, "depth exceeds bounded scan"):
            chat._append({"id": "depth", "ts": "t", "from": "owner",
                          "dm": "target", "text": "private"}, lanes[0])

    def test_malformed_redirect_refuses_instead_of_writing_old_lane(self):
        lane = chat.DM_PREFIX + "broken"
        chat._ensure_dir()
        with open(chat._dm_redirect_path(lane), "w", encoding="utf-8") as f:
            f.write("not a dm lane")
        with self.assertRaisesRegex(OSError, "redirect is malformed"):
            chat._append({"id": "bad-route", "ts": "t", "from": "owner",
                          "dm": "broken", "text": "private"}, lane)
        self.assertFalse(os.path.exists(chat.room_path(lane)))

    def test_unreadable_redirect_refuses_instead_of_writing_old_lane(self):
        lane = chat.DM_PREFIX + "blocked"
        redirect = chat._dm_redirect_path(lane)
        chat._ensure_dir()
        pk.atomic_write(redirect, chat.DM_PREFIX + "target")
        real_open = open

        def denied(path, *args, **kwargs):
            if path == redirect:
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        with mock.patch("helm.chat.open", side_effect=denied, create=True), \
                self.assertRaisesRegex(OSError, "redirect is unreadable"):
            chat._append({"id": "bad-read", "ts": "t", "from": "owner",
                          "dm": "blocked", "text": "private"}, lane)
        self.assertFalse(os.path.exists(chat.room_path(lane)))

    def test_stale_keyed_dm_retry_keeps_original_id_after_rename(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        first = chat.post("keyed", who="owner", dm="alice", event_id="op-1",
                          sign=False)
        ok, message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok, message)
        retry = chat.post("keyed", who="owner", dm="alice", event_id="op-1",
                          sign=False)
        self.assertEqual(retry["id"], first["id"])
        lane = chat.DM_PREFIX + seats._seat_key("renamed")
        self.assertEqual([row["id"] for row in chat.read(lane)[0]], [first["id"]])

    def test_stale_keyed_dm_retry_survives_rename_and_rotation(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        first = chat.post("keyed", who="owner", dm="alice", event_id="op-1",
                          sign=False)
        ok, message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok, message)
        filler = chat.post("filler", who="owner", dm="renamed", sign=False)
        while seats.deliver_any(session="s-a", seat="renamed") is not None:
            pass
        lane = chat.DM_PREFIX + seats._seat_key("renamed")
        self.assertTrue(chat._rotate(chat.room_path(lane), cap=0, room=lane))
        self.assertNotIn(first["id"], [row["id"] for row in chat.read(lane)[0]])
        retry = chat.post("keyed", who="owner", dm="alice", event_id="op-1",
                          sign=False)
        self.assertEqual(retry["id"], first["id"])
        self.assertEqual([row["id"] for row in chat.read(lane)[0]], [filler["id"]])

    def test_stale_dm_reply_resolves_parent_in_redirect_target(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        parent = chat.post("parent", who="owner", dm="alice")
        ok, message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok, message)
        reply = chat.post("child", who="bob", dm="alice",
                          reply_to=parent["id"])
        self.assertEqual(reply["reply_to"], parent["id"])
        self.assertEqual(reply["rfrom"], "owner")
        self.assertEqual(reply["rts"], parent["ts"])

    def test_dm_reply_recovers_pre_redirect_rename_before_parent_lookup(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        parent = chat.post("parent", who="owner", dm="alice")
        old_lane = chat.DM_PREFIX + seats._seat_key("alice")
        real = seats_rename.pk.atomic_write

        def crash(path, value):
            if path == chat._dm_redirect_path(old_lane):
                raise SystemExit("before redirect")
            return real(path, value)

        with mock.patch("helm.seats_rename.pk.atomic_write", side_effect=crash):
            with self.assertRaises(SystemExit):
                seats.rename_seat("alice", "renamed")
        self.assertFalse(os.path.exists(chat.room_path(old_lane)))
        reply = chat.post("child", who="bob", dm="alice",
                          reply_to=parent["id"])
        self.assertEqual(reply["reply_to"], parent["id"])
        self.assertEqual(reply["rfrom"], "owner")
        self.assertEqual(reply["rts"], parent["ts"])
        self.assertTrue(os.path.exists(chat.room_path(old_lane)))

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

    def test_stale_old_lane_writer_redirects_after_rename(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        old_room = chat.DM_PREFIX + seats._seat_key("alice")
        ok, _message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok)
        row = {"id": "stale", "ts": "t", "from": "owner",
               "text": "stale sender", "dm": "alice"}
        chat._append(row, old_room)
        new_room = chat.DM_PREFIX + seats._seat_key("renamed")
        self.assertEqual(chat.read(old_room)[1], 0)
        self.assertEqual(chat.read(new_room)[0][-1]["id"], "stale")

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
        # a DM gates the stop; the block no longer tags the row "[dm]"
        # (task 692 dropped samples) — it fired, and the count is pinned below
        self.assertIn("undelivered message(s)", blocks[0])
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
        _tmp_declare(self, "ada")
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
        chat.post("owner in proj-b", who="daria", origin="web", room="proj-b")
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
        chat.post("owner speaks", who="daria")          # owner is not a seat
        self.assertFalse(os.path.exists(seats.seen_path("daria")))
        self.assertNotIn("daria", seats.roster())

    def test_muted_deliver_still_refreshes_presence(self):
        seats.join(session="s-rt2", seat="rt-muted", cwd="/tmp/p")
        os.remove(seats.seen_path("rt-muted"))
        os.environ["HELM_CHAT_DELIVER"] = "0"            # delivery muted…
        self.assertIsNone(seats.deliver(session="s-rt2"))   # …so no nudge…
        self.assertTrue(os.path.exists(seats.seen_path("rt-muted")))  # …still alive


class BeaconProcsBase(SeatsBase):
    """A synthetic /proc tree: `proc` writes one whole entry (cmdline,
    environ, stat and comm), and LAUNCHER is the pid of the live bash
    wrapper a healthy beacon parents to.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

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


class BeaconProcsTest(BeaconProcsBase):
    """The DETECTION half of the armed-beacon gate, against a synthetic /proc
    tree — proof it reads the real process table (rearm.py's exact argv shape)
    rather than guessing from env. Ground-truthed live at build time against 10
    real beacons of the form `python3 .../helm chat wait --seat X --follow`.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses BeaconProcsBase."""

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


class BeaconRearmRungTest(BeaconProcsBase):
    """GUARD 1's OTHER HALF — the unlatched per-turn rung that closes the gap
    between "a turn happened" and "a wake path exists".

    THE GAP IS A MEASUREMENT. A live census of nineteen seats read twelve DEAF,
    and five of those twelve had stamped a presence beat AFTER their deaf spell
    began — one with its own tool-call records written thirteen hours into a
    fourteen-hour spell. Only a turn produces either, so those seats took turns
    and re-armed nothing: the standing advice to WAIT, because a seat re-arms
    on its next turn, describes an event that keeps happening without helping.

    BOTH DIRECTIONS OVER ONE PROCESS TABLE, differing by exactly one process.
    A rung that is only shown firing proves nothing about the seat it must
    leave alone, and an absence arm with no control cannot tell "correctly
    silent" from "never speaks" — so the DEAF arm below is the unconditional
    positive control for the COVERED arm, taken on the same observable (the
    rung's own return value) in the same call, against the same fixture with
    the beacon removed."""

    SID = "aaaaaaaa-1111-2222-3333-444444444444"

    def seat_up(self, name="seat-a"):
        """A LAUNCHED fleet seat: the launch seam's stamp plus a roster row."""
        os.environ["HELM_CHAT_NAME"] = name
        seats.join(seat=name, cwd="/tmp/p")

    def rung(self, root, live=None, seat="seat-a"):
        """The rung's answer with the PROCESS TABLE pointed at `root`.

        THE REAL PREDICATE RUNS. Only the process table and the session census
        are redirected; `beacon_procs(strict=True)` and `beacons.classify` are
        the production ones, so what these arms exercise is the decision this
        rung actually makes rather than a stub agreeing with its author."""
        real = seats_stop_signals.beacon_procs

        def probe(name, proc_dir=None, strict=False):
            return real(name, root, strict=strict)

        with mock.patch.object(seats_stop_signals, "beacon_procs", probe), \
                mock.patch.object(beacons, "live_sessions",
                                  return_value=live or {}):
            return seats_stop_guard._rearm_rung(self.SID, seat)

    def waiter(self):
        return self.proc(108, ["python3", "/x/bin/helm", "chat", "wait",
                               "--seat", "seat-a", "--follow"],
                         env=["CLAUDE_CODE_SESSION_ID=%s" % self.SID])

    def test_a_live_beacon_is_untouched_and_a_dead_one_is_told(self):  # noqa: VACUOUS_ASSERTION — the DEAF arm above the assertIsNone is its unconditional control, same observable, same call
        self.seat_up()
        root = self.waiter()
        covered = self.rung(root, live={self.SID: 4242})
        shutil.rmtree(os.path.join(root, "108"))        # the beacon dies
        deaf = self.rung(root, live={self.SID: 4242})
        unproven = self.rung(os.path.join(self.tmp, "no-such-proc"))
        # THE CONTROL FIRST: the string is producible on this observable.
        self.assertIn("NO PROVEN WAKE PATH", deaf)
        self.assertIn("seat 'seat-a'", deaf)
        self.assertIn("NO live `helm chat wait` process", deaf)
        self.assertIn('Monitor(command: "helm chat wait --seat seat-a '
                      '--follow", timeout_ms: 1800000)', deaf)
        self.assertIn("A TURN DOES NOT ARM IT", deaf)
        self.assertIn("select:Monitor", deaf)
        self.assertIn("NOT latched", deaf)
        # …and only then: the seat that HAS one is not disturbed.
        self.assertIsNone(covered)
        # UNREADABLE IS NOT COVERED. The probe that cannot answer still speaks,
        # and says which of the two it is.
        self.assertIn("NO PROVEN WAKE PATH", unproven)
        self.assertIn("could not be PROVEN", unproven)
        self.assertIn("unlistable", unproven)

    def test_a_probe_that_raises_speaks_instead_of_going_quiet(self):
        """FAIL LOUD. A rung whose instrument breaks must not hand the seat the
        same silence it gives a proven-live beacon."""
        self.seat_up()
        def boom(name, proc_dir=None, strict=False):
            raise OSError("probe exploded")
        with mock.patch.object(seats_stop_signals, "beacon_procs", boom):
            line = seats_stop_guard._rearm_rung(self.SID, "seat-a")
        self.assertIn("NO PROVEN WAKE PATH", line)
        self.assertIn("the beacon probe raised OSError", line)

    def test_a_process_that_is_not_a_fleet_seat_owes_nothing(self):  # noqa: VACUOUS_ASSERTION — `stamped` is the unconditional control on this observable
        """The same door `_beacon_block` uses: no launch-seam stamp, no rung.
        Controlled by the stamped case, so a rung that never speaks at all
        cannot pass this."""
        self.seat_up()
        stamped = self.rung(os.path.join(self.tmp, "no-such-proc"))
        os.environ.pop("HELM_CHAT_NAME", None)
        self.assertIn("NO PROVEN WAKE PATH", stamped)
        self.assertIsNone(self.rung(os.path.join(self.tmp, "no-such-proc")))

    def test_the_line_reaches_the_seat_on_BOTH_exits(self):
        """A finding routed onto the warn channel alone is dropped whole by the
        refusal exit, so on any stop where ANOTHER rung blocks the seat would
        learn nothing about being unreachable — and a refused stop is exactly
        when it is most likely to go idle owing work."""
        allowed, refused = io.StringIO(), io.StringIO()
        seats_stop_seam.emit_warns(["REARM-LINE"], stream=allowed)
        seats_stop_seam.survives_refusal("REARM-LINE")
        seats_stop_seam.emit_blocks(["ANOTHER RUNG'S BLOCK"], stream=refused)
        self.assertIn("REARM-LINE", allowed.getvalue())
        self.assertIn("ANOTHER RUNG'S BLOCK", refused.getvalue())
        self.assertIn("REARM-LINE", refused.getvalue())
        # AND IT IS SPENT BY THE EMISSION, never printed twice from the queue.
        again = io.StringIO()
        seats_stop_seam.emit_blocks(["ANOTHER RUNG'S BLOCK"], stream=again)
        self.assertIn("ANOTHER RUNG'S BLOCK", again.getvalue())
        self.assertNotIn("REARM-LINE", again.getvalue())


class BeaconGateTest(SeatsBase):
    """GUARD 1 — a turn must not end with no armed beacon (the restarted-
    integrator incident: the seat's Monitor died with the old process, was
    never re-armed, and the seat ran BLIND for ~1.5h accumulating 98
    undelivered rows while missing a gate verdict it was waiting on).

    Hermetic: HELM_CHAT_NAME is the launch-seam stamp, beacon_procs is stubbed
    so no test ever depends on the host's real process table."""

    def guard(self, payload=None, args=(), pids=(), trouble=None,
              obligation=True):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        with mock.patch.object(seats, "beacon_procs",
                               return_value=(list(pids), trouble)), \
                mock.patch.object(seats_stop_signals, "_beacon_obligation",
                                  return_value=obligation):
            return self.cmd("stop-guard", ["--hook-json", *args],
                            stdin=stdin or b"{}")

    def obligation(self, rid="d1", sender="builder", recipient="oi",
                   status="open", supersedes=None, root=None):
        return {"id": rid, "status": status, "sender": sender,
                "recipient": recipient, "supersedes": supersedes,
                "chain_root": root or rid}

    def obligation_state(self, rows, unavailable=None):
        snap = {row["id"]: row for row in rows}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(snap, unavailable)):
            return seats_stop_signals._beacon_obligation("oi")

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
                      'timeout_ms: 1800000)', err)
        self.assertIn("re-arm it in the same turn", err)
        self.assertIn("select:Monitor", err)      # …and the DEFERRED escape
        self.assertIn("98 undelivered", err)      # why, from the real incident
        self.assertIn("holds owed dispatch work", err)

    def test_the_rearm_rung_is_REACHED_by_the_real_ladder(self):
        """A rung nobody calls is the defect the unit arms cannot see. This
        drives the verb, and it drives it on the exit that DISCARDS warns — so
        what it proves is that the line arrives through the whole guard, on the
        stop where the old channel would have dropped it.

        The second stop is the control for the FIRST one's latch: the block
        above fires once per unchanged missing state, so a re-stop passes — and
        this rung, which has no latch, must still speak on that pass. That is
        the whole cure, since every deaf seat measured had already been told
        once and heard nothing on every turn after."""
        self.seat_up()
        sid = {"session_id": "s-rearm"}
        rc, _o, refused = self.guard(sid)
        self.assertEqual(rc, 2)                 # `_beacon_block` refuses here
        self.assertIn("NO PROVEN WAKE PATH", refused)
        rc, _o, allowed = self.guard(sid)
        self.assertEqual(rc, 0, allowed)        # the block's latch is spent…
        self.assertNotIn("NO ARMED BEACON", allowed)
        self.assertIn("NO PROVEN WAKE PATH", allowed)   # …this one is not

    def test_readable_zero_makes_beacon_optional_and_new_work_rearms(self):
        self.seat_up()
        sid = {"session_id": "s-b0"}
        rc, _o, err = self.guard(sid, obligation=True)
        self.assertEqual(rc, 2)
        self.assertIn("NO ARMED BEACON", err)
        rc, _o, err = self.guard(sid, obligation=False)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO ARMED BEACON", err)  # noqa: VACUOUS_ASSERTION — both adjacent calls must hit
        rc, _o, err = self.guard(sid, obligation=True)
        self.assertEqual(rc, 2)
        self.assertIn("NO ARMED BEACON", err)
        self.assertIn("holds owed dispatch work", err)

    def test_sender_or_recipient_owed_work_requires_the_beacon(self):
        received = [self.obligation(recipient="oi")]
        sent = [self.obligation(sender="oi", recipient="reviewer")]
        self.assertEqual(len(received), 1)
        self.assertEqual(len(sent), 1)
        self.assertTrue(self.obligation_state(received))
        self.assertTrue(self.obligation_state(sent))

    def test_terminal_held_and_carried_parents_are_not_owed(self):  # noqa: VACUOUS_ASSERTION — open control first
        self.assertTrue(self.obligation_state([self.obligation()]))
        closed = [self.obligation("fixed", status="verdict"),
                  self.obligation("cancelled", status="cancelled"),
                  self.obligation("held", status="held")]
        self.assertFalse(self.obligation_state(closed))
        parent = self.obligation("parent", recipient="oi", root="chain")
        child = self.obligation("child", sender="other", recipient="other",
                                supersedes="parent", root="chain")
        self.assertFalse(self.obligation_state([parent, child]))

    def test_unreadable_obligations_fail_closed_with_the_reason(self):  # noqa: VACUOUS_ASSERTION — readable control first
        self.seat_up()
        self.assertTrue(self.obligation_state([self.obligation()]))
        self.assertIsNone(self.obligation_state([], unavailable="ledger unreadable"))
        rc, _o, err = self.guard({"session_id": "s-bu"}, obligation=None)
        self.assertEqual(rc, 2)
        self.assertIn("NO ARMED BEACON", err)
        self.assertIn("UNREADABLE dispatch ledger", err)
        self.assertIn("absence of owed work is unproven", err)

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
        chat.post("@wisp land the fix", who="daria")
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

    def guard(self, sid, args=(), cwd=None):
        payload = {"session_id": sid}
        if cwd is not None:
            payload["cwd"] = cwd
        return self.cmd("stop-guard", ["--hook-json", *args],
                        stdin=json.dumps(payload).encode())

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

    def record_run(self, sid, command, cwd, exit_code):
        event = {"session_id": sid, "tool_name": "Bash", "cwd": cwd,
                 "hook_event_name": "PostToolUse",
                 "tool_input": {"command": command, "cwd": cwd},
                 "tool_response": {"exitCode": exit_code}}
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(event)

    def test_red_gate_fires_once_then_rearms_on_a_new_red_run(self):
        seats.join(session="s-r1", seat="wisp", cwd="/tmp/p")
        self.plant_runs("s-r1", {"token": "pytest", "exit": 1, "digest": "aaa"})
        rc, _o, err = self.guard("s-r1")
        self.assertEqual(rc, 2)
        self.assertIn("last recorded run", err)
        self.assertIn("pytest", err)
        self.assertIn("exited 1", err)
        rc, _o, err = self.guard("s-r1")
        self.assertEqual(rc, 0, err)                 # same red state: latched
        self.plant_runs("s-r1", {"token": "pytest", "exit": 1, "digest": "bbb"},
                        mode="a")                    # a NEW red run re-arms once
        rc, _o, err = self.guard("s-r1")
        self.assertEqual(rc, 2)
        self.assertIn("last recorded run", err)

    def test_green_rerun_silences_the_red_gate(self):
        seats.join(session="s-r2", seat="wisp", cwd="/tmp/p")
        self.plant_runs("s-r2", {"token": "pytest", "exit": 1, "digest": "aaa"},
                        {"token": "pytest", "exit": 0, "digest": "ccc"})
        rc, _o, err = self.guard("s-r2")
        self.assertEqual(rc, 0, err)                 # latest per token is green
        self.assertNotIn("stop-whisper", err)

    def test_script_spellings_share_one_resolved_identity_and_green_clears_red(self):
        root = os.path.join(self.tmp, "repo")
        sub = os.path.join(root, "fab")
        script = os.path.join(sub, "test", "focused-routing.sh")
        os.makedirs(os.path.dirname(script))
        with open(script, "w") as f:
            f.write("#!/bin/sh\n")
        forms = (
            (":fab/test/focused-routing.sh", root, 0),
            ("bash fab/test/focused-routing.sh", root, 143),
            ("sh -c 'fab/test/focused-routing.sh'", root, 137),
            (script, sub, 144),
            ("test/focused-routing.sh", sub, 0),
        )
        self.plant_runs("s-rpath", *(
            {"token": token, "identity": record.runner_identity(token, cwd),
             "cwd": cwd, "exit": code, "digest": "p%d" % i}
            for i, (token, cwd, code) in enumerate(forms)))
        latest = seats._runner_latest("s-rpath")
        self.assertEqual(len(latest), 1)
        self.assertEqual(next(iter(latest.values()))["exit"], 0)
        self.assertEqual(
            seats._runner_identity("fab/test/focused-routing.sh", root),
            seats._runner_identity("test/focused-routing.sh", sub))
        self.assertEqual(
            seats._runner_identity("bash fab/test/focused-routing.sh", root),
            seats._runner_identity(script, root))
        link = os.path.join(root, "focused-routing-link.sh")
        os.symlink(script, link)
        self.assertEqual(seats._runner_identity(link, root),
                         seats._runner_identity(script, root))
        shutil.rmtree(root)
        latest = seats._runner_latest("s-rpath")
        self.assertEqual(len(latest), 1)
        self.assertEqual(next(iter(latest.values()))["exit"], 0)
        seats.join(session="s-rpath", seat="wisp", cwd=root)
        rc, _o, err = self.guard("s-rpath", cwd=root)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stop-whisper", err)

    def test_runner_identity_refuses_to_merge_distinct_or_malformed_tokens(self):
        root = os.path.join(self.tmp, "repo")
        paths = [os.path.join(root, d, "run-test.sh") for d in ("a", "b")]
        for path in paths:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n")
        ids = [seats._runner_identity(path, root) for path in paths]
        self.assertNotEqual(ids[0], ids[1])
        self.plant_runs(
            "s-distinct",
            {"token": paths[0], "identity": ids[0], "cwd": root,
             "exit": 1, "digest": "red-a"},
            {"token": paths[1], "identity": ids[1], "cwd": root,
             "exit": 0, "digest": "green-b"})
        latest = seats._runner_latest("s-distinct")
        self.assertEqual(len(latest), 2)
        self.assertEqual(seats._gate_candidate(latest)[0], "redgate:red-a:1")
        self.assertEqual(seats._runner_identity("pytest -q", root),
                         seats._runner_identity("pytest tests", root))
        self.assertEqual(seats._runner_identity("python -u -m unittest x", root),
                         seats._runner_identity("python -m unittest y", root))
        self.assertEqual(
            seats._runner_identity(":missing/run-test.sh", root),
            seats._runner_identity("bash missing/run-test.sh", root))
        self.assertEqual(
            seats._runner_identity(":fab/test/focused-routing.sh"),
            seats._runner_identity("bash fab/test/focused-routing.sh"))
        self.assertNotEqual(seats._runner_identity("pytest", root),
                            seats._runner_identity("tox", root))
        self.assertNotEqual(seats._runner_identity("just check", root),
                            seats._runner_identity("just test", root))
        self.assertNotEqual(seats._runner_identity("bash 'unterminated", root),
                            seats._runner_identity("pytest", root))

    def test_legacy_rows_normalize_without_stop_cwd_or_live_filesystem(self):
        rows = (
            {"token": ":fab/test/focused-routing.sh", "exit": 1,
             "digest": "old-red"},
            {"token": "bash fab/test/focused-routing.sh", "exit": 143,
             "digest": "old-kill"},
            {"token": "fab/test/focused-routing.sh", "exit": 0,
             "digest": "old-green"},
            {"token": "pytest -q", "exit": 1, "digest": "py-red"},
            {"token": "pytest tests", "exit": 0, "digest": "py-green"},
        )
        self.plant_runs("s-legacy", *rows)
        latest = seats._runner_latest("s-legacy", "/unrelated/stop/cwd")
        self.assertEqual(len(latest), 2)
        self.assertEqual(sorted(r["exit"] for r in latest.values()), [0, 0])

    def test_recorder_through_rung_preserves_targets_and_effective_script_path(self):
        root = os.path.join(self.tmp, "repo")
        for d in ("a", "b"):
            path = os.path.join(root, d, "run-test.sh")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n")
        self.record_run("s-cd", "cd a && ./run-test.sh", root, 1)
        self.record_run("s-cd", "cd b && ./run-test.sh", root, 0)
        latest = seats._runner_latest("s-cd")
        self.assertEqual(len(latest), 2)
        self.assertIsNotNone(seats._gate_candidate(latest))

        self.record_run("s-quote", "bash a/run-test.sh", root, 1)
        self.record_run("s-quote", "sh -c 'a/run-test.sh'", root, 0)
        latest = seats._runner_latest("s-quote")
        self.assertEqual(len(latest), 1)
        self.assertIsNone(seats._gate_candidate(latest))

        self.record_run("s-npm", "npm run test:unit", root, 1)
        self.record_run("s-npm", "npm run test:e2e", root, 0)
        latest = seats._runner_latest("s-npm")
        self.assertEqual(len(latest), 2)
        self.assertIsNotNone(seats._gate_candidate(latest))

    def test_legacy_migration_and_absolute_identity_ignore_current_filesystem(self):
        root = os.path.join(self.tmp, "repo")
        path = os.path.join(root, "run-test.sh")
        os.makedirs(root)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
        self.plant_runs("s-migrate", {"token": "run-test.sh", "exit": 1,
                                      "digest": "legacy-red"})
        self.record_run("s-migrate", "bash run-test.sh", root, 0)
        self.assertIsNone(seats._gate_candidate(
            seats._runner_latest("s-migrate")))

        target = os.path.join(root, "target.sh")
        with open(target, "w") as f:
            f.write("#!/bin/sh\n")
        os.unlink(path)
        os.symlink(target, path)
        before = record.runner_identity(path, resolve=False)
        os.unlink(path)
        os.symlink(os.path.join(root, "other.sh"), path)
        after = record.runner_identity(path, resolve=False)
        self.assertEqual(before, after)

    def test_negative_subprocess_returncodes_are_no_verdict(self):
        latest = {"kill": {"token": "pytest", "exit": -9, "digest": "k"},
                  "int": {"token": "tox", "exit": -2, "digest": "i"}}
        self.assertIsNone(seats._gate_candidate(latest))

    def test_positive_exits_keep_numeric_truth_and_whisper_only_known_facts(self):
        for code in (1, 2, 137, 143, 144):
            with self.subTest(code=code):
                _fp, line = seats._gate_candidate({
                    ("token", "pytest"): {
                        "token": "pytest", "exit": code,
                        "digest": "x%d" % code, "ts": time.time()}})
                self.assertIn("last recorded run", line)
                self.assertIn("exited %d" % code, line)
                self.assertIn("no later green run of the same runner found", line)
                self.assertNotIn("gate ran RED", line)
                self.assertNotIn("failed", line.lower())
                self.assertNotIn("fix", line.lower())
                self.assertNotIn("no green rerun since", line)

    def test_old_positive_exit_is_reported_as_historical_not_live_or_absent(self):
        now = int(time.time())
        _fp, line = seats._gate_candidate({
            ("token", "tox"): {"token": "tox", "exit": 2,
                                "digest": "old", "ts": now - 13 * 3600}},
            now=now)
        self.assertIn("historical run", line)
        self.assertIn("exited 2", line)
        self.assertIn("over 12h ago", line)
        self.assertIn("no later green run of the same runner found", line)
        self.assertNotIn("rerun or surface before stopping", line)

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
        lines = [x for x in err.splitlines()
                 if x.startswith("[helm stop-whisper]")]
        self.assertEqual(len(lines), 1, err)
        line = lines[0]
        self.assertLessEqual(len(line), 260)   # frame + cap, still bounded
        # UNKNOWN, not SHARED: this fixture's guard never receives a cwd, so
        # the venue probe is blind and the honest venue word is UNKNOWN
        for must in ("UNKNOWN", "git diff --stat HEAD", "NOT yours",
                     "never commit"):
            self.assertIn(must, line, "clipped away: %s" % must)
        # THE INVARIANT: the advice precedes the boilerplate, so overflow can only
        # ever eat the note. Assert the ORDER, because that is what survives a
        # future re-wording — an index check cannot be satisfied by accident.
        self.assertLess(line.index("never commit"),
                        line.index("holds once per state"))

    def test_red_gate_owns_the_stop_over_unbanked(self):
        seats.join(session="s-b2", seat="wisp", cwd="/tmp/p")
        self.plant("s-b2", **{"last-dirty": 1})
        self.plant_edits("s-b2", "seats.py")
        self.plant_runs("s-b2", {"token": "vitest", "exit": 0, "digest": "ggg"},
                        {"token": "pytest", "exit": 2, "digest": "rrr"})
        rc, _o, err = self.guard("s-b2")
        self.assertEqual(rc, 2)
        self.assertEqual(err.count("[helm stop-whisper]"), 1)  # one line per stop
        self.assertIn("last recorded run", err)           # salience: red wins
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

    def test_a_dirty_flag_from_ANOTHER_TREE_does_not_speak_for_this_one(self):  # noqa: VACUOUS_ASSERTION — the two assertIsNotNone calls above the absence are unconditional positive controls on the SAME observable (this predicate's return), one for an unrecorded root and one for a matching root; the rung credits a control only from the same CALL's channels and cannot see across three calls to one function
        """A DIRTY OBSERVATION CARRIES THE TREE IT WAS TAKEN IN, OR IT IS MUTE.

        The flag is probed in the dirtying TOOL'S workdir, which on a seat
        holding lane rooms is routinely a worktree and not the shared checkout.
        Without the tree, this rung named the SHARED checkout over a lane's
        dirt and sent the reader to a `git diff --stat HEAD` that comes back
        EMPTY -- then offered add -u, LEAVE IT, or stash create, none of which
        fits a premise that is not true.

        DRIVEN AT THE PREDICATE, NOT THROUGH THE WHOLE GUARD, deliberately.
        The ladder emits ONE whisper per stop and a second session in the same
        fixture never reaches this rung at all, so a guard-level arm passes
        whether or not the cure exists -- measured: it survived a mutation
        that disabled the cure outright. The rung's guard-level wiring is
        already pinned by its sibling arms above; what is new here is the
        predicate, so that is what is asserted.
        """
        repo = self.repo("docs/a.md")
        latest = {"r": {"token": "pytest", "exit": 0, "digest": "ggg", "ts": 1}}
        args = (True, ["seats.py"], latest)

        # CONTROL: no recorded root at all -- the pre-field state still speaks,
        # or this rung would go out of service for every legacy observation.
        self.assertIsNotNone(
            seats_stop_signals._unbanked_candidate(*args, root=None, here=repo),
            "control: an unrecorded root must still whisper")

        # CONTROL: recorded root IS this tree -- it speaks.
        self.assertIsNotNone(
            seats_stop_signals._unbanked_candidate(*args, root=repo, here=repo),
            "control: an observation OF this tree must whisper")

        # THE CURE: recorded somewhere else -- silent about this checkout.
        self.assertIsNone(
            seats_stop_signals._unbanked_candidate(
                *args, root=os.path.join(repo, "..", "other-tree"), here=repo),
            "a lane room's dirt must not be reported as this checkout's")

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

    def test_an_unreadable_ROSTER_offers_nothing_just_like_unreadable_claims(self):
        """THE POACHING PATH, MEASURED. _live_seats built its answer from the
        fail-open roster read, so a corrupt file produced an EMPTY live set —
        and by this rung's own rule, an absent recipient's work is stranded
        and therefore offerable. Measured on the live fleet: eleven live seats
        through the real roster, ZERO through a fail-open read. Every seat's
        work becomes takeable because one file could not be parsed.

        The claims half has failed closed since it was written, carrying the
        comment 'an unreadable claims file must never let either of them
        poach'. The roster half was taken on trust. This pins both to one
        law."""
        seats.join(session="s-offer", seat="ds4pro", cwd="/tmp/p")
        seats.join(session="s-owner", seat="codex", cwd="/tmp/p")
        old = time.time() - seats.QUIET_S - 60
        os.utime(seats.seen_path("codex"), (old, old))
        self.plant_dispatch("ab12cd34ef560011", "codex")

        # CONTROL FIRST, on the same call: with a READABLE roster and a
        # proven-dead waiter this row IS offerable, so the refusal below is
        # the unreadable roster speaking and not a rung that offers nothing.
        with mock.patch.object(seats, "beacon_procs", return_value=([], None)):
            rows = seats._offer_rows("ds4pro")
        self.assertEqual([r[0] for r in rows], ["ab12cd34"],
                         "control: this row must be offerable when readable")

        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write("{ this is not json\n")
        with mock.patch.object(seats, "beacon_procs", return_value=([], None)):
            rows = seats._offer_rows("ds4pro")
        self.assertEqual(rows, [],
                         "an unreadable roster offered another seat's work")

    def test_a_roster_path_that_cannot_be_RESOLVED_is_unknown_not_empty(self):
        """THE TRI-STATE HAD THE BUG IT EXISTS TO PREVENT, one layer up.
        roster_path() resolves the chat dir, and with a relative HELM_HOME and
        the process cwd removed os.path.abspath raises FileNotFoundError.
        Resolved INSIDE roster_checked's try, that landed in the missing-file
        branch and came back as PROVEN EMPTY with failed=False — the single
        thing this function exists to distinguish, reported as its opposite.

        It silently defeated the fail-closed offer above: a roster nobody can
        even NAME read as an empty one, so every seat looked absent and the
        rung went on offering. Naming the file is part of reading it, so the
        resolution is its own step now and it fails CLOSED."""
        gone = tempfile.mkdtemp(prefix="helm-test-nopath-")
        prior_cwd = os.getcwd()
        prior = {k: os.environ.get(k) for k in ("HELM_HOME", "HELM_CHAT_DIR")}

        def restore():
            os.chdir(prior_cwd)
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        try:
            # CONTROL FIRST, through the same call: a resolvable path answers
            # failed=False, so the True below is the missing cwd and not a
            # function that always refuses.
            _rows, failed = seats.roster_checked()
            self.assertFalse(failed, "control: a resolvable roster is not failed")
            # the CLAIMS reader carries the identical split, and it is the
            # function the roster fix was copied FROM — so its control belongs in
            # the same arm, proving both are honest before the cwd vanishes.
            self.assertEqual(seats._live_claims(), {},
                             "control: a resolvable claims file is proven-empty")

            os.chdir(gone)
            os.rmdir(gone)
            os.environ["HELM_HOME"] = "relative-home"
            os.environ.pop("HELM_CHAT_DIR", None)
            _rows, failed = seats.roster_checked()
            self.assertTrue(
                failed, "a path that cannot be resolved reported PROVEN EMPTY")
            # AND THE SIBLING. _live_claims returned {} — PROVEN NO CLAIMS — for a
            # path it could not resolve, so _session_holds_claim answered False
            # and the rung read an unreadable estate as an idle one. That is
            # exactly the state its own fail-closed contract promises to refuse.
            self.assertIsNone(seats._live_claims(),
                              "an unresolvable claims path reported NO CLAIMS")
            self.assertTrue(seats._session_holds_claim("s-any"),
                            "fail-closed contract broken: unsure read as idle")
        finally:
            # Restore BEFORE unittest tearDown removes the fixture cwd. addCleanup
            # runs AFTER tearDown; the former cleanup therefore tried the now-dead
            # prior_cwd, fell back to /tmp, and leaked both cwd and fixture env into
            # every later module (the transcript grep shim then refused outside a
            # repo and deep_search silently returned no hits).
            restore()

        self.assertEqual(os.getcwd(), prior_cwd,
                         "the deleted-cwd arm leaked its fallback into the suite")
        # THE KEYS THAT MOVED, NEVER THE MAPPING (task/2370): comparing dicts
        # built from os.environ renders their VALUES on failure.
        self.assertEqual(sorted(prior), ["HELM_CHAT_DIR", "HELM_HOME"])
        self.assertEqual([k for k in sorted(prior)
                          if os.environ.get(k) != prior[k]], [],
                         "the deleted-cwd arm leaked fixture env into the "
                         "suite at these KEYS")

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

    def test_autoclaim_survives_a_later_latch_write_failure(self):
        repo = self.repo()
        self.git(repo, "checkout", "-q", "-b", "review-tip")
        with open(os.path.join(repo, "change"), "w") as f:
            f.write("not landed\n")
        self.git(repo, "add", "change")
        self.git(repo, "commit", "-q", "-m", "review tip")
        tip = self.git(repo, "rev-parse", "HEAD")
        self.git(repo, "checkout", "-q", "main")
        session, seat = "s-ac-write", "seat-under-test"
        seats.join(session=session, seat=seat, cwd=repo)
        self.plant_dispatch("ac13ac13ac13ac13", seat, kind="review",
                            repo_id=os.path.join(repo, ".git"), tip=tip, ref=tip)
        latch = seats._stop_fp_path(
            "main", seat, session, kind="stopwhisper")
        real_write = pk.write_json
        faults = []

        def write(path, payload, *args, **kwargs):
            if path == latch:
                claim = seats._live_claims().get("dispatch:ac13ac13")
                self.assertIsInstance(claim, dict)
                self.assertEqual(claim.get("session"), session)
                faults.append(path)
                raise OSError("planted post-claim latch failure")
            return real_write(path, payload, *args, **kwargs)

        with mock.patch.object(pk, "write_json", side_effect=write):
            rc, _out, err = self.guard(
                {"session_id": session}, args=["--seat", seat])
        self.assertEqual(faults, [latch])
        self.assertEqual(rc, 2, err)
        self.assertIn("auto-claimed dispatch ac13ac13", err)
        self.assertIn("START it now", err)
        claim = seats._live_claims().get("dispatch:ac13ac13")
        self.assertEqual((claim.get("holder"), claim.get("session")),
                         (seat, session))
        self.assertTrue(claim.get("lease"))

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
        self.assertIn("last recorded run", err)           # the higher rung WINS…
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
        chat.post("@ds4pro look at this", who="daria")   # an undelivered mention
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
        session opens nothing without the lease token.

        BOTH CALLERS DECLARE alice, because `--seat` is an ASSERTION and a
        lease needs an admitted actor (task/994). That is faithful to the
        reproduction: B's whole advantage is knowing A's SESSION, and the
        point is that knowing it opens nothing. Making B a different seat
        would have proved a weaker thing — that a stranger is refused — and
        the arm is about the token, not the name."""
        # sA IS ALICE'S ROSTER-VISIBLE SID, so the roster has to say so — that
        # is the arm's own premise ("B knows A's roster-visible SID"), and
        # without the row A is not a seat and never gets to claim at all.
        self.addCleanup(_tmp_corroborate("alice", "sA"))
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sA",
                                          "HELM_CHAT_NAME": "alice"}):
            rc, out, _ = self.cmd("claim", ["port:1", "--seat", "alice"])
        self.assertEqual(rc, 0)
        # B knows sA (roster/API) and even sets it as their ambient session
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sA",
                                          "HELM_CHAT_NAME": "alice"}):
            rc, _out, err = self.cmd("release", ["port:1", "--seat", "alice"])
        self.assertEqual(rc, 1)
        self.assertIn("the lease id", err)
        # the old flag is dead: passing it changes nothing
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "alice"}):
            rc, _out, err = self.cmd("release", ["port:1", "--seat", "alice",
                                                 "--session", "sA"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(seats.claims_list()), 1)   # still held
        # the printed lease IS the confirmation token
        lease = out.split("lease ")[1].split(",")[0]
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sA",
                                          "HELM_CHAT_NAME": "alice"}):
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

    def test_the_seats_module_docstring_does_not_deny_council(self):
        """THE SECOND COPY OF THE CLAIM THE ARM BELOW PINS. chat.HELP said
        council was DEFERRED after it shipped and cost a reviewer a rebuild
        attempt; that copy was cured and pinned. The same sentence also lived
        in helm/seats.py's module docstring, where the pin below cannot see it
        — it reads chat.HELP — so it survived its sibling's cure by exactly
        the margin of what that arm inspects.

        IT ASSERTS THE CLAIM, NOT THE WORD. The cured docstring quotes the old
        sentence while recording that it is dead, so a bare "DEFERRED" search
        would fail against the fix and pass against a revert of it — an arm
        that is not merely weak but inverted.

        AND IT BINDS THE PROSE TO THE CODE IN BOTH DIRECTIONS, which is the
        part a one-sided absence check cannot do: the docstring may claim
        council ships only while council actually does, so removing the
        feature fails here too, naming the docstring that would have been left
        lying."""
        from helm import council, seats
        doc = seats.__doc__ or ""
        self.assertTrue(doc, "helm/seats.py has no module docstring, so this "
                             "arm inspects nothing")
        # THE PREMISE THE PROSE RESTS ON, checked first: if council stopped
        # shipping, the assertions below would pin a docstring that had itself
        # become the lie.
        # ALIASED TO A LOCAL FIRST, AND NOT AS A STYLE CHOICE: the vacuous
        # rung does not credit an observable reached through an ATTRIBUTE, so
        # asserting on `council.<verb>` directly costs this whole method its
        # coverage verdict — measured, and binding to a local clears it.
        shipped = [getattr(council, verb, None)
                   for verb in ("convene", "reveal", "abort", "status_lines")]
        self.assertTrue(all(callable(f) for f in shipped),
                        "a council verb is gone, so the seats docstring's "
                        "SHIPPED claim is now the stale one")
        self.assertIn("Council (embargoed verdicts) SHIPPED", doc,
                      "the seats module docstring no longer states that "
                      "council shipped, so a reader grepping it for council "
                      "learns nothing or learns the deferral")
        # THE THREE ASSERTION FORMS THE DEAD PARAGRAPH USED. Quoting them as
        # history is fine and the cure does exactly that; asserting them is
        # what must never come back.
        self.assertNotIn("is DEFERRED to 0.3", doc,
                         "the seats module docstring still asserts the "
                         "deferral about a feature that ships")
        self.assertNotIn("No live consumer today", doc,
                         "the seats module docstring still says council has "
                         "no consumer, and the council verbs are dispatchable")
        self.assertNotIn("record, don't build", doc,
                         "the seats module docstring still tells a reader not "
                         "to build what is already built")

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
        """Xrev, closed structurally: the snapshot version read a
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
        """DURABILITY (the independently-blocking finding, and the one I
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
        """Xrev: a judgment cast once the others are VISIBLE is not
        independent — the single property the embargo exists to guarantee."""
        from helm import council
        council.convene("crT", ["seat-a", "seat-b", "seat-c"], 1, threshold=1, tip="deadbeef")
        council.signal("crT", "seat-a", "YES", "deadbeef", "ev")
        council.reveal("crT")
        _r, err = council.signal("crT", "seat-b", "NO", "deadbeef", "ev")
        self.assertIn("REVEALED", err)

    def test_a_council_judges_ONE_artifact(self):
        """Xrev: signals binding DIFFERENT tips could reach 'quorum'
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
        _tmp_declare(self, "voter-a")
        council.convene("crL", ["voter-a", "voter-b"], 1, threshold=2,
                        convener="the-convener", tip="deadbeef")
        rc = seats._cmd_council("verdict", ["crL", "YES", "--tip", "deadbeef"])
        self.assertEqual(rc, 0)                    # the signal IS recorded...
        self.assertIn("voter-a", council.registry("crL")["signals"])
        rows, _t = chat.read("crL")                # ...and says NOTHING public
        self.assertEqual([r for r in rows if "signalled" in (r.get("text") or "")], [])
        self.assertEqual([r for r in rows if r.get("from") == "voter-a"], [])

    def test_quorum_is_a_reveal_bar_not_a_decision(self):
        """Re-gate: YES+NO reached reveal with no aggregate rule, so
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
        """Re-gate: abort-after-reveal and a repeated abort both
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
        """Durability re-gate: a planted row with the right member,
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
        """Durability re-gate: the VERB checked membership but the
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
        """Durability re-gate: 'could not read the ledger' collapsed
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
        """Durability re-gate: keying the refusal on the PATH let any
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
        """Durability re-gate: councils that ran before the ledger left
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
        """Migration attack: validating as it goes and
        writing in TWO steps half-imports broken legacy data —
        threshold='bogus' raised an uncaught ValueError AFTER the convene had
        already landed, and duplicate members wrote a convene the reducer then
        dropped. Validate the WHOLE snapshot before ANY append."""
        from helm import council
        # INVERTED: this used to ACCEPT a fresh council over a
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
        """Invalid signal entries were silently skipped while
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
        """Migration attack: every terminal status was imported as an
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
        """Migration attack: later events were checked against the
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
        """The sharpest migration finding: `old.room` was
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
        """The checks were `if x is not None and x != expected`
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
                    # envelope + terminal union
                    {"v": 2}, {"id": "OTHER"}, {"aborted_by": {}},
                    {"aborted_by": ""}, {"reason": []},
                    # the last fields _emit stamped after validation
                    {"ts": []}, {"ts": ""}, {"convener": {}}):
            ev = dict(good, **bad)
            self.assertEqual(council._valid_import(ev, room="r")[0], False, bad)
            self.assertIsNone(council._apply(None, ev, room="r"), bad)

    def test_a_planted_import_with_falsy_evidence_or_no_ts_is_dropped(self):
        """The reducer still did `str(s.evidence or "")` — the
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
        """_normalize_legacy never checked old.v, so a missing
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
        """Per-signal `ts` and the council's `created` were
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
        """The re-gate probes, now answered structurally rather than by
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
        """Re-gate: a first-signal-selected target lets the fastest
        member choose the question everyone else is judging."""
        from helm import council
        council.convene("crB", ["seat-a", "seat-b"], 1, threshold=2,
                        tip="aaaaaaaa")
        self.assertEqual(council.registry("crB")["tip"], "aaaaaaaa")
        _r, err = council.signal("crB", "seat-a", "YES", "bbbbbbbb", "e")
        self.assertIn("bound to tip", err)

    def test_the_binding_digest_covers_council_epoch_and_member(self):
        """Re-gate: the digest covered only tip+verdict+evidence, so
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
    """HIGH: roster_report auto-ran the legacy reap_roster, which
    deleted ANY stale row on presence alone — an inactive-but-fully-persisted
    seat lost its row to a 3s web poll, bypassing gc's transcript/process
    evidence and the manual dry-run gate. The legacy path is DELETED, not
    fenced: a report is a READ, cleanup has exactly one owner (gc_roster)."""

    def test_legacy_auto_reap_is_gone(self):
        self.assertFalse(hasattr(seats, "reap_roster"))

    def test_report_keeps_a_stale_seat_row_identical(self):
        """The exact probe: persisted seat present before
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
        chat.post("owner words", who="daria")   # owner rail, not a roster seat
        rooms = {r["room"]: r for r in web._rooms_summary()}
        names = [s["seat"] for s in rooms.get("main", {}).get("seats", [])]
        self.assertNotIn("daria", names)

    def test_rooms_summary_fail_open_when_roster_breaks(self):
        """THE READER HAS TWO FAILURE SHAPES AND THIS ARM DRIVES BOTH.

        An earlier pass replaced the raising fixture with an empty dict and
        this docstring argued the raise was FICTION, because pk.read_json
        catches everything. That reasoning names the wrong symbol.
        seats.roster is `pk.read_json(roster_path(), {}) or {}`, and
        roster_path() resolves the chat dir BEFORE that swallowing read — so
        under a relative HELM_HOME with the process cwd removed it raises
        FileNotFoundError and never reaches pk.read_json at all. The raise
        was reachable, and deleting its fixture deleted coverage: with only
        the empty case, the exception guard under test can be removed and
        this arm stays green.

        So both are asserted below. An empty dict is what a missing or
        corrupt roster delivers THROUGH the swallowing read; a raise is what
        the path resolution does BEFORE it.

        AND THE ARM WAS VACUOUS A SECOND WAY, which only the control below
        exposed: alice was never JOINED, so no roster row existed and the
        summary reported no seats whether the reader worked or not. The
        assertion was a tautology dressed as a degradation test. Every
        sibling arm in this class joins first; this one did not."""
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("x", who="alice")
        # CONTROL on the same observable: with the roster readable this call
        # DOES report seats, so the empty list below is the breakage speaking
        # and not a summary that never populates.
        healthy = {r["room"]: r for r in web._rooms_summary()}
        self.assertTrue(healthy["main"]["seats"], "control: seats expected")
        # BOTH FAILURE SHAPES. An empty dict is what a missing or CORRUPT
        # roster yields, since pk.read_json swallows everything; roster() can
        # ALSO raise, because roster_path() resolves the chat dir before that
        # swallowing read. An earlier pass replaced the raise fixture with the
        # empty one believing a raise impossible, which deleted coverage: with
        # only the empty case, the exception guard here can be removed and the
        # arm stays green.
        with mock.patch.object(seats, "roster", return_value={}):
            rooms = {r["room"]: r for r in web._rooms_summary()}
        self.assertEqual(rooms["main"]["seats"], [])   # degraded, never fatal
        with mock.patch.object(seats, "roster", side_effect=OSError("boom")):
            raised = {r["room"]: r for r in web._rooms_summary()}
        self.assertEqual(raised["main"]["seats"], [])  # and a RAISE degrades too


class ChatDispatchTest(SeatsBase):
    def test_chat_verbs_reach_seats(self):
        out = io.StringIO()
        # A LEASE IS ACTOR-ATTRIBUTED, so `--seat alice` is an ASSERTION that
        # this process IS alice, not a selector naming her. The fixture
        # declares the identity the assertion asserts; without it the process
        # is DERIVED and the claim is refused (tests/_tmphome.declaring).
        with _tmp_declaring(["--seat", "alice"]), contextlib.redirect_stdout(out):
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
        r = {"text": "a reply", "from": "daria"}
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
        # BOTH reviewers' MED (an xrev of c2f4856): the test
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
        """HIGH (finding 1): the old hand-rolled root list omitted
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
        """HIGH (finding 3): victims were computed before the lock
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
        """HIGH (exact-SHA probe): the row delete committed under the
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
        """HIGH (finding 3): a same-uid process whose cmdline/environ
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
        """HIGH (finding 3): a row with NO remembered session had no
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




class AMovedFunctionOwesTheFacadeFanOutTest(SeatsBase):
    """The facade's patch fan-out is what lets a caller patch `seats.X` and
    have the implementation modules see it. A function that MOVES to a module
    the fan-out does not list keeps resolving the OLD binding, so a fixture
    patching `seats.seat_scope` sets a name the moved function no longer reads
    -- and its home-room pin is silently ignored while every other assertion
    in the arm still passes."""

    def test_the_room_scan_sees_a_facade_patch(self):
        from unittest import mock as _mock
        seats.join(session="s-fan", seat="fan1", cwd="/tmp/p")
        # THE PIN MUST EXIST: `_scan_rooms` only appends a pinned room whose
        # file is on disk, so a patched home naming nothing would be dropped
        # for a reason that has nothing to do with the fan-out.
        chat.post("seeding the pinned room", who="daria", room="a-pinned-room")
        with _mock.patch.object(seats, "seat_scope",
                                return_value={"home": "a-pinned-room",
                                              "tracked": True, "rooms": ()}):
            rooms = seats._scan_rooms("main", seat="fan1", session="s-fan",
                                      scan_lane="stop")
        self.assertIn("a-pinned-room", rooms,
                      "the moved function resolved its own module's binding, "
                      "so the patched home pin never reached it")

    def test_the_owner_module_is_registered(self):
        """THE REGISTRY ITSELF, asserted directly: a fan-out that silently
        omits a module is exactly as green as one that includes it."""
        names = [m.__name__.rsplit(".", 1)[-1] for m in seats._impl_modules()]
        self.assertIn("seats_roomscan", names)


class TheSampleSaysWhatItLookedAtNotOnlyWhatItFoundTest(SeatsBase):
    """task/2463 finding 1, driven through the SHIPPED producer.

    `_pending_all` returns HITS. A room successfully scanned and found empty
    contributes nothing to that list, so any caller reconstructing "what did I
    look at" from the result loses it -- and `beacons.undrained` did exactly
    that. The consequence is the whole finding: CONSUMING the sole pending row
    in the owed room makes that room disappear from `scanned`, the drain
    question reads that as "this pass never looked there", and the standing
    alarm can never be cleared by the very pass that proves it drained.

    The old arms for this could not have caught it, because they supplied
    `scanned=('helm',), seen=()` to a double -- a state the real producer could
    not emit. This one asks the producer.
    """

    def test_a_room_read_and_found_empty_is_still_a_room_this_pass_read(self):
        seats.join(session="s-cov", seat="cov1", cwd="/tmp/p")
        chat.post("@cov1 the only row", who="daria")

        before = {}
        hits = seats._pending_all("main", "cov1", "s-cov", scan_lane="stop",
                                  coverage=before)
        self.assertGreaterEqual(len(hits or []), 1,
                                "fixture: the addressed row must be pending")
        self.assertEqual((before.get("rooms") or {}).get("main"), ("read", None))

        # CONSUME IT through the shipped verb, so the emptiness below is the
        # real one and not a fixture that never had a row.
        self.assertIn("the only row", seats.deliver(session="s-cov",
                                                    seat="cov1") or "")

        after = {}
        hits = seats._pending_all("main", "cov1", "s-cov", scan_lane="stop",
                                  coverage=after)
        self.assertEqual(list(hits or []), [],
                         "fixture: the row must be consumed")
        # THE ASSERTION THE FINDING IS ABOUT. The room is empty AND it was
        # read, and those two facts now travel together. Before the cure the
        # second one was unrecoverable from anything this call returned.
        self.assertEqual((after.get("rooms") or {}).get("main"), ("read", None))

    def test_the_hits_alone_cannot_answer_which_rooms_were_read(self):
        """THE CONTROL THAT MAKES THE ARM ABOVE MEAN SOMETHING: derive the old
        `scanned` the old way, from the hits, and watch it lose the room. If
        this ever agrees with coverage, the arm above is asserting a property
        that would hold without the cure."""
        seats.join(session="s-cov2", seat="cov2", cwd="/tmp/p")
        chat.post("@cov2 the only row", who="daria")
        seats.deliver(session="s-cov2", seat="cov2")
        cov = {}
        hits = seats._pending_all("main", "cov2", "s-cov2", scan_lane="stop",
                                  coverage=cov)
        from_hits = tuple(dict.fromkeys(r for r, _row in hits or ()))
        self.assertEqual(from_hits, ())
        self.assertIn("main", cov.get("rooms") or {})


class APreparedKeystrokeIsValidatedByItsInputsTest(SeatsBase):
    """THE AUTHORIZATION OF A DEAF-IN-EFFECT KEYSTROKE, driven through the
    shipped producers: `seats.join` for the binding, `chat.post` for the owed
    row, `seats.deliver` for a real cursor commit, a keyed append whose receipt
    fails for a real rollback, and a proxywatch state file for a real pause.

    `resumeturn._prepare_due` may block; its grant's `still()` may not, and it
    must refuse on exactly the changes that make the preparation stale -- a
    cursor that moved, bytes that changed or vanished without one, a pause --
    while authorizing when none of them happened."""

    SEAT, SID = "val1", "s-val"

    def setUp(self):
        super().setUp()
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")

    def prepare(self, seat=None, session=None):
        from helm import resumeturn
        return resumeturn._prepare_due(seat or self.SEAT, session or self.SID,
                                       room="main", door="enter")

    def consume(self, seat=None, session=None):
        return seats.deliver(session=session or self.SID, seat=seat or self.SEAT)

    def test_a_stable_world_authorizes_and_validates(self):
        chat.post("@val1 an owed row", who="daria")
        grant = self.prepare()
        self.assertTrue(grant.ok, grant.why)
        self.assertEqual(grant.still(), (True, ""))

    def test_a_consumption_after_the_preparation_is_a_moved_cursor(self):
        chat.post("@val1 an owed row", who="daria")
        grant = self.prepare()
        self.assertTrue(grant.ok, "fixture: %s" % grant.why)
        self.assertIn("an owed row", self.consume() or "",
                      "fixture: the shipped deliver did not consume the row")
        valid, why = grant.still()
        self.assertIs(valid, False, "a consumed row still validated")
        self.assertIn("cursor", why)

    def _observe_with_a_commit_inside(self, commit):
        """Prepare while `commit` lands INSIDE the owed room's observation --
        after the room read and before the wake cursor read -- and return
        (grant, observations taken, what the commit returned)."""
        from helm import resumeturn, seats_stop_fp
        real, real_once = seats_stop_fp._tail, resumeturn._prepare_once
        calls, observations, landed = [], [], []

        def commit_inside(room, cur, report=None):
            got = real(room, cur, report=report)
            calls.append(room)
            if room == "main" and calls.count("main") == 1:
                landed.append(commit())
            return got

        def observed(*a, **k):
            observations.append(a)
            return real_once(*a, **k)

        with mock.patch.object(seats_stop_fp, "_tail",
                               side_effect=commit_inside), \
                mock.patch.object(resumeturn, "_prepare_once",
                                  side_effect=observed):
            grant = self.prepare()
        self.assertIn("main", calls, "fixture: the owed room was never read")
        return grant, len(observations), landed

    def test_a_cursor_commit_during_the_preparation_discards_it(self):
        """ANY commit in the room moves its seqlock, so the observation it
        landed inside is not one coherent snapshot and is taken again: the
        grant comes from an observation no commit interrupted."""
        seats.join(session="s-other", seat="val2", cwd="/tmp/p")
        chat.post("@val1 an owed row", who="daria")
        chat.post("@val2 a row for somebody else", who="daria")
        grant, observations, landed = self._observe_with_a_commit_inside(
            lambda: self.consume("val2", "s-other"))
        self.assertIn("somebody else", "".join(map(str, landed)),
                      "fixture: the mid-observation commit consumed nothing")
        self.assertGreaterEqual(observations, 2,
                                "a preparation kept an observation a cursor "
                                "commit landed inside")
        self.assertTrue(grant.ok, grant.why)
        self.assertEqual(grant.still(), (True, ""))

    def test_this_seats_own_consumption_during_the_preparation_is_no_licence(self):
        chat.post("@val1 an owed row", who="daria")
        grant, _observations, landed = self._observe_with_a_commit_inside(
            self.consume)
        self.assertIn("an owed row", "".join(map(str, landed)),
                      "fixture: the mid-observation deliver consumed nothing")
        self.assertFalse(grant.ok, "a preparation authorized on a row the "
                                   "seat consumed inside its observation")
        self.assertEqual(grant.kind, "drained", grant.why)

    def test_owed_bytes_that_vanish_without_a_cursor_advance_refuse(self):
        grants = []
        real = chat._write_event_receipts

        def rollback_after_a_reader_saw_it(path, receipts):
            with open(chat.room_path("main"), encoding="utf-8") as f:
                appended = "@val1 a keyed row" in f.read()
            if appended and not grants:
                grants.append(self.prepare())
                raise OSError("receipt ledger unavailable")
            return real(path, receipts)

        with mock.patch.object(chat, "_write_event_receipts",
                               side_effect=rollback_after_a_reader_saw_it):
            with self.assertRaises(OSError):
                chat.post("@val1 a keyed row", who="daria", event_id="evt-1")
        self.assertEqual(len(grants), 1, "fixture: no reader saw the row")
        grant = grants[0]
        self.assertTrue(grant.ok, "fixture: %s" % grant.why)
        valid, why = grant.still()
        self.assertIs(valid, False, "a rolled-back row still validated")
        # A LATER APPEND REFILLS THE SIZE -- past the old end -- with
        # different bytes, and the cursor never moved throughout.
        chat.post("@val1 a keyed row, refilled by a longer and different body",
                  who="daria")
        valid, why = grant.still()
        self.assertIs(valid, False,
                      "a refilled byte range validated as the owed row")
        self.assertIn("bytes", why)

    def test_a_pause_after_the_preparation_refuses_and_a_pause_before_it_is_paused(self):
        seat, sid = "codex-41", "s-pause"
        seats.join(session=sid, seat=seat, cwd="/tmp/p")
        chat.post("@codex-41 an owed row", who="daria")
        os.makedirs(os.path.dirname(proxywatch._state_path()), exist_ok=True)
        pk.write_json(proxywatch._state_path(), {"ts": time.time(), "upstream": {
            "codex": {"state": "HEALTHY", "dark": False}}})
        grant = self.prepare(seat, sid)
        self.assertTrue(grant.ok, "fixture: %s" % grant.why)
        os.makedirs(os.path.dirname(proxywatch._state_path()), exist_ok=True)
        pk.write_json(proxywatch._state_path(), {"ts": time.time(), "upstream": {
            "codex": {"state": "RATE-LIMITED", "dark": True}}})
        valid, why = grant.still()
        self.assertIs(valid, False, "a pause that landed after the "
                                    "preparation validated")
        self.assertIn("paused", why)
        again = self.prepare(seat, sid)
        self.assertFalse(again.ok)
        self.assertEqual(again.kind, "paused", again.why)

    def test_an_unreadable_census_withholds_and_is_never_a_drain(self):
        from helm import seats_stop_fp
        chat.post("@val1 an owed row", who="daria")
        with mock.patch.object(seats_stop_fp, "_pending_all",
                               side_effect=OSError("tmpfs mid-remount")):
            grant = self.prepare()
        self.assertFalse(grant.ok, "an unreadable census licensed a keystroke")
        self.assertEqual(grant.kind, "unknown", grant.why)
        self.assertNotIn("drained", grant.why.lower(),
                         "UNKNOWN was spelled as a drain")
        # THE CONTROLS, same fixture: readable and owed authorizes; consumed
        # is a drain.
        self.assertTrue(self.prepare().ok)
        self.consume()
        drained = self.prepare()
        self.assertFalse(drained.ok)
        self.assertEqual(drained.kind, "drained", drained.why)

    def test_the_act_door_restarts_on_a_moved_cursor_and_refuses_the_enter(self):
        """End to end through `submit`: the Enter proof is taken, the row is
        consumed before the validation, and the door prepares again -- which
        now finds nothing owed -- so no Enter is pressed."""
        from helm import harness, resumeturn

        class Pane(harness._CLIAdapter):
            name = "fake"

            def __init__(self):
                self.sent, self.composer, self.after_hold = [], "", None

            def read(self, handle, limit=3000, timeout=60):
                frame = "\n".join(("─" * 40, "❯\xa0" + self.composer,
                                   "─" * 40, "  opus-5 | ~/dev/repo"))
                if self.composer and self.after_hold is not None:
                    hook, self.after_hold = self.after_hold, None
                    hook()
                return frame

            def send(self, handle, text, enter=True):
                self.sent.append((text, enter))
                self.composer = "" if enter else self.composer + text

        chat.post("@val1 an owed row", who="daria")
        admit = lambda door: resumeturn._prepare_due(  # noqa: E731
            self.SEAT, self.SID, room="main", door=door)
        pane = Pane()
        pane.after_hold = self.consume
        state, detail = pane.submit("h1", "GO", settle=0, admit=admit)
        self.assertEqual([s for s in pane.sent if s[1]], [],
                         "Enter was pressed on a preparation a consumption "
                         "had made stale")
        self.assertEqual(state, harness.NOT_DELIVERED, detail)
        self.assertEqual(getattr(detail, "kind", None), "drained", detail)
        # THE CONTROL, unconditional: the same door on a stable world places
        # and submits.
        chat.post("@val1 another owed row", who="daria")
        pane = Pane()
        state, detail = pane.submit("h1", "GO", settle=0, admit=admit)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(len([s for s in pane.sent if s[1]]), 1)


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
        chat.post("@ds4pro a real ask", who="daria")
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
            f.write(json.dumps({"ts": int(time.time()), "token": "pytest",
                                "exit": 1, "digest": "aaa"}) + "\n")

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
        self.assertIn("non-distraction protocol", err)
        self.assertIn("3 leases held, 0 subagents spawned this session", err)
        # THE CONCRETE VERBS, not an abstraction — and both of them, because
        # "delegate more" is advice a seat cannot act on at a Stop boundary.
        self.assertIn("spawn a subagent (Agent tool)", err)
        self.assertIn("helm dispatch send", err)
        # the WHY rides the block (a stopbook teaches or it nags)
        self.assertIn("critical path", err)
        self.assertIn("HELM_STOP_GUARD_NDP=0", err)
        # LATCHED: the same load-state on the next stop passes. The absence
        # is read against a stop the guard provably spoke on — the lease
        # rung's latched warn and/or the beacon-arm line still print.
        rc2, _o2, err2 = self.guard({"session_id": self.SID})
        self.assertEqual(rc2, 0, err2)
        self.assertIn("[helm stop-guard]", err2)
        self.assertNotIn("NON-DISTRACTION", err2)

    def test_the_block_names_the_door_for_a_seat_told_not_to_delegate(self):
        # The FIX on this lane, and the shape was CONSTRUCTED rather than
        # argued: fresh session, three session-bound serial leases, no
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
        self.assertIn("non-distraction protocol", err)
        self.assertIn("standing instruction not to delegate", err)
        self.assertIn("HELM_STOP_GUARD_NDP=0", err)
        # AND THE ORDER OF AUTHORITY IS STATED, not implied: a throughput rung
        # must never read as licence to disobey the human who set the
        # constraint. This is the sentence a seat quotes back at its operator.
        self.assertIn("wrong about you", err)

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

    def test_the_POINT_reader_still_finds_a_flat_tombstone(self):
        """The scanner arm below is not enough: _agent_stopped reads ONE path.

        The scan-based rung and the point reader fail differently. A point
        reader that resolved only the family subdir would answer NOT-STOPPED
        for every agent whose tombstone an un-relaunched writer left flat —
        and nothing in a directory-scan arm can see that, because the scan
        finds the flat file while the point read never looks at it."""
        self.plant_red()
        # CREATE THE ROOTS: chat_dir()/state/deleg is minted by _ensure_dir,
        # and without it the flat write below raises FileNotFoundError. The
        # sibling arm got this and this one did not — same fix, one site.
        chat._ensure_dir()
        agent = "sub-flat-point"
        s_hash = seats.hashlib.sha256(self.SID.encode("utf-8")).hexdigest()
        a_hash = seats.hashlib.sha256(agent.encode("utf-8")).hexdigest()
        flat = os.path.join(chat.chat_dir(),
                            ".deleg_stopped.%s.%s" % (s_hash, a_hash))
        with open(flat, "w", encoding="utf-8") as fh:
            json.dump({"session": self.SID, "agent": agent}, fh)
        self.assertTrue(seats._agent_stopped(self.SID, agent))
        # the NEW root still answers, so the read is new-then-legacy and not
        # legacy-only — a reader that had simply been reverted would pass the
        # assertion above and fail this one
        seats._mark_agent_stopped(self.SID, "sub-new-point")
        self.assertTrue(seats._agent_stopped(self.SID, "sub-new-point"))
        # MUST-MISS: an agent with no tombstone in EITHER root
        self.assertFalse(seats._agent_stopped(self.SID, "sub-never"))

    def test_one_unreadable_root_is_UNKNOWN_not_undelegated(self):
        """ANY unreadable root, not only all of them.

        A finding in a root we DID read short-circuits and is definite.
        Reaching the end means we found nothing — and nothing from a PARTIAL
        read cannot be told from nothing from a whole one, because the root
        that would not open is exactly where the tombstone might be."""
        self.plant_red()
        # CREATE THE ROOTS. The family subdir is minted by _ensure_dir, and
        # without it listdir raises FileNotFoundError naturally — which this
        # arm would mistake for its own injected failure, so every assertion
        # below would pass for the wrong reason and the control would be red.
        chat._ensure_dir()
        fam, flat = chat.state_scan_dirs("deleg")
        self.assertTrue(os.path.isdir(fam), "family root must exist")
        self.assertTrue(os.path.isdir(flat), "legacy root must exist")
        real = seats.os.listdir

        def one_root_fails(target):
            def _ls(pth, *a, **k):
                if os.path.realpath(str(pth)) == os.path.realpath(target):
                    raise OSError("unreadable")
                return real(pth, *a, **k)
            return _ls

        # UNCONDITIONAL control FIRST, on the same observable: with both
        # roots readable and nothing planted the reader says NOT delegated.
        # Without this the Trues below could come from a function that always
        # says True, and they sit inside a loop the rung cannot prove ran.
        self.assertFalse(seats._session_delegated(self.SID + "-clean"))
        # UNROLLED on purpose: a loop hides whether either direction ran.
        with mock.patch.object(seats.os, "listdir",
                               side_effect=one_root_fails(flat)):
            self.assertTrue(seats._session_delegated(self.SID), "legacy root")
        with mock.patch.object(seats.os, "listdir",
                               side_effect=one_root_fails(fam)):
            self.assertTrue(seats._session_delegated(self.SID), "family root")

    def test_an_unreadable_root_stays_quiet_instead_of_calling_it_undelegated(self):
        """Splitting one listdir into two must not lose the fail-closed leg.

        The single-root reader returned True on OSError — trouble yields
        nothing, never a louder lane. Scanning two roots with `continue` on
        error would fall through both and answer NEVER DELEGATED, which is
        the LOUD direction on a seat we cannot see. Unreadable is UNKNOWN."""
        self.plant_red()
        # roots are minted by _ensure_dir; without them a flat write
        # raises FileNotFoundError and the arm passes or dies for a
        # reason that has nothing to do with what it tests.
        chat._ensure_dir()
        with mock.patch.object(seats.os, "listdir",
                               side_effect=OSError("unreadable")):
            self.assertTrue(seats._session_delegated(self.SID))
        # MUST-MISS: with the roots readable and nothing planted, the same
        # session reads as NOT delegated — so the True above came from the
        # error leg, not from a function that always returns True.
        self.assertFalse(seats._session_delegated(self.SID + "-clean"))

    def test_a_flat_tombstone_from_pre_namespace_code_still_silences_the_gate(self):
        """The mid-boot case the arm above names, now that markers moved.

        Markers write to chat_dir()/state/deleg/ but a LIVE tmpfs holds
        thousands of FLAT tombstones, and a beacon runs the code it armed
        with — so un-relaunched seats keep writing flat for the whole compat
        window. A reader that saw only the new subdir would answer "never
        delegated" for every one of them and re-fire this gate on seats that
        had already delegated: the exact failure the wire-name arm above
        exists to prevent, reached through the DIRECTORY instead of the
        constant."""
        self.plant_red()
        # roots are minted by _ensure_dir; without them a flat write
        # raises FileNotFoundError and the arm passes or dies for a
        # reason that has nothing to do with what it tests.
        chat._ensure_dir()
        # the producer now writes into the family subdir
        newp = seats._delegation_stop_path(self.SID, "sub-new")
        self.assertTrue(seats._mark_agent_stopped(self.SID, "sub-new"))
        self.assertIn(os.path.join("state", "deleg"), newp)
        self.assertTrue(os.path.exists(newp))
        self.assertTrue(seats._session_delegated(self.SID))

        # a DIFFERENT session whose tombstone exists only in the FLAT dir,
        # exactly as an un-relaunched writer leaves it
        other = self.SID + "-legacy"
        s_hash = seats.hashlib.sha256(other.encode("utf-8")).hexdigest()
        a_hash = seats.hashlib.sha256(b"sub-old").hexdigest()
        flat = os.path.join(chat.chat_dir(),
                            ".deleg_stopped.%s.%s" % (s_hash, a_hash))
        with open(flat, "w", encoding="utf-8") as fh:
            json.dump({"session": other, "agent": "sub-old"}, fh)
        self.assertTrue(seats._session_delegated(other))

        # MUST-MISS: the dual scan must not turn every session into a
        # delegator — a session with no tombstone in EITHER root stays False,
        # or the arm above would pass on a reader that always returns True.
        self.assertFalse(seats._session_delegated(self.SID + "-none"))

    def test_the_gate_latches_per_state_and_a_doubled_queue_rearms(self):
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        self.hold(3)
        b1, w1 = self.gate()
        self.assertIsNotNone(b1, "3 held leases + 0 subagents must gate")
        self.assertIsNone(w1)
        self.assertTrue(
            b1.startswith("[helm stop-guard] non-distraction protocol"), b1)
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


class CanonicalKeyResolverTest(SeatsBase):
    """ONE ROSTER KEY PER IDENTITY, ON THE WAY OUT AS WELL AS IN.

    `write_roster` already resolves the unique casefold-equivalent key before
    it loads a row and REFUSES a roster holding two; `recipient_matches` is
    that relation for addressing; `beacons` builds its own names map to read
    through. Everything else indexed the mapping raw, so a case-variant
    spelling missed a row that exists and the miss read as an absent seat.
    """

    LIVE = "L" * 32

    def _two_variants(self):
        """A roster a human can produce and production refuses to guess at."""
        path = seats.roster_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps({"kimi": {"session": self.LIVE},
                                          "Kimi": {"home_room": "other"}}))

    def test_one_row_answers_under_every_spelling(self):
        seats.write_roster("kimi", session=self.LIVE, cwd=self.tmp)
        # UNCONDITIONAL FIRST, because every assertion below sits inside a
        # loop and an empty one passes: the exact spelling proves the calls
        # reach a row at all before any variant is asked about.
        self.assertEqual(seats.canonical_seat("kimi"), ("kimi", None))
        self.assertEqual((seats.seat_row("kimi")[0] or {}).get("session"),
                         self.LIVE)
        for spelling in ("Kimi", "KIMI"):
            with self.subTest(spelling=spelling):
                self.assertEqual(seats.canonical_seat(spelling), ("kimi", None))
                row, err = seats.seat_row(spelling)
                self.assertIsNone(err)
                self.assertEqual((row or {}).get("session"), self.LIVE)

    def test_seat_where_STOPS_calling_an_ambiguous_seat_genuinely_unknown(self):  # noqa: VACUOUS_ASSERTION — MEASURED, and the cause is UNKNOWN. The unconditional positive control IS present on the same root: `assertEqual(orcaadopt.resolve("solo")["seat"], "solo")` runs before the assertIsNone and fails if the census mock has broken resolve outright. The rung nevertheless reports absent-control here — stripping only this comment, line count held, moves the scan from 190 findings to 191 and the extra one names this arm. Why it cannot see the control is NOT established; this suppression asserts the measured fact and invents no mechanism
        """THE ONE CALLER WHOSE BEHAVIOUR THIS CURE ACTUALLY CHANGES.

        `resolve` answers None only for a name helm has never heard of, and
        both its callers print that as a typo — its own comment block says
        ANSWERING None IS A POSITIVE CLAIM. An ambiguous roster reached that
        exit with roster_failed False, sids empty and no named procs, so the
        operator-facing `seat where` reported a seat holding TWO ROWS as
        unknown: a false ABSENCE told to the operator about the exact state
        they have to repair.

        The other two callers gate on `not failed and sids`, where the empty
        sids term already dominates, so they are indifferent. Without this arm
        the only behaviour this lane changes is the only one it does not
        guard."""
        from helm import orcaadopt
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], None)):
            # UNCONDITIONAL POSITIVE FIRST, on the same observable: a seat
            # with one row and a session resolves to a dict. Without it
            # the None below could come from a resolve() this mock has
            # broken outright rather than from a genuine unknown.
            seats.write_roster("solo", session=self.LIVE, cwd=self.tmp)
            self.assertEqual(orcaadopt.resolve("solo")["seat"], "solo")
            # THEN the absence control: a name nobody holds must STILL
            # answer None, or the finding is satisfied by a resolve()
            # that never returns None at all.
            self.assertIsNone(orcaadopt.resolve("stranger"))
            self._two_variants()
            # ASSERT THE ANSWER, NOT THE ABSENCE OF None: a dict with
            # this seat and a state is a positive observable, so the
            # arm cannot pass on a resolve() that returns any truthy
            # thing, and the control above still requires a genuine
            # unknown to answer None.
            info = orcaadopt.resolve("kimi")
            self.assertIsInstance(
                info, dict,
                "an ambiguous roster is a repair state, not an unknown seat")
            self.assertEqual(info.get("seat"), "kimi")
            self.assertTrue(info.get("state"))

    def test_an_AMBIGUOUS_roster_is_a_FAILED_probe_not_an_ABSENT_seat(self):
        """A REFUSAL AND A NEGATIVE ARE OPPOSITE POLARITIES AND THE CALLER
        CANNOT TELL THEM APART FROM THE ANSWER ALONE.

        `roster_identity` promises a failed probe reaches its guard as
        UNKNOWN, because "this seat holds no sessions" clears a resume on no
        evidence. Discarding the resolver's ambiguity refusal converts the
        refusal into that negative."""
        from helm import orcaadopt
        seats.write_roster("solo", session=self.LIVE, cwd=self.tmp)
        # UNCONDITIONAL CONTROL: an unambiguous roster resolves and does NOT
        # report failure, so a function that answered failed=True to
        # everything fails here rather than passing the arm below.
        current, sids, failed = orcaadopt.roster_identity("solo")
        self.assertFalse(failed)
        self.assertEqual(current, self.LIVE)
        self._two_variants()
        current, sids, failed = orcaadopt.roster_identity("kimi")
        self.assertTrue(failed, "an ambiguous roster is UNKNOWN, not absent")
        self.assertIsNone(current)
        self.assertEqual(sids, [])

    def test_a_name_nobody_holds_is_ABSENT_and_not_an_error(self):  # noqa: VACUOUS_ASSERTION — the arm above is the unconditional positive control on the same two calls, so a resolver that answered None to everything fails there
        """The control that stops the resolver from being a door that answers
        one word. Absence is (None, None): no row names this seat."""
        seats.write_roster("kimi", session=self.LIVE, cwd=self.tmp)
        self.assertEqual(seats.canonical_seat("stranger"), (None, None))
        self.assertEqual(seats.seat_row("stranger"), (None, None))

    def test_an_AMBIGUOUS_roster_refuses_rather_than_guessing(self):
        """Two case-variant rows is a REPAIRABLE state, not an absent seat,
        and the two must not share a representation: `write_roster` raises on
        it, and a reader on a never-raise path says so in the second slot."""
        self._two_variants()
        key, err = seats.canonical_seat("KIMI")
        self.assertIsNone(key)
        self.assertIn("case-variant", err or "")
        self.assertIn("kimi", err or "")
        self.assertIn("Kimi", err or "")
        row, rerr = seats.seat_row("KIMI")
        self.assertIsNone(row)
        self.assertEqual(rerr, err)

    def test_the_CLAIM_JUMP_guard_sees_a_case_variant_name(self):
        """THE P1 THIS LANE EXISTS FOR, and the two branches of one function
        disagreed. `identity_disagreement` compares bound against declared
        with casefold, so TAKEOVER was safe; four lines down the CLAIM-JUMP
        branch asked the roster for `own` by raw key, missed the live row, and
        returned NO DISPUTE. That branch exists because the predicate before
        it was measured open by an adversary: inherited name plus a FRESH sid
        produced no dispute and every rung opened. A case-variant name is the
        same hole one axis over."""
        from helm import session as helm_session
        # LIVE MEANS A PROCESS HOLDS IT: the claim-jump asks the claude census,
        # so the arm plants the holder instead of reading the host's table.
        held = mock.patch.object(helm_session, "_proc_claude_census",
                                 return_value={
                                     "rows": [{"pid": 4242,
                                               "session": self.LIVE}],
                                     "listing_failed": False,
                                     "who_failed": False,
                                     "census_partial": False})
        held.start()
        self.addCleanup(held.stop)
        seats.write_roster("kimi", session=self.LIVE, cwd=self.tmp)
        mine = "M" * 32                      # a fresh sid, rostered nowhere
        self.addCleanup(os.environ.pop, "HELM_CHAT_NAME", None)
        # UNCONDITIONAL FIRST. The exact spelling disputed before this lane
        # too, so it proves the fixture actually reaches the guard; an empty
        # loop below would otherwise pass in silence.
        os.environ["HELM_CHAT_NAME"] = "kimi"
        self.assertEqual(seats_identity.identity_disagreement(mine),
                         ("kimi", "kimi"))
        for spelling in ("Kimi", "KIMI"):
            with self.subTest(spelling=spelling):
                os.environ["HELM_CHAT_NAME"] = spelling
                self.assertEqual(
                    seats_identity.identity_disagreement(mine),
                    (spelling, spelling),
                    "a declared name already held by a LIVE session is a "
                    "claim-jump however it is spelled")

    def test_the_guard_stays_QUIET_for_a_name_nobody_holds(self):  # noqa: VACUOUS_ASSERTION — the arm above is the unconditional positive control: the same call on the same roster returns a dispute for a held name, so None here discriminates
        """The other pole. A guard cured by making it fire on everything is
        not cured, and every arm above asserts a dispute."""
        seats.write_roster("kimi", session=self.LIVE, cwd=self.tmp)
        os.environ["HELM_CHAT_NAME"] = "stranger"
        self.addCleanup(os.environ.pop, "HELM_CHAT_NAME", None)
        self.assertIsNone(seats_identity.identity_disagreement("M" * 32))

    def test_a_joined_seat_is_TRACKED_under_a_case_variant(self):
        """`seat_scope` feeds delivery, catchup, receipts and the stop
        fingerprint. A missed row answered tracked=False, home=None and an
        empty mute set — a joined seat reading as one that never joined."""
        seats.write_roster("kimi", session=self.LIVE, cwd=self.tmp)
        self.assertTrue(seats_identity.seat_scope("kimi")["tracked"],
                        "control: the exact spelling must read tracked")
        self.assertTrue(seats_identity.seat_scope("Kimi")["tracked"])


class StatusTargetsTheIdentityNotTheSpellingTest(SeatsBase):
    """`set_status` is where this row started: it resolved its roster row by
    EXACT key, so a LIVE seat was unaddressable by a spelling every other
    layer treats as the same identity, and the miss rendered as "no roster
    row" — which is what a seat that never joined looks like."""

    def test_a_case_variant_target_reaches_the_one_row(self):
        seats.write_roster("selfie", session="q" * 32, cwd=self.tmp)
        ok, msg = seats.set_status("selfie", "exact")
        self.assertTrue(ok, msg)          # control: the plain spelling works
        ok, msg = seats.set_status("Selfie", "variant")
        self.assertTrue(ok, msg)
        rows = seats.roster()
        self.assertEqual(sorted(k for k in rows
                                if k.casefold() == "selfie"), ["selfie"])
        self.assertEqual(rows["selfie"].get("status"), "variant")

    def test_the_SHOW_leg_answers_for_the_ROW_THE_SET_LEG_WRITES(self):
        """ONE VERB, TWO LEGS, AND THEY MUST RESOLVE THE SAME IDENTITY.

        Curing only the write side leaves a verb that ACCEPTS a spelling and
        then cannot show what it just stored under it — a worse surface than
        refusing both, because the refusal now contradicts an action the same
        command took one invocation earlier."""
        seats.write_roster("selfie", session="q" * 32, cwd=self.tmp)
        rc, out, err = self.cmd("status", ["variant-line", "--seat", "Selfie"])
        self.assertEqual(rc, 0, err)      # control: the SET leg accepts it
        rc, out, err = self.cmd("status", ["--seat", "Selfie"])
        self.assertEqual(rc, 0, err)
        self.assertIn("variant-line", out)
        self.assertNotIn("no roster row", err)

    def test_the_SHOW_legs_ambiguity_refusal_exits_2_not_1(self):
        """rc 1 HERE MEANS "no such row" AND THIS IS NOT THAT.

        An ambiguous roster is "repair state, then ask again". A script
        branching on rc 1 would read a two-row repair state as an absent
        seat, rebuilding the refusal-as-negative collapse at the shell
        boundary — the same collapse this lane cures inside the process."""
        seats.write_roster("solo", session="q" * 32, cwd=self.tmp)
        rc, _out, err = self.cmd("status", ["--seat", "solo"])
        self.assertEqual(rc, 0, err)      # control: one row shows cleanly
        path = seats.roster_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps({"selfie": {"session": "q" * 32},
                                          "Selfie": {"home": "/h"}}))
        rc, _out, err = self.cmd("status", ["--seat", "SELFIE"])
        self.assertEqual(rc, 2, err)
        self.assertIn("case-variant", err)

    def test_a_writer_spelled_differently_is_not_its_own_ANNOTATOR(self):
        """`by != seat` raw records a seat as the annotator of its OWN row
        whenever the two spellings differ, inventing a cross-seat annotation
        out of capitalisation. status_by exists to say who annotated SOMEBODY
        ELSE."""
        seats.write_roster("selfie", session="q" * 32, cwd=self.tmp)
        ok, msg = seats.set_status("selfie", "mine", by="Selfie")
        self.assertTrue(ok, msg)
        self.assertNotIn("status_by", seats.roster()["selfie"])
        ok, msg = seats.set_status("selfie", "theirs", by="coordinator")
        self.assertTrue(ok, msg)          # control: a real annotator IS kept
        self.assertEqual(seats.roster()["selfie"].get("status_by"),
                         "coordinator")

    def test_an_AMBIGUOUS_roster_refuses_with_the_repair_sentence(self):
        """Two case-variant rows is repairable, not absent, and a write that
        picked one would bind onto whichever it happened to sort to."""
        path = seats.roster_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps({"selfie": {"session": "q" * 32},
                                          "Selfie": {"home": "/h"}}))
        ok, msg = seats.set_status("SELFIE", "either")
        self.assertFalse(ok)
        self.assertIn("case-variant", msg)
        rows = seats.roster()
        self.assertNotIn("status", rows["selfie"])
        self.assertNotIn("status", rows["Selfie"])


class RebindReportsWhatActuallyHappenedTest(SeatsBase):
    """The three r7 findings on the identity-stamp route, each about a
    SENTENCE the code prints rather than about the migration itself.

    All three share a shape: the report is driven by something other than the
    outcome it describes — the flags that reached the block, the raw bytes of
    a stored key, the arm of a try that happened to catch. A reader cannot
    tell any of them from a correct report, which is why they are arms and not
    review notes.
    """

    OLD = "legacy-row"

    def _legacy_roster(self, name=None):
        rows = seats.roster()
        rows[name or self.OLD] = {"session": "s-old", "seen": 0}
        pk.write_json(seats.roster_path(), rows)
        return rows

    def test_an_unresolvable_path_is_UNKNOWN_and_never_proven_absent(self):
        """RESOLVING THE PATH IS NOT READING THE FILE. os.getcwd() raises
        FileNotFoundError when the process's cwd has been removed, and with the
        resolution inside the open's try that landed on the proven-absent arm:
        "nothing to stamp", over a roster full of unmigrated rows."""
        self._legacy_roster()
        with mock.patch.object(seatmod_incarnation, "roster_path",
                               side_effect=FileNotFoundError(2, "No such cwd")):
            names, marked, unread = seatmod_incarnation.preview_incarnations()
        self.assertEqual((names, marked), ([], 0))
        self.assertIn("could not be resolved", unread)
        self.assertIn("UNKNOWN", unread)

    def test_a_roster_that_parses_as_a_NON_MAPPING_is_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the sibling arm below, which drives this same reader with `{}` and pins the ([], 0, "") answer; here every subTest asserts a REFUSAL and there is no non-refusing case to assert outside the loop
        """`rows = json.load(f) or {}` ran BEFORE the shape check and spent
        it. A roster whose content is a valid JSON `[]` -- or null, or false,
        or 0, or "" -- becomes an empty MAPPING, so the isinstance guard sees a
        dict and says nothing, and a file this reader can open and cannot use
        answers "nothing to stamp". That is indistinguishable from a fully
        migrated fleet, which is the reassuring direction this reader's own
        docstring promises to refuse."""
        self._legacy_roster()            # so the roster's directory exists
        for raw, shape in (("[]", "list"), ("null", "NoneType"),
                           ("false", "bool"), ("0", "int"), ('""', "str")):
            with self.subTest(raw=raw):
                with open(seats.roster_path(), "w", encoding="utf-8") as fh:
                    fh.write(raw)
                names, marked, unread = \
                    seatmod_incarnation.preview_incarnations()
                self.assertEqual((names, marked), ([], 0))
                self.assertIn("UNKNOWN", unread)
                self.assertIn(shape, unread,
                              "the refusal must name what it actually parsed "
                              "as, or a reader cannot tell which file is wrong")

    def test_an_EMPTY_MAPPING_is_still_a_legitimate_nothing_to_stamp(self):
        """THE CONTROL, and it is what keeps the arm above from being a rule
        that calls every readable roster unknown. `{}` is a well-formed roster
        with no rows, and a genuinely absent file is proven absence -- both
        still answer ([], 0, "") with no complaint."""
        self._legacy_roster()            # so the roster's directory exists
        with open(seats.roster_path(), "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.assertEqual(seatmod_incarnation.preview_incarnations(),
                         ([], 0, ""))

    def test_a_genuinely_missing_roster_is_still_proven_absent(self):
        """THE CONTROL, and without it the arm above is satisfied by a preview
        that calls everything unknown. Only the OPEN may claim absence, and it
        still does."""
        try:
            os.unlink(seats.roster_path())
        except FileNotFoundError:
            pass
        self.assertEqual(seatmod_incarnation.preview_incarnations(),
                         ([], 0, ""))

    def _rebind_all(self, **migrate):
        """Drive the SHIPPED --all route and return everything it said.

        BOTH STREAMS, because the three sentences under test do not share one:
        the stamp's success line goes to stdout and its SKIPPED/UNKNOWN lines
        go to stderr, so a capture of either alone reads as silence for half
        the cases. And `harness.detect` is pinned to a NON-orca answer because
        the suffix being tested only exists on that route — on a box that has
        orca (this one does) the branch never runs and every assertion about
        it would pass by never executing."""
        from helm import harness as harness_mod
        with mock.patch.object(seatmod_roster, "migrate_incarnations",
                               **migrate), \
                mock.patch.object(seat_lifecycle, "registered_seats",
                                  return_value=([], False)), \
                mock.patch.object(harness_mod, "detect", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            seat_lifecycle._rebind(["--all", "--apply"])
        return out.getvalue() + err.getvalue()

    def test_a_SKIPPED_stamp_does_not_report_that_it_already_ran(self):
        """A completion sentence beside an explicit failure, on the one route
        where the migration is the only thing the operator needed."""
        said = self._rebind_all(side_effect=OSError("roster unreadable"))
        self.assertIn("identity stamp SKIPPED", said)
        self.assertNotIn("already ran", said)

    def test_a_stamp_that_DID_run_still_says_so(self):
        """The control for the arm above, and it is what keeps the cure from
        being "delete the sentence": the suffix follows the OUTCOME, so a
        stamp that completed keeps the line that route exists to print."""
        self.assertIn("already ran", self._rebind_all(return_value=([], 0, "")))

    def test_an_UNKNOWN_stamp_does_not_report_that_it_already_ran(self):
        """The third outcome. An unreadable roster is not a completed stamp
        either, and the flag-gated suffix could not tell them apart."""
        said = self._rebind_all(return_value=([], 0, "the roster was unreadable"))
        self.assertIn("identity stamp UNKNOWN", said)
        self.assertNotIn("already ran", said)

    def test_a_control_character_in_a_legacy_key_cannot_reshape_the_line(self):
        """Migration deliberately supports legacy FILE KEYS, and json.load
        preserves whatever bytes they hold. Joining them straight into a
        terminal line hands the roster's content control of the line it rides
        in; the stored identity is not the displayed copy."""
        said = self._rebind_all(return_value=(["ghost\x1b[2Kreal"], 0, ""))
        self.assertIn("identity stamp", said)
        self.assertNotIn("\x1b", said,
                         "an escape sequence reached the terminal")
        self.assertIn("ghost", said,
                      "the name was dropped rather than scrubbed")
        self.assertIn("real", said)


class AnUnreadableCursorIsNamedAtTheStopTest(SeatsBase):
    """task/2530 (b): an inbox room the stop guard could not read is named.

    `_pending_rows` answers an UNREADABLE consumption cursor with no rows,
    because replaying a cursor helm cannot trust brings consumed rows back as
    owed. That is the right answer for the rows and the wrong one for the
    silence: the stop guard passed no coverage, so a room it could not read
    and a room with nothing in it were the same empty list. The guard's law is
    that an unreadable inbox is never a hold, so the room is surfaced as a
    WARN and blocks nothing.

    The cursor is a real file at the real `cursor_path`, and the must-hit in
    `corrupt_cursor` proves the shipped reader classifies it unreadable."""

    MARK = "could not be fully read"

    def owed(self, session):
        seats.join(session=session, cwd="/tmp/p", seat="alice")
        chat.post("@alice an owed row", who="bob")

    def corrupt_cursor(self, session):
        path = seats.cursor_path("main", "alice", session)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not a cursor\n")
        mark = {}
        self.assertIsNone(seats._cursor("main", "alice", session, report=mark))
        self.assertEqual(mark.get("outcome"), "unreadable", mark)
        return mark["detail"]

    def unread(self, warns):
        return [w for w in warns if self.MARK in w]

    def inbox_blocks(self, blocks):
        return [b for b in blocks if "undelivered message(s)" in b]

    def test_an_unreadable_cursor_is_named_at_the_stop_and_blocks_nothing(self):
        self.owed("s-unr")
        blocks, warns = seats.stop_guard(session="s-unr", room="main",
                                         seat="alice")
        self.assertEqual(len(self.inbox_blocks(blocks)), 1, blocks)
        self.assertEqual(self.unread(warns), [], warns)
        why = self.corrupt_cursor("s-unr")
        blocks, warns = seats.stop_guard(session="s-unr", room="main",
                                         seat="alice")
        hit = self.unread(warns)
        self.assertEqual(len(hit), 1, warns)
        # The room, then the reader's own reason inside the scan's wording.
        self.assertIn("'alice': main (", hit[0])
        self.assertIn(why, hit[0])
        self.assertEqual(self.inbox_blocks(blocks), [], blocks)
        # AND THE STOP IS NOT CALLED CLEAN. The unchanged guard said "inbox
        # clean" over this very room, measured on e665913b4.
        self.assertEqual([w for w in warns if "inbox clean" in w], [], warns)

    def test_an_unreadable_cursor_is_named_while_the_stop_is_continuing(self):
        self.owed("s-cont")
        why = self.corrupt_cursor("s-cont")
        blocks, warns = seats.stop_guard(session="s-cont", room="main",
                                         seat="alice", stop_active=True)
        self.assertEqual(blocks, [])
        hit = self.unread(warns)
        self.assertEqual(len(hit), 1, warns)
        self.assertIn("'alice': main (", hit[0])
        self.assertIn(why, hit[0])

    def test_a_readable_cursor_reports_owed_rows_and_a_drained_one_is_silent(self):
        """The controls, through a cursor the real delivery wrote: drained,
        both stop paths say nothing about it; one more row, the block
        returns and still names no unread room."""
        self.owed("s-read")
        self.assertIn("an owed row", seats.deliver(
            session="s-read", seat="alice", channel="hook",
            emit=lambda _line: None))
        mark = {}
        self.assertIsNotNone(seats._cursor("main", "alice", "s-read",
                                           report=mark))
        self.assertEqual(mark["outcome"], "read", mark)
        for active in (False, True):
            blocks, warns = seats.stop_guard(session="s-read", room="main",
                                             seat="alice", stop_active=active)
            self.assertEqual(self.unread(warns), [], warns)
            self.assertEqual(self.inbox_blocks(blocks), [], blocks)
            self.assertEqual([w for w in warns if "arrived DURING" in w], [])
        chat.post("@alice a second owed row", who="bob")
        blocks, warns = seats.stop_guard(session="s-read", room="main",
                                         seat="alice")
        self.assertEqual(len(self.inbox_blocks(blocks)), 1, blocks)
        self.assertEqual(self.unread(warns), [], warns)

    def test_a_drained_readable_cursor_is_still_called_clean(self):
        """The control for the clean line: a room that read and held nothing
        keeps its "inbox clean" sentence."""
        self.owed("s-clean")
        seats.deliver(session="s-clean", seat="alice", channel="hook",
                      emit=lambda _line: None)
        _blocks, warns = seats.stop_guard(session="s-clean", room="main",
                                          seat="alice")
        self.assertEqual(len([w for w in warns if "inbox clean" in w]), 1,
                         warns)

    def drained(self, session):
        self.owed(session)
        seats.deliver(session=session, seat="alice", channel="hook",
                      emit=lambda _line: None)

    def estate(self, session):
        """How the real scan saw the room estate. Read AFTER the stops, so
        the must-hit cannot perturb the pass it is about."""
        seen = {}
        seats_stop_signals._pending_all("main", "alice", session,
                                        scan_lane="stop", coverage=seen)
        return seen["estate"]

    def test_an_unlistable_room_estate_is_named_at_the_stop_and_not_clean(self):
        """The scan could not LIST the rooms, so it read only the pinned ones
        and said so on stderr, which a Stop hook that exits 0 never shows the
        seat. The stop then called the inbox clean over foreign rooms it never
        visited. It is the unreadable cursor's class one level up, and the
        estate outcome rides the same coverage dict."""
        self.drained("s-est")
        with mock.patch.object(chat, "list_rooms",
                               side_effect=OSError("estate unreadable")):
            runs = [seats.stop_guard(session="s-est", room="main",
                                     seat="alice", stop_active=active)
                    for active in (False, True)]
            outcome, why = self.estate("s-est")
        self.assertEqual(outcome, "unlistable")
        for blocks, warns in runs:
            hit = self.unread(warns)
            self.assertEqual(len(hit), 1, warns)
            self.assertIn(why, hit[0])
            self.assertEqual(self.inbox_blocks(blocks), [], blocks)
            self.assertEqual([w for w in warns if "inbox clean" in w], [],
                             warns)

    def test_a_capped_rotation_is_ordinary_and_still_called_clean(self):
        """The control: a pass that covered a bounded slice of the foreign
        rooms is rotation working, not a failure to read, so it names nothing
        and keeps the clean line."""
        self.drained("s-cap")
        chat.post("a foreign room exists", room="elsewhere", who="bob")
        with mock.patch("helm.seats_roomscan.ROOM_SCAN_CAP", 0):
            _blocks, warns = seats.stop_guard(session="s-cap", room="main",
                                              seat="alice")
            outcome, _why = self.estate("s-cap")
        self.assertEqual(outcome, "capped")
        self.assertEqual(self.unread(warns), [], warns)
        self.assertEqual(len([w for w in warns if "inbox clean" in w]), 1,
                         warns)


class AStaleDispatchClaimSaysSo(unittest.TestCase):
    """The Stop line explains the row without ruling on claim release.

    A stale claim can survive a lifecycle change; the rebind door cannot
    release another holder's lease. The holder needs the row's current state,
    including whether a successor now carries the work.
    """

    SEAT = "hc2-advice-fixture"
    RID = "aabbccdd11223344"
    KID = "eeff001122334455"

    def rows(self, **row):
        base = {"id": self.RID, "chain_root": self.RID, "status": "open",
                "recipient": self.SEAT, "lane": "a-lane"}
        base.update(row)
        return {self.RID: base}

    def successor(self, **over):
        row = {"id": self.KID, "chain_root": self.RID,
               "supersedes": self.RID, "status": "open",
               "recipient": "successor-seat", "lane": "a-lane"}
        row.update(over)
        return row

    def test_an_OPEN_row_addressed_to_this_seat_is_a_live_obligation(self):
        """A parent alone remains owed; claim release would not discharge it."""
        text = seats._dispatch_advice(
            "dispatch:aabbccdd", self.SEAT, snap=self.rows())
        self.assertIn("LIVE obligation", text)
        self.assertIn("helm dispatch triage aabbccdd", text)

    def test_a_successor_carries_the_work_without_discharging_it(self):
        snap = self.rows()
        parent = snap[self.RID]
        self.assertEqual(dispatches.owed(snap), [parent])
        alone = seats._dispatch_advice("dispatch:aabbccdd", self.SEAT, snap=snap)
        self.assertIn("LIVE obligation", alone)
        kid = self.successor()
        snap[self.KID] = kid
        self.assertIs(dispatches.carrier(parent, snap), kid)
        self.assertEqual(dispatches.owed(snap), [kid])
        text = seats._dispatch_advice("dispatch:aabbccdd", self.SEAT, snap=snap)
        self.assertIn("CARRIED by dispatch " + self.KID[:12], text)
        self.assertIn("Supersession is not discharge", text)
        self.assertNotIn("LIVE obligation", text)
        self.assertNotIn("helm dispatch triage aabbccdd", text)
        # HELD is active too, but carrying still takes precedence over PAUSE.
        parent["status"] = "held"
        held = seats._dispatch_advice("dispatch:aabbccdd", self.SEAT, snap=snap)
        self.assertIn("CARRIED by dispatch " + self.KID[:12], held)
        self.assertNotIn("STILL YOURS", held)

    def test_an_UNKNOWN_chain_does_not_hide_the_parent(self):
        snap = self.rows()
        parent = snap[self.RID]
        kid = self.successor(chain_root=dispatches.CHAIN_UNKNOWN)
        snap[self.KID] = kid
        self.assertIsNone(dispatches.carrier(parent, snap))
        self.assertIn(parent, dispatches.owed(snap))
        text = seats._dispatch_advice("dispatch:aabbccdd", self.SEAT, snap=snap)
        self.assertIn("LIVE obligation", text)
        # The identical edge with a proven chain really does carry it.
        kid["chain_root"] = self.RID
        self.assertIs(dispatches.carrier(parent, snap), kid)
        carried = seats._dispatch_advice("dispatch:aabbccdd", self.SEAT, snap=snap)
        self.assertIn("CARRIED by dispatch " + self.KID[:12], carried)

    def test_carrier_status_is_not_reclassified_by_the_advice(self):
        """The owner accepts this same-chain successor, even with an unknown
        status. Report only carrying, never invent LIVE or discharged work."""
        snap = self.rows()
        kid = self.successor(status="unrecognized")
        snap[self.KID] = kid
        self.assertIs(dispatches.carrier(snap[self.RID], snap), kid)
        text = seats._dispatch_advice("dispatch:aabbccdd", self.SEAT, snap=snap)
        self.assertIn("CARRIED by dispatch " + self.KID[:12], text)
        self.assertIn("Supersession is not discharge", text)
        self.assertNotIn("LIVE obligation", text)
        unknown = seats._dispatch_advice(
            "dispatch:aabbccdd", self.SEAT, snap=self.rows(status="unrecognized"))
        self.assertIn("UNKNOWN", unknown)

    def test_a_REBOUND_row_names_the_new_recipient_and_is_stale(self):
        text = seats._dispatch_advice(
            "dispatch:aabbccdd", self.SEAT, snap=self.rows(recipient="another-seat"))
        self.assertIn("@another-seat", text)
        self.assertIn("STALE", text)

    def test_a_DISCHARGED_row_is_stale_whatever_discharged_it(self):
        live = seats._dispatch_advice("dispatch:aabbccdd", self.SEAT,
                                      snap=self.rows())
        self.assertIn("LIVE obligation", live)
        self.assertNotIn("STALE", live)
        stale = seats._dispatch_advice("dispatch:aabbccdd", self.SEAT,
                                       snap=self.rows(status="cancelled"))
        self.assertIn("STALE", stale)
        for status in ("cancelled", "closed", "verdict"):
            text = seats._dispatch_advice(
                "dispatch:aabbccdd", self.SEAT, snap=self.rows(status=status))
            self.assertIn(status.upper(), text)
            self.assertIn("STALE", text)

    def test_an_UNREADABLE_ledger_reports_UNKNOWN(self):
        """Unreadable is not discharged; this sentence does not rule on release."""
        text = seats._dispatch_advice(
            "dispatch:aabbccdd", self.SEAT, snap=None,
            ledger_note="the dispatch ledger raised")
        self.assertIn("UNKNOWN", text)
        self.assertIn("the dispatch ledger raised", text)

    def test_a_claim_matching_NO_row_or_MANY_cannot_bind_one_obligation(self):
        text = seats._dispatch_advice(
            "dispatch:99999999", self.SEAT, snap=self.rows())
        self.assertIn("NO row", text)
        many = {"aabbccdd1111": {"status": "cancelled", "recipient": "x"},
                "aabbccdd2222": {"status": "cancelled", "recipient": "x"}}
        text2 = seats._dispatch_advice(
            "dispatch:aabbccdd", self.SEAT, snap=many)
        self.assertIn("2 rows", text2)

    def test_the_sentence_is_never_empty(self):
        """Silence under a release command could be mistaken for discharge."""
        self.assertTrue(
            seats._dispatch_advice("dispatch:aabbccdd", self.SEAT,
                                   snap=self.rows()).strip())
        for snap in (None, {}, self.rows(), self.rows(status="cancelled")):
            for res in ("dispatch:aabbccdd", "dispatch:", ""):
                text = seats._dispatch_advice(res, self.SEAT, snap=snap)
                self.assertTrue(text.strip(), (res, snap))

    def test_a_HELD_row_is_PAUSED_and_the_claim_is_NOT_stale(self):
        """A HOLD with no carrier is an acknowledged PAUSE, not discharge."""
        text = seats._dispatch_advice(
            "dispatch:aabbccdd", self.SEAT, snap=self.rows(status="held"))
        self.assertIn("HELD", text)
        self.assertIn("PAUSE", text)
        self.assertNotIn("STALE", text)
        self.assertIn("helm dispatch release aabbccdd", text)
        # PAIRED POLE: the three states that REALLY discharge a row are still
        # stale, so this is a rule about `held` and not a refusal to call
        # anything stale.
        for status in dispatches.CLOSED_STATES:
            text2 = seats._dispatch_advice(
                "dispatch:aabbccdd", self.SEAT, snap=self.rows(status=status))
            self.assertIn("STALE", text2)

    def test_a_RETIRED_row_is_terminal_although_its_status_still_says_open(self):
        """Administrative retirement is terminal while leaving status open."""
        retired = self.rows(retired_admin={"at": 1, "by": "a-door"})
        self.assertEqual("open", retired["aabbccdd11223344"]["status"],
                         "MUST-HIT: retirement leaves the status word alone, "
                         "which is what makes this readable only through the "
                         "canonical predicate")
        text = seats._dispatch_advice(
            "dispatch:aabbccdd", self.SEAT, snap=retired)
        self.assertIn("RETIRED", text)
        self.assertIn("STALE", text)
        # PAIRED POLE: the same row WITHOUT the retirement field is a live
        # obligation, so this reads the field and not the lane.
        text2 = seats._dispatch_advice(
            "dispatch:aabbccdd", self.SEAT, snap=self.rows())
        self.assertIn("LIVE obligation", text2)

    def test_an_UNREADABLE_ledger_is_UNKNOWN_in_the_shape_its_producer_RETURNS(self):
        """A failed snapshot is ({}, reason), distinct from readable emptiness."""
        text = seats._dispatch_advice(
            "dispatch:aabbccdd", self.SEAT, snap={},
            ledger_note="the dispatch ledger raised")
        self.assertIn("UNKNOWN", text)
        self.assertIn("the dispatch ledger raised", text)
        self.assertNotIn("NO row", text)
        # PAIRED POLE: the SAME empty dict with NO reason is a readable ledger
        # that genuinely holds no such row, and must keep saying so — or this
        # arm would be satisfied by a reader that called every empty ledger
        # unreadable.
        text2 = seats._dispatch_advice(
            "dispatch:aabbccdd", self.SEAT, snap={}, ledger_note=None)
        self.assertIn("NO row", text2)
        self.assertNotIn("UNKNOWN", text2)


class AChangedDispatchRowUnlatchesItsAdvice(SeatsBase):
    """THE COMPRESSION MUST NOT OUTLIVE THE INSTRUCTION IT COMPRESSES.

    The held-lease sermon latches on (resource, lease id, TTL band) so a
    re-stop on an unchanged set prints one line that refers the seat back to
    the detail printed earlier. All three coordinates are properties of the
    LEASE — and a dispatch claim's instruction is a property of the ROW.
    Another actor can rebind, cancel or hold that row between two stops while
    the resource, the lease and the band all stay put, so the fingerprint said
    "unchanged" and the one-liner pointed at guidance this rung had just been
    built to correct.
    """

    SEAT = "hc2-latch-fixture"
    SESSION = "11111111-2222-3333-4444-555555555555"
    RID = "aabbccdd11223344"

    def _claim(self):
        pk.atomic_write(seats.claims_path(), json.dumps({
            "dispatch:aabbccdd": {
                "lease": "a-lease-token", "holder": self.SEAT,
                "session": self.SESSION,
                "exp_mono": seats._now_mono() + 3600}}))

    def _stop(self, snapshot):
        """One stop whose resident folded `snapshot`: the stand-in resident
        computes the stop facts from it, and the rung reads those (its own
        one reading, the default)."""
        warns, blocks = [], []
        with mock.patch.object(dispatches, "snapshot", return_value=snapshot):
            seats_stop_claims.claims_rung(
                self.SESSION, "a-room", self.SEAT, blocks=blocks, warns=warns)
        return "\n".join(blocks + warns)

    def _row(self, **over):
        row = {"id": self.RID, "chain_root": self.RID, "status": "open",
               "recipient": self.SEAT, "lane": "a-lane"}
        row.update(over)
        return ({self.RID: row}, None)

    def test_a_LIVE_claim_carries_BOTH_its_release_command_and_its_advice(self):
        """The command remains runnable; its advice explains the owed work.
        Claim release does not change the dispatch recipient or status."""
        self._claim()
        out = self._stop(self._row())
        self.assertIn("helm chat release", out,
                      "the seat must be able to release its own lease")
        self.assertIn("--lease a-lease-token", out,
                      "the command must include this holder's token")
        self.assertIn("is OPEN and yours", out,
                      "and it must still say what the row is")
        # THE SEPARATOR IS THE CONTRACT WITH THE EXTRACTOR: a reader takes the
        # command by splitting on it, so the advice must use the same one the
        # lane-lease line already uses. One separator for the whole line now —
        # ` — ` between the lane, its remainder, its command and its advice.
        line = [ln for ln in out.splitlines() if "--lease" in ln][0]
        command = line.split(" — ")[1].strip()
        self.assertTrue(command.startswith("helm "), command)
        self.assertNotIn("is OPEN and yours", command,
                         "the advice must split cleanly off the command")

    def test_a_live_successor_between_stops_replaces_the_parent_advice(self):
        """Same claim, token and TTL band; only the successor set changes."""
        self._claim()
        snap, note = self._row()
        parent = dict(snap[self.RID])
        first = self._stop((snap, note))
        self.assertIn("is OPEN and yours", first)
        kid = {"id": "eeff001122334455", "chain_root": self.RID,
               "supersedes": self.RID, "status": "open",
               "recipient": "successor-seat", "lane": "a-lane"}
        snap[kid["id"]] = kid
        self.assertEqual(snap[self.RID], parent,
                         "the parent itself has not changed")
        self.assertEqual(dispatches.owed(snap), [kid])
        second = self._stop((snap, note))
        self.assertIn("CARRIED by dispatch " + kid["id"][:12], second)
        self.assertIn("this parent is not itself owed", second)
        self.assertNotIn("is OPEN and yours", second)
        line = [ln for ln in second.splitlines() if "--lease" in ln][0]
        command = line.split(" — ")[1].strip()
        self.assertIn("helm chat release", command)
        self.assertIn("--lease a-lease-token", command)
        self.assertNotIn("CARRIED", command)
        # An unchanged carried state must still take the compression path.
        third = self._stop((snap, note))
        self.assertIn("lease", third.lower())
        self.assertNotIn("CARRIED by dispatch", third)

    def test_a_row_REBOUND_between_two_stops_re_prints_the_advice(self):
        """The successive-stop control: same lease, same TTL band, CHANGED
        row. The second stop states what the row IS, rather than referring the
        seat to a sentence that describes a different row."""
        self._claim()
        first = self._stop(self._row())
        self.assertIn("is OPEN and yours", first,
                      "MUST-HIT: the first stop prints the full advice")
        self._claim()
        second = self._stop(self._row(recipient="another-seat"))
        self.assertIn("REBOUND", second,
                      "the row changed, so the advice must be re-printed")
        self.assertIn("@another-seat", second)

    def test_an_UNCHANGED_row_still_compresses_on_the_next_stop(self):
        """THE OPPOSITE POLE, and without it this arm would be satisfied by
        deleting the latch. A re-stop on a genuinely unchanged claim must
        still compress, because that is what the latch is for."""
        self._claim()
        first = self._stop(self._row())
        self.assertIn("is OPEN and yours", first)
        self._claim()
        second = self._stop(self._row())
        # UNCONDITIONAL POSITIVE ON THE SAME OBSERVABLE: the compressed stop
        # still SPEAKS — it carries the count and refers to the detail — so
        # this arm cannot be satisfied by a rung that fell silent or raised.
        self.assertIn("lease", second.lower(),
                      "MUST-HIT: the compressed stop still names the lease")
        self.assertNotIn("is OPEN and yours", second,
                         "an unchanged row must still compress to one line")



class CursorLockPopulationTest(SeatsBase):
    """A cursor transaction must not mint a permanent file per cursor.

    `_flocked` acquires by opening the path, and release unlocks and closes
    but never unlinks — unlinking a held lock detaches the name and hands the
    next caller a fresh inode nobody else's flock covers. So a per-cursor lock
    file is permanent once created, and cursors are born per (room, seat,
    session): on the live bus the flat chat directory reached 105,276 dirents
    of which 52,315 were `.lock` against 459 rooms, and every `list_rooms`
    getdents pays for all of them. Exclusion moved to the room lock that
    already covered every path in the closure; these arms pin BOTH halves —
    the file is not created, and the exclusion that replaced it is real."""

    ROOM = "main"

    def seat_up(self, seat="alice"):
        seats.join(session="s-" + seat, cwd="/tmp/p", seat=seat)

    def _locks(self):
        return {n for n in os.listdir(chat.chat_dir()) if n.endswith(".lock")}

    def _can_take(self, path):
        """True iff a FRESH open+flock on `path` acquires. flock binds to the
        open file description, so a second open in this process conflicts with
        a held one exactly as another process would."""
        from helm.seats_common import _flocked
        with _flocked(path, blocking=False) as probe:
            return probe.f is not None

    def test_a_cursor_transaction_mints_no_lock_of_its_own(self):
        self.seat_up()
        pair = seats_cursor._cursor_pair(self.ROOM, "alice", "s-alice")
        room_lock = os.path.basename(
            seats_cursor._cursor_txn_lock_path(pk.slug(self.ROOM)))
        siblings = {os.path.basename(p) + ".lock" for p in pair}
        with seats_cursor._cursor_locks(pair, estate=False):
            inside = self._locks()
        # BOTH POLES ON ONE LISTING. It must SHOW the lock this transaction is
        # entitled to mint — the room's — and must NOT show a per-cursor
        # sibling. Without the first pole the second is equally satisfied by a
        # listing that went blind or a transaction that never ran.
        self.assertIn(room_lock, inside,
                      "MUST-HIT: the room transaction lock is still minted")
        self.assertEqual(inside & siblings, set(),
                         "a cursor-sibling lock file was created")

    def test_the_room_lock_is_what_actually_excludes_a_second_writer(self):
        """The cure is only sound if the lock that replaced the per-path one
        is HELD for the section. Both poles on one observable: held inside,
        free outside — an arm that only checked 'cannot take' would pass
        against a path that is permanently unacquirable."""
        self.seat_up()
        pair = seats_cursor._cursor_pair(self.ROOM, "alice", "s-alice")
        room_lock = seats_cursor._cursor_txn_lock_path(pk.slug(self.ROOM))
        with seats_cursor._cursor_locks(pair, estate=False):
            self.assertFalse(self._can_take(room_lock),
                             "the room transaction lock was not held")
        self.assertTrue(self._can_take(room_lock),
                        "MUST-HIT: the room lock is free once released")

    def test_a_path_with_no_room_is_refused_not_locked(self):
        """What makes "every path is covered by its room" TOTAL rather than
        usual. A first cut kept a sibling-lock fallback for unparsable paths;
        the suite showed it unreachable, because the closure raises on such a
        path before any lock is taken. Pinned so a later loosening of the
        closure cannot quietly admit an uncovered path."""
        self.seat_up()
        chat._ensure_dir()
        odd = os.path.join(chat.chat_dir(), "not-a-cursor-name")
        real = seats_cursor.cursor_path(self.ROOM, "alice", "s-alice")
        self.assertEqual(
            [seats_cursor.parse_cursor_path(p) is None for p in (odd, real)],
            [True, False],
            "the fixture must be unparsable and its control readable")
        with self.assertRaises(OSError):
            with seats_cursor._cursor_locks({odd}, estate=False):
                pass
        # The SAME call shape with a parsable path runs, so one listing carries
        # both poles: the room lock a real transaction mints is present, and
        # the refused path left nothing behind.
        pair = seats_cursor._cursor_pair(self.ROOM, "alice", "s-alice")
        with seats_cursor._cursor_locks(pair, estate=False):
            pass
        after = self._locks()
        self.assertIn(os.path.basename(
            seats_cursor._cursor_txn_lock_path(pk.slug(self.ROOM))), after,
            "MUST-HIT: a parsable transaction does mint its room lock")
        self.assertNotIn(os.path.basename(odd) + ".lock", after,
                         "a refused path must leave no lock behind")
