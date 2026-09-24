#!/usr/bin/env python3
"""Every escape from "a seat cannot idle past a NEW inbox row", enumerated.

THE PROMISE, as docs/VERBS.md and docs/HOOKS.md state it after 04feba4: an
agent cannot idle past a NEW inbox row, bounded by the pending-fingerprint
latch (a re-stop on the SAME rows passes). The 0.2 council's one finding was
"helm's claims about itself are not checked", and this promise is the case
that kept proving it: each review round named one more way past the guard, and
each repair named that one. A promise with an unenumerated escape set is the
same defect as a detector with an unenumerated bypass set — the cure is the
whole object, so this file IS the enumeration.

THE COMPLETE ESCAPE SET, and every one of them is either closed here or
DELIBERATE-AND-NAMED:

  1. `stop_hook_active` short-circuit (seats.py stop_guard, first statement) —
     DELIBERATE. The harness is already continuing off a stop hook; blocking
     again is the infinite-loop shape both reference guards forbid. A row that
     lands mid-turn is genuinely not shown at that stop. Pinned below so it
     can never become accidental, and named in the promise's own boundary.
  2. The pending-fingerprint latch — DELIBERATE and already documented: the
     first stop on a given pending set blocks, a re-stop on the SAME set
     passes, any NEW row re-arms. An unlatchable gate would block every stop
     forever, which is the wedge the guard is forbidden to become — and for
     most of this file's life that sentence was ASPIRATION, not code: the
     latch write swallowed its OSError and blocked anyway, an unconditional
     re-block wearing a once-per-set promise. The rung now honours the
     sentence: an UNWRITABLE latch degrades the block to a WARN that still
     shows the rows and the cure — so while the latch dir is unwritable, a
     row surfaces at every stop but never blocks. That non-blocking window
     is a DELIBERATE-AND-NAMED member of this set (the beacon rung's
     no-latch-no-block law), pinned end to end in tests/test_seats.py
     StopGuardTest.test_an_unwritable_inbox_latch_degrades_to_warn_not_block.
  3. `HELM_STOP_GUARD=0` / `HELM_STOP_GUARD_INBOX=0` — DELIBERATE operator
     kill switches, pinned so a silent default flip is a red test.
  4. `_room_dirty` answering CLEAN on an unreadable room — A BUG, closed by
     this lane. Only FileNotFoundError is emptiness; every other OSError is a
     room that might hold rows and must take the expensive path. Its own
     contract says a false negative cannot happen, and this was one.
  5. `seats.cmd` catching every stop_guard exception and returning 0 — the
     FAIL-OPEN law is right (a guard that can wedge the fleet is worse than
     one that misses a row) and its SILENCE was the bug: a crashed guard read
     as an ordinary clean allow. Found by a review attacking the enumeration
     rather than the prose, which is what made it the class and not a case.
     Closed by making the failure LOUD; the stop still passes.
  6. `ROOM_SCAN_CAP` bounding a pass to 16 rooms — DELIBERATE, and the honest
     statement is that coverage is EVENTUAL, not immediate: `_fair_room_slice`
     rotates the unscanned remainder so every room comes up, but at any SINGLE
     stop a row in an unscanned room does not block. A cap that skips work may
     never produce a confident verdict about the work it skipped.
     TWO CORRECTIONS a review measured against this paragraph, both of which
     made the paragraph itself false, which is exactly the eighth-path finding
     this file invites:
       6a. The bound was not the bound. `size = ROOM_SCAN_CAP - 1` assumed the
           pinned set held ONE room; it holds up to FOUR (the seat's DM lane,
           primary, home, main), so a pass advertised as 16 scanned 19. The
           budget now subtracts the pins that are already committed. The pins
           themselves are never dropped — losing a seat's own DM lane to a cap
           would be worse than exceeding it — so the honest bound is 16 OR the
           pin count, whichever is larger, and that is the sentence that
           belongs here rather than a flat 16.
       6b. "Rotation makes coverage eventual" was conditional and did not say
           so. When the round-robin state is unreadable, `_fair_room_slice`
           fell back to the same ALPHABETICAL PREFIX on every pass forever, so
           rooms sorting past the budget were never scanned again and
           "eventual" quietly became "never" — escape 4's shape, one layer up,
           attacking the CLAIM rather than the code. It still fails open (a
           pass that cannot rotate must still deliver) and now says out loud
           that coverage is not eventual until rotation is readable again.
  7. A failed room LISTING answering "no other rooms" — A BUG of escape 4's
     exact shape, found while pinning 6 and closed here: the scan proceeds
     with what it has and SAYS coverage is unknown instead of implying the
     estate is empty. It said so only on stderr, which a Stop hook that exits
     0 never shows the seat, so the stop still called the inbox clean over
     the rooms the listing could not name. The stop guard now names the
     unlistable estate in escape 10's WARN and withholds the clean line
     (task/2530). Pinned in tests/test_seats.py
     AnUnreadableCursorIsNamedAtTheStopTest.

  9. An unreadable room TAIL reading as inbox-clean — A BUG, found by
     review and closed here. `_tail` answered an unreadable
     room with the SAME None it uses for "genuinely nothing new", so a room
     helm could not READ was indistinguishable from an empty one. This is
     escape 4's sentence one layer DOWN: escape 4 fixed the cheap STAT
     precheck, and this is the READ behind it — so the precheck could
     correctly answer DIRTY and the reader still answer empty, and the stop
     read inbox-clean anyway. Only FileNotFoundError is emptiness; every
     other OSError now says the rows are UNKNOWN, not absent, and the pass
     still proceeds.
  8. The GATE HOOK swallowing `timeout`'s rc 124 in silence — A BUG, found by
     attacking the enumeration and closed here. The generated Stop
     command was `timeout N helm chat stop-guard --hook-json; rc=$?; [ "$rc" =
     2 ] && exit 2; exit 0`. Propagating only 2 is CORRECT and stays: a helm
     crash must not wedge every seat's turn end. But rc 124 is `timeout`
     KILLING the guard before it reached a verdict, and it was the one
     swallowed code with no trace — a crash writes a traceback to stderr, a
     timeout kill writes nothing. So a guard that never ran was byte-identical,
     from the harness's view, to one that ran and found nothing. Same sentence
     as escapes 4, 5 and 7, one layer out at the hook boundary rather than
     inside seats.py. It still exits 0 and now says the stop went UNCHECKED.

     ESCAPE 8 WAS CLOSED TOO NARROWLY, and the gap it left cost a fleet-wide
     outage on 2026-08-04 (task/258). "A crash writes a traceback to stderr"
     covered rc 1 but not rc **127**, which is not a crash at all: it is
     `timeout` finding nothing to exec because the hook's helm path is gone.
     The one enumerated code got a voice and the unenumerated ones kept the
     silence. The arm set is EXHAUSTIVE now — `case "$rc" in 2) exit 2 ;; 0) ;;
     124) … ;; 127) … ;; *) … ;; esac; exit 0` — so a code nobody anticipated
     is loud by construction rather than after the next incident.

  10. An UNREADABLE consumption cursor contributing no rows -- DELIBERATE AND
      NAMED. A cursor in flight, moved under the read, or malformed yields no
      rows, because replaying it from zero brings rows this seat already
      consumed back as owed. So a row really waiting in that room does not
      block this stop. The failure mode to guard is SILENCE: a room the scan
      could not read reading as a room with nothing in it, under an "inbox
      clean" line. The stop guard passes coverage, names each unreadable room
      with the reader's reason in one WARN on both stop paths, never blocks,
      and does not call that stop clean (task/2530). Pinned end to end in
      tests/test_seats.py AnUnreadableCursorIsNamedAtTheStopTest.

AN ELEVENTH path outside this set is a finding against the enumeration, which is
the bar this file exists to set — the same bar codex held the runtime-family
class to before it closed by construction. Three rounds have now moved that
bar (4 -> 6 -> 7 -> 8 -> 9), task/2530 moved it to 10 with the unreadable
cursor, and rounds two and three each moved it again
WITHOUT adding an
entry: 6a and 6b were corrections to a paragraph that was simply false about
its own subject. That is the sharper version of what this file is for. An
unenumerated escape is a gap; a paragraph confidently describing a bound the
code does not enforce is a LIE the reader has no reason to check, and it lives
in the artifact whose entire job is being checkable. Every number in here is
falsifiable on purpose. The enumeration is only as good as its next attacker.
"""
import contextlib
import errno
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import beacons, chat, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_NAME", "HELM_CHAT_ROOM",
            "HELM_STOP_GUARD", "HELM_STOP_GUARD_INBOX", "HELM_SCRATCH_GC",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


class RoomDirtyUnknownTest(unittest.TestCase):
    """Escape 4: the cheap precheck may never answer CLEAN on UNKNOWN."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-dirty-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        self.cursor = {"dev": 1, "ino": 2, "off": 10}

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_unreadable_room_is_DIRTY_not_clean(self):
        """Permission, I/O, not-a-directory, a tmpfs mid-remount: each is a
        room that MIGHT hold rows. Answering clean drops them silently, which
        is the false negative this function's contract forbids."""
        for err in (errno.EACCES, errno.EIO, errno.ENOTDIR, errno.ELOOP,
                    errno.ENAMETOOLONG):
            with self.subTest(errno=errno.errorcode[err]):
                with mock.patch.object(seats, "_cursor",
                                       return_value=dict(self.cursor)), \
                        mock.patch.object(
                            os, "stat",
                            side_effect=OSError(err, os.strerror(err))):
                    self.assertIs(seats._room_dirty("main", "seat"), True)

    def test_an_ABSENT_room_is_still_clean(self):
        """The one OSError that really is emptiness — the control that keeps
        the fix from degenerating into always-dirty."""
        with mock.patch.object(seats, "_cursor",
                               return_value=dict(self.cursor)), \
                mock.patch.object(os, "stat",
                                  side_effect=FileNotFoundError(
                                      errno.ENOENT, "no such file")):
            self.assertIs(seats._room_dirty("main", "seat"), False)

    def test_a_never_seen_room_and_a_grown_room_stay_dirty(self):
        """The two positive controls: no cursor at all, and an append past
        the recorded offset. Without these the fix above could be an
        always-True stub and this file would still be green."""
        with mock.patch.object(seats, "_cursor", return_value=None):
            self.assertIs(seats._room_dirty("main", "seat"), True)

        class _St:
            st_dev, st_ino, st_size = 1, 2, 99      # same file, MORE bytes

        with mock.patch.object(seats, "_cursor",
                               return_value=dict(self.cursor)), \
                mock.patch.object(os, "stat", return_value=_St()):
            self.assertIs(seats._room_dirty("main", "seat"), True)

    def test_an_unchanged_room_is_clean(self):
        """The negative control: identical identity and size is the one shape
        that may answer clean, and it must keep doing so or the hot path
        collapses into a full scan per quiet room."""
        class _St:
            st_dev, st_ino, st_size = 1, 2, 10

        with mock.patch.object(seats, "_cursor",
                               return_value=dict(self.cursor)), \
                mock.patch.object(os, "stat", return_value=_St()):
            self.assertIs(seats._room_dirty("main", "seat"), False)


class GuardFailureIsLoudTest(unittest.TestCase):
    """Escapes 5 and 7: fail-open is the law, silence was the bug."""

    def test_a_crashed_guard_says_so_and_still_allows_the_stop(self):
        """The repro: the caller swallowed everything and returned 0, so
        an exploded guard was indistinguishable from a clean one."""
        import io
        import contextlib
        from helm import seats as S
        err = io.StringIO()
        with mock.patch.object(S, "stop_guard",
                               side_effect=OSError("pending scan exploded")), \
                contextlib.redirect_stderr(err):
            rc = S.cmd("stop-guard", [])
        self.assertEqual(rc, 0)                       # never wedge a stop
        text = err.getvalue()
        self.assertIn("COULD NOT RUN", text)          # ...but never silently
        self.assertIn("UNCHECKED", text)
        self.assertIn("OSError", text)

    def test_an_unlistable_room_estate_reports_unknown_coverage(self):
        """Escape 7: `names = []` claimed the fleet had no other rooms.

        THE CHAT DIR IS PINNED TO A REAL ONE ON PURPOSE, and this test was RED
        in the whole suite without it (gate 64f68aab1cf3c570, 6273 run
        / 1 failed; it passed standalone, which is the tell).

        `chat.list_rooms` returns [] through an EARLY RETURN when chat_dir()
        is not a directory (chat.py:239) — it never reaches listdir. So the
        PermissionError this test installs was never raised, no warning was
        printed, and the arm went green-to-red purely on which earlier test
        last left chat_dir pointing at a cleaned-up tmpdir. The test was
        asserting on ambient state that 6000 other tests do not guarantee.

        Reproduced both directions before fixing: chat_dir a real dir ->
        warning present; chat_dir a deleted path -> warning absent, exactly
        what the suite saw."""
        import io
        import contextlib
        import tempfile
        from helm import seats as S, chat
        room_dir = tempfile.mkdtemp(prefix="helm-scanrooms-")
        self.addCleanup(shutil.rmtree, room_dir, True)
        # PRECONDITION, asserted rather than assumed: if this ever stops being
        # a directory the early return eats the whole test, and the failure
        # should say THAT instead of "the warning was missing".
        self.assertTrue(os.path.isdir(room_dir))
        err = io.StringIO()
        with mock.patch.object(chat, "chat_dir", lambda: room_dir), \
                mock.patch.object(S.os, "listdir",
                                  side_effect=PermissionError(13, "denied")), \
                contextlib.redirect_stderr(err):
            try:
                S._scan_rooms("main", seat="seat", scan_lane="stop")
            except Exception:
                pass          # the scan is DELIBERATELY exploded; the warning
                              # on stderr is the assertion, not the return
        self.assertIn("coverage is UNKNOWN", err.getvalue())


class DeliberateEscapesTest(unittest.TestCase):
    """Escapes 1-3: real, deliberate, and pinned so they stay deliberate.

    These arms do not argue the escapes are wrong. They assert the guard
    behaves as its docstring says, so that a future change which quietly
    widens one of them turns a test red instead of turning a promise false."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-escape-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        # THE REAPER IS OFF BECAUSE THIS CLASS DRIVES THE REAL STOP HOOK.
        # stop_guard's silent-mechanical lane DELETES dead-session scratch
        # under the host's /tmp/claude-* estate; a test that calls it without
        # this eats real files belonging to live sessions. test_scratch's
        # HostMutationTripwireTest is source-driven and caught this file the
        # moment it existed — which is the guard-catches-the-new-module case
        # working exactly as designed.
        os.environ["HELM_SCRATCH_GC"] = "0"

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stop_hook_active_blocks_never_but_now_looks(self):  # noqa: VACUOUS_ASSERTION — the no-reblock half IS emptiness; the look is pinned positively below, and the warn path is asserted in tests.test_seats
        """ESCAPE 1, CLOSED by the #74 stop-guard fix landing beside this
        enumeration (the 2026-08-01 5.5-hour fleet idle is its receipt): the
        short-circuit once returned before the guard ever read the inbox. The
        composed contract keeps the half that was always right — blocking
        under stop_active stays forbidden — and drops the half that was not:
        the inbox IS read, and mid-turn rows surface as warns (asserted with
        real rows in tests.test_seats; this arm pins the enumeration's own
        claim, that the guard LOOKS)."""
        with mock.patch.object(seats, "_pending_all", return_value=[]) as pending:
            blocks, warns = seats.stop_guard(session="s", seat="seat",
                                             stop_active=True)
        self.assertEqual((blocks, warns), ([], []))
        pending.assert_called()          # the short-circuit no longer blinds it

    def test_the_kill_switches_are_honoured_and_are_the_only_ones(self):  # noqa: VACUOUS_ASSERTION — kill-switch contract is emptiness; the not-called assertion is the observable
        """ESCAPE 3, deliberate: two named env switches, pinned by name so a
        rename or a silent default flip is a red test rather than a fleet-wide
        silent disarm."""
        for var in ("HELM_STOP_GUARD", "HELM_STOP_GUARD_INBOX"):
            with self.subTest(var=var):
                os.environ[var] = "0"
                try:
                    with mock.patch.object(seats, "_pending_all") as pending:
                        seats.stop_guard(session="s", seat="seat")
                    pending.assert_not_called()
                finally:
                    os.environ.pop(var, None)


class GateHookTimeoutIsLoudTest(unittest.TestCase):
    """Escape 8: a guard killed by its own timeout may not read as clean.

    These arms RUN the generated shell rather than matching its text, because
    the defect was never in the wording — it was in what the harness observed,
    and only executing it can show that."""

    def _gate_spec(self):
        from helm import hooks
        for sp in hooks.SPECS:
            if sp.get("gate"):
                return sp
        self.fail("no gate spec — escape 8's subject does not exist")

    def _run(self, body, timeout_s=1):
        """Execute the gate command's SHAPE with `body` standing in for the
        guard, and report exactly what the harness would see."""
        import subprocess
        from helm import hooks
        import shlex
        spec = self._gate_spec()
        words = shlex.split(hooks.spec_command(spec))
        # Substitute the real CHILD with a stub whose rc we control; the
        # rc-handling ladder — the thing under test — is the tracked
        # bin/helm-hook, executed, with every metadata operand as rendered.
        self.assertEqual(words[1], "gate", words)    # control: shape as assumed
        self.assertEqual(words[6], hooks.helm_bin(), words)
        head = " ".join(shlex.quote(w) for w in words[:6])
        # A PRIVATE SUPPRESSION WINDOW PER RUN. The rendered 124 arm now speaks
        # once per hook class per ten-minute window and counts the rest
        # (`hooks.hookalarm`), and that state is shared across PROCESSES — so
        # without a directory of its own, one arm's timeout silences the next
        # arm's and the ladder reads as a channel that says nothing. That is a
        # leaked channel wearing the costume of the exact defect these escape
        # arms exist to catch, which is why each run gets its own.
        import os as _os
        import tempfile as _tempfile
        p = subprocess.run(["bash", "-c", "%s timeout %g %s"
                            % (head, timeout_s, body)],
                           capture_output=True, text=True,
                           env=dict(_os.environ,
                                    HELM_HOOK_ALARM_DIR=_tempfile.mkdtemp()))
        return p.returncode, p.stderr

    def test_a_timed_out_guard_says_the_stop_was_unchecked(self):
        # A real kill at 0.1 s: rc 124 comes from `timeout` itself, as live.
        rc, err = self._run("sleep 30", timeout_s=0.1)
        self.assertEqual(rc, 0)                      # fail-open law holds
        self.assertIn("TIMED OUT", err)
        self.assertIn("UNCHECKED", err)

    @staticmethod
    def _rc(code):
        """A stub that EXITS with `code`. It must be a real executable, not a
        bare `exit N`: `timeout` execs its argument, and `exit` is a shell
        builtin, so `timeout 1 exit 2` fails to exec and yields 127 instead.
        That mistake made the rc-2 control below report a false failure of the
        product on its first run — the control was wrong, the fix was fine."""
        return "sh -c 'exit %d'" % code

    def test_a_refusing_guard_still_blocks_and_stays_quiet(self):  # noqa: VACUOUS_ASSERTION — the ABSENCE of the timeout line IS the contract here; rc==2 above is the unconditional positive control on the same run
        """The negative control that matters most: the timeout branch must not
        intercept a real BLOCK. Without this arm the fix could swallow rc 2 and
        every other test here would still pass."""
        rc, err = self._run(self._rc(2))
        self.assertEqual(rc, 2)
        self.assertNotIn("TIMED OUT", err)

    def test_a_crashing_or_absent_guard_names_its_own_failure(self):
        """THIS ARM USED TO PIN THE SILENCE, AND THE SILENCE WAS THE NEXT BUG.

        It read: "rc 1 and 127 keep failing open SILENTLY — deliberately,
        because a crash already writes its own traceback", the worry being that
        announcing every nonzero would "put a scary line under every ordinary
        helm error". Both halves were measured false on 2026-08-04 (task/258):

          * rc 127 is not a crash. It is `timeout` finding NOTHING TO EXEC —
            the hook's helm path is gone. Eight settings files named a deleted
            lane room's bin/helm, both gates among them, and four credential
            homes ran unguarded with nothing said. A guard that could not run
            is strictly worse than one that timed out, and only the timeout
            spoke.
          * there is no "ordinary helm error" behind this line. seats.py's
            stop_guard catches its own exceptions, says THE GUARD COULD NOT RUN
            itself, and returns **0** — so the wrapper's default arm never
            doubles it. An rc that reaches here at all means helm died before
            its own handler, which is not ordinary.

        What survives from the original: fail-open (rc 0) is still the law, and
        the arms must not cross-fire — a crash must never be reported as a
        timeout.

        The second bullet is WHY the change is safe, so it is measured below
        rather than asserted in prose: anyone who sees a loud line under a helm
        error and reaches for a revert has to refute a green test first."""
        # CONTROL FOR THE SAFETY ARGUMENT: an exception inside stop_guard is
        # caught by helm itself, announced by helm itself, and returns 0 — so
        # rc never reaches the wrapper's default arm for an ordinary failure,
        # and the new line cannot double an existing one.
        with mock.patch.object(seats, "stop_guard",
                               side_effect=RuntimeError("broken rung")):
            out, err_txt = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err_txt):
                rc_internal = seats.cmd("stop-guard", [])
        self.assertEqual(rc_internal, 0, "an internal crash must fail OPEN")
        self.assertIn("THE GUARD COULD NOT RUN", err_txt.getvalue())

        rc, err = self._run(self._rc(127))
        self.assertEqual(rc, 0)
        self.assertIn("THE GUARD IS MISSING", err)
        self.assertIn("ALLOWED and UNCHECKED", err)
        self.assertNotIn("TIMED OUT", err)
        rest = [self._run(self._rc(code)) for code in (1, 3)]
        self.assertEqual([rc for rc, _e in rest], [0, 0])
        blob = "\n".join(e for _rc, e in rest)
        self.assertIn("THE GUARD FAILED rc=1", blob)
        self.assertIn("THE GUARD FAILED rc=3", blob)
        self.assertIn("ALLOWED and UNCHECKED", blob)
        self.assertNotIn("TIMED OUT", blob)

    def test_a_clean_guard_is_silent_and_allows(self):
        rc, err = self._run("true")
        self.assertEqual(rc, 0)
        self.assertEqual(err.strip(), "")


if __name__ == "__main__":
    unittest.main()


class ScanCapAndRotationAreProvenTest(unittest.TestCase):
    """THE ARMS THAT SHOULD HAVE SHIPPED WITH 6a AND 6b.

    A review: "cap/clamp/RR-warning mutations all survive". Verified
    before writing this — reverting `size = max(0, ROOM_SCAN_CAP - len(rooms))`
    to the old `ROOM_SCAN_CAP - 1` left 398 tests GREEN. The fixes in b4c893d
    were real and the corrections to the enumeration were true, and NOTHING
    tested either one: the escape-6 corrections lived entirely in prose. A
    green suite over an unproven fix is exactly the vacuity this file exists
    to refuse, so the file had the defect it was written to catch."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-cap-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_cap_bounds_the_PASS_not_the_tail(self):
        """6a. `ROOM_SCAN_CAP - 1` assumed ONE pinned room; there are up to
        FOUR (the seat's DM lane, primary, home, main), so a pass advertised
        as 16 scanned 19 — a probe measured it. The budget must subtract the
        pins already committed."""
        from unittest import mock
        many = ["z%03d" % i for i in range(40)]
        with mock.patch.object(seats.chat, "list_rooms", return_value=many), \
                mock.patch.object(seats.os.path, "exists", return_value=True), \
                mock.patch.object(seats, "seat_scope",
                                  return_value={"home": "homeroom"}):
            got = seats._scan_rooms(primary="primaryroom", seat="seat",
                                    session="s", scan_lane="stop")
        self.assertLessEqual(
            len(got), seats.ROOM_SCAN_CAP,
            "a pass advertised as %d rooms scanned %d — the cap bounded the "
            "TAIL, not the pass" % (seats.ROOM_SCAN_CAP, len(got)))

    def test_a_failed_enumeration_survives_every_later_estate_write(self):
        """A RECORDED COVERAGE FAILURE IS NEVER OVERWRITTEN, and the scan
        overwrote its own. The listing raises, the scan records `unlistable`
        and keeps going with the rooms it already holds -- and EVERY path out
        of it ends in one more estate write, which replaced that failure with
        `complete`. The consumer then read a complete estate over readable
        empty pins and drained a backlog sitting in the room the listing could
        not name. This drives the real producer through a real raise, not an
        injected evidence dict."""
        from unittest import mock
        report = {}
        with mock.patch.object(seats.chat, "list_rooms",
                               side_effect=OSError("estate is unreadable")), \
                mock.patch.object(seats.os.path, "exists", return_value=True), \
                mock.patch.object(seats, "seat_scope",
                                  return_value={"home": "homeroom"}):
            got = seats._scan_rooms(primary="primaryroom", seat="seat",
                                    session="s", scan_lane="stop",
                                    report=report)
        self.assertTrue(got, "fixture: the scan returned nothing at all, so "
                             "this arm is not about a pass that continued")
        self.assertEqual(report["estate"][0], "unlistable",
                         "the failed enumeration was overwritten by a later "
                         "estate write: %r" % (report.get("estate"),))
        # AND THE CONSUMER REFUSES ON IT. A drain decided over readable empty
        # pins is exactly the false drain this records against.
        verdict = beacons.sample_is_complete(
            {"scanned": tuple(got), "seen": (), "bounded": {},
             "estate": report["estate"]})
        self.assertTrue(verdict.unknown)
        # THE CONTROL, unconditional and through the same producer: a listing
        # that SUCCEEDS records a complete estate and the same consumer proves
        # the drain -- so the refusal above is the failure, not the shape of
        # every answer this pass can give.
        ok = {}
        with mock.patch.object(seats.chat, "list_rooms", return_value=["z1"]), \
                mock.patch.object(seats.os.path, "exists", return_value=True), \
                mock.patch.object(seats, "seat_scope",
                                  return_value={"home": "homeroom"}):
            seats._scan_rooms(primary="primaryroom", seat="seat", session="s",
                              scan_lane="stop", report=ok)
        self.assertEqual(ok["estate"][0], "complete")
        self.assertTrue(beacons.sample_is_complete(
            {"scanned": ("helm",), "seen": (), "bounded": {},
             "estate": ok["estate"]}).proven)

    def test_the_pins_are_never_dropped_to_satisfy_the_cap(self):
        """The other direction, and the reason the fix is a subtraction rather
        than a slice of the whole: losing a seat's own DM lane to a cap would
        be worse than exceeding it."""
        from unittest import mock
        many = ["z%03d" % i for i in range(40)]
        with mock.patch.object(seats.chat, "list_rooms", return_value=many), \
                mock.patch.object(seats.os.path, "exists", return_value=True), \
                mock.patch.object(seats, "seat_scope",
                                  return_value={"home": "homeroom"}):
            got = seats._scan_rooms(primary="primaryroom", seat="seat",
                                    session="s", scan_lane="stop")
        self.assertIn("primaryroom", got)
        self.assertIn(seats.dm_lane("seat"), got)

    def test_dead_rotation_state_SAYS_coverage_is_no_longer_eventual(self):
        """6b. An unreadable round-robin state returned the same alphabetical
        prefix on every pass forever, so rooms past the budget were never
        scanned again and "coverage is EVENTUAL" quietly became "never". It
        still fails open — a pass that cannot rotate must still deliver — and
        it must SAY so."""
        import io
        import contextlib
        from unittest import mock
        err = io.StringIO()
        # PATCH WHERE THE CODE READS IT. The rotation lives in seats_roomscan
        # and imported `_flocked` by name, so a patch on the facade's binding
        # reaches a name this code no longer consults -- the lock then SUCCEEDS,
        # nothing warns, and the arm reads as a regression in the message.
        from helm import seats_roomscan
        with mock.patch.object(seats_roomscan, "_flocked",
                               side_effect=OSError("rr state unreadable")), \
                contextlib.redirect_stderr(err):
            out = seats._fair_room_slice(["b", "a", "c"], "seat", "s", 2, "stop")
        self.assertEqual(out, ["a", "b"])          # fails OPEN: still delivers
        text = err.getvalue()
        self.assertIn("NOT eventual", text)
        self.assertIn("FIXED prefix", text)

    def test_a_readable_rotation_stays_silent(self):
        """NEGATIVE CONTROL: the warning must fire on failure only. Without
        this the arm above could pass on a version that warns every pass and
        buries every real signal under it."""
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = seats._fair_room_slice(["b", "a", "c"], "seat", "s", 2, "stop")
        self.assertEqual(len(out), 2)
        self.assertNotIn("NOT eventual", err.getvalue())

    def test_an_unreadable_room_TAIL_says_so_and_never_reads_clean(self):
        """THE NINTH ESCAPE. `_tail` answered an unreadable room
        with the same None it uses for "genuinely nothing new", so a room helm
        could not READ was indistinguishable from an empty one and the stop
        read inbox-clean. Escape 4's sentence one layer down — that fixed the
        cheap STAT, this is the READ behind it, so the precheck could say
        DIRTY and the reader still answer empty."""
        import io
        import contextlib
        from unittest import mock
        for err in (errno.EACCES, errno.EIO, errno.ENOTDIR):
            with self.subTest(errno=errno.errorcode[err]):
                buf = io.StringIO()
                with mock.patch("builtins.open",
                                side_effect=OSError(err, os.strerror(err))), \
                        contextlib.redirect_stderr(buf):
                    seats._tail("someroom", {"off": 0})
                text = buf.getvalue()
                self.assertIn("UNKNOWN", text)
                self.assertIn("NOT proven clean", text)

    def test_an_ABSENT_room_tail_stays_silent(self):
        """NEGATIVE CONTROL: absence really is emptiness and must not warn, or
        every seat with a not-yet-created room gets a scary line every stop —
        the same over-correction escape 4 had to avoid."""
        import io
        import contextlib
        from unittest import mock
        buf = io.StringIO()
        with mock.patch("builtins.open",
                        side_effect=FileNotFoundError(errno.ENOENT, "nope")), \
                contextlib.redirect_stderr(buf):
            self.assertIsNone(seats._tail("gone", {"off": 0}))
        self.assertEqual(buf.getvalue().strip(), "")


class DeadSinkCannotStealTheWakeTest(unittest.TestCase):
    """ESCAPE 6: A CONSUMER THAT CANNOT DELIVER CONSUMES ANYWAY.

    The delivery path commits the cursor PAST an addressed occurrence and
    only then emits (helm/seats_delivery.py, the addressed-hit branch), so a
    consumer whose output reaches nobody has still taken the row out of the
    ledger as far as every other reader is concerned. Two live consumers
    share one (seat, session) cursor; the dead-sink one acquiring first is
    all it takes. The result is not a duplicate and not noise — it is a wake
    that silently never happens while every surface reports coverage, which
    is the exact shape this file exists to enumerate.

    THE FENCE IS TRI-STATE AND ONLY *PROVEN* UNUSABLE WITHHOLDS, because the
    delivery path must refuse only what can be PROVEN to reach no reader —
    the same asymmetry beacons.sink_state is built on. UNKNOWN consumes
    exactly as it always has, and the second arm here is what stops the first
    being satisfied by a path that delivers nothing to anyone.
    """

    ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
                "HELM_CHAT_NODE_URL", "HELM_CHAT_NAME")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-deadsink-")
        self.prior = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    SEAT, SID = "deadsink", "s-deadsink"

    def addressed(self, text):
        seats.join(session=self.SID, seat=self.SEAT, cwd=self.tmp)
        return chat.post("@%s %s" % (self.SEAT, text), who="alice")

    def test_a_PROVEN_dead_sink_consumer_cannot_take_the_addressed_wake(self):
        """THE ROW'S OWN BAR: the dead sink must not steal the commit, and a
        usable consumer must then receive a REAL addressed wake. Asserting
        only that the dead consumer emitted nothing would pass against a fence
        that also destroyed the row, so the proof is the SECOND call."""
        self.addressed("this must survive a dead consumer")
        seen = []

        # THE DEAD-SINK CONSUMER ACQUIRES FIRST, exactly as the race requires.
        line = seats.deliver_any(session=self.SID, seat=self.SEAT,
                                 emit=seen.append, sink_usable=False)
        self.assertIsNone(line, "a consumer that cannot deliver reported a "
                                "delivery: %r" % (line,))
        self.assertEqual(seen, [], "it emitted into a sink that reaches "
                                   "nobody: %r" % (seen,))

        # THE USABLE CONSUMER STILL GETS IT — the occurrence was never spent.
        landed = []
        got = seats.deliver_any(session=self.SID, seat=self.SEAT,
                                emit=landed.append, sink_usable=True)
        self.assertIsNotNone(
            got, "the addressed row was consumed by the dead-sink pass and "
                 "the live consumer woke for nothing — this is the silent "
                 "lost wake the fence exists to stop")
        self.assertIn("this must survive a dead consumer", got)
        self.assertEqual(len(landed), 1, landed)

    def _destination(self, dest):
        """`destination_usable` against a REAL fd, with `sys.stdout` bound to
        it -- which is the object the resolver actually consults."""
        from helm import seats_join
        with mock.patch.object(sys, "stdout", dest):
            return seats_join.destination_usable(print)

    def test_the_DESTINATION_TABLE_is_measured_on_REAL_fds(self):  # noqa: VACUOUS_ASSERTION — the two assertIs(False, ...) rows ARE the unconditional positive control: they prove the classifier refuses a real fd, on the same observable, so the None rows cannot pass by refusing nothing
        """EVERY ROW OF THE TABLE, AGAINST A KERNEL SINK THAT REALLY EXISTS.

        The arm this replaces planted an EMPTY proc root, so `sink_probe`
        answered UNKNOWN for everything and the whole table read "usable" --
        false-green on all four rows at once, including the override it was
        written to protect. A fixture that cannot reach the thing it measures
        agrees with any implementation."""
        from helm import beacons
        prior = os.environ.pop(beacons.FILE_SINK_OK, None)
        if prior is not None:
            self.addCleanup(os.environ.__setitem__, beacons.FILE_SINK_OK, prior)
        path = os.path.join(self.tmp, "capture.log")

        with open(path, "w") as fh:
            self.assertIs(False, self._destination(fh),
                          "a regular file reaches no reader and nobody "
                          "declared otherwise")
        with open(os.devnull, "w") as null:
            self.assertIs(False, self._destination(null))
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        with os.fdopen(w, "w") as pipe:
            self.assertIsNone(self._destination(pipe),
                              "a pipe is not refutable, so it is UNKNOWN")
        self.assertIsNone(self._destination(io.StringIO()),
                          "an in-process collector is not a kernel sink")
        # AND THE RESOLVER REFUSES TO GUESS for an emitter it cannot place.
        self.assertIsNone(self._destination_of_unknown_callable(),
                          "an unresolvable emitter must stay UNKNOWN")

    def _destination_of_unknown_callable(self):
        from helm import seats_join
        with open(os.devnull, "w") as null, mock.patch.object(sys, "stdout", null):
            return seats_join.destination_usable(lambda _line: None)

    def test_a_DECLARED_file_capture_is_delivered_to_not_withheld(self):  # noqa: VACUOUS_ASSERTION — the /dev/null assertion is the unconditional positive control on the same call -- it fires whether or not the override is honoured, so a classifier that returned None for everything fails here
        """THE OVERRIDE IS PART OF THE CLASSIFICATION, NOT A GATE BESIDE IT.

        An operator who sets the variable has declared that a regular file IS
        the destination they want, and the ARM-TIME check already honours it
        and lets the beacon start. A delivery fence that then refuses every
        row leaves them a waiter running forever against rows that stay
        PENDING -- worse than the refusal the override exists to lift."""
        from helm import beacons
        os.environ[beacons.FILE_SINK_OK] = "1"
        self.addCleanup(os.environ.pop, beacons.FILE_SINK_OK, None)
        path = os.path.join(self.tmp, "declared.log")
        with open(path, "w") as fh:
            self.assertIsNone(self._destination(fh),
                              "the operator declared this capture usable")
        # THE CONTROL, on the same fd and the same pass: /dev/null is
        # non-waking by construction and the override must NOT excuse it.
        with open(os.devnull, "w") as null:
            self.assertIs(False, self._destination(null),
                          "nobody captures to /dev/null in order to read it")

    def test_a_WITHHELD_pass_does_not_spend_the_SHARED_room_rotation(self):  # noqa: VACUOUS_ASSERTION — the MUST-HIT above proves a delivering pass really moves the ring file, and the paired pole proves the usable peer still gets the row -- both on the observables this asserts are unchanged
        """THE STARVATION FINDING. `_fair_room_slice` advances a PERSISTED
        ring at SCAN time, before anyone knows whether a row was delivered --
        so a pass that was going to withhold every row still moved the
        frontier, and a dead consumer alternating with a usable one handed
        the other the half it had just left, forever."""
        from helm import seats_common, seats_roomscan
        # ENOUGH ROOMS TO REACH THE BOUNDED RING AT ALL. Below the cap every
        # room is scanned directly and the persisted ring is never written, so
        # a fixture with a handful of rooms compares "absent" to "absent" and
        # passes against the bug -- measured, this arm was green without the
        # fence until the estate got bigger than the cap.
        for i in range(seats_common.ROOM_SCAN_CAP + 4):
            chat.post("filler", who="alice", room="r%02d" % i)
        seats.join(session=self.SID, seat=self.SEAT, cwd=self.tmp)
        ring = seats_roomscan.scan_path(self.SEAT, self.SID, "deliver")
        # MUST-HIT, TAKEN BEFORE THE ADDRESSED ROW EXISTS so this probe cannot
        # consume the row the arm is about: a pass that CAN deliver really
        # does move this file, or "unchanged" below is a statement about a
        # file nothing ever writes.
        empty = os.path.exists(ring) and open(ring).read()
        seats.deliver_any(session=self.SID, seat=self.SEAT,
                          emit=[].append, sink_usable=True)
        self.assertNotEqual(empty, os.path.exists(ring) and open(ring).read(),
                            "MUST-HIT: the shared rotation is not being "
                            "persisted at all, so this arm measures nothing")
        self.addressed("this must not be rotated past")
        before = open(ring).read()

        self.assertIsNone(
            seats.deliver_any(session=self.SID, seat=self.SEAT,
                              emit=[].append, sink_usable=False))
        after = os.path.exists(ring) and open(ring).read()
        self.assertEqual(before, after,
                         "a consumer that cannot deliver spent the shared "
                         "rotation budget its usable peer needs")

        # THE PAIRED POLE: a pass that CAN deliver still gets the row, so the
        # fence above is not simply a delivery that stopped working.
        landed = []
        got = seats.deliver_any(session=self.SID, seat=self.SEAT,
                                emit=landed.append, sink_usable=True)
        self.assertIsNotNone(got, "the usable peer was starved too")
        self.assertIn("this must not be rotated past", got)

    def test_an_UNKNOWN_sink_consumes_exactly_as_before(self):
        """THE PAIRED POLE, and without it the arm above is satisfied by a
        delivery path that withholds from everyone. UNKNOWN is the default
        every existing caller passes, so this is also the no-behaviour-change
        proof for the rest of the tree."""
        self.addressed("ordinary delivery")
        seen = []
        got = seats.deliver_any(session=self.SID, seat=self.SEAT,
                                emit=seen.append)          # sink_usable absent
        self.assertIsNotNone(got, "the default path stopped delivering")
        self.assertIn("ordinary delivery", got)
        self.assertEqual(len(seen), 1, seen)

        # AND IT REALLY CONSUMED: the same call finds nothing left.
        self.assertIsNone(
            seats.deliver_any(session=self.SID, seat=self.SEAT,
                              emit=seen.append),
            "the row was not consumed, so the arm above cannot distinguish "
            "a committed delivery from a withheld one")

    def test_a_FOLLOWER_on_dev_null_measures_itself_and_withholds(self):
        """THE PLUMBING MUST BE REACHED IN PRODUCTION, or the fence is dead
        code that every arm above exercises by hand. This one drives the real
        measurement site: `seats_join.wait(follow=True)` with no injected emit,
        so `stream` really is `_emit_line` writing THIS process's fd 1 — and
        that fd is redirected to /dev/null, which is exactly the shape the
        classifier proves reaches no reader.

        The follower must therefore leave the row, and the assertion is that a
        usable consumer afterwards still finds it. Asserting only that the
        follower printed nothing would be vacuous: printing to /dev/null looks
        identical either way, which is the whole difficulty of this defect."""
        from helm import seats_join
        self.addressed("a follower on the discard must not eat this")

        saved = os.dup(1)
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, 1)
            # MUST-HIT: with fd 1 really on the discard, the measurement the
            # production path takes must say REFUTES — otherwise this arm is
            # running against an ordinary sink and proves nothing.
            state, _tok = beacons.sink_probe(os.getpid())
            measured = state
            seats_join.wait(seat=self.SEAT, session=self.SID, follow=True,
                            timeout=0.25, poll=0.05)
        finally:
            os.dup2(saved, 1)
            os.close(saved)
            os.close(null)

        self.assertEqual(measured, beacons.SINK_REFUTES,
                         "MUST-HIT: fd 1 on /dev/null did not classify as "
                         "REFUTES, so this arm never exercised the fence")
        landed = []
        got = seats.deliver_any(session=self.SID, seat=self.SEAT,
                                emit=landed.append, sink_usable=True)
        self.assertIsNotNone(
            got, "the /dev/null follower consumed the addressed row through "
                 "the real wait() path — the fence is not wired into "
                 "production, only into the hand-driven arms above")
        self.assertIn("a follower on the discard must not eat this", got)

    def _wait_while_fd1_flips(self, first, second, then_address=None):
        """Run the real follower with fd 1 on `first`, flip it to `second`
        part-way through the wait from another thread, and optionally plant an
        addressed row AFTER the flip. Returns nothing; the caller reads the
        observables. fd 1 is restored whatever happens."""
        import threading
        from helm import seats_join
        saved = os.dup(1)
        flipped = threading.Event()

        def flip():
            os.dup2(second, 1)
            if then_address:
                self.addressed(then_address)
            flipped.set()
        t = threading.Timer(0.2, flip)
        try:
            os.dup2(first, 1)
            t.start()
            seats_join.wait(seat=self.SEAT, session=self.SID, follow=True,
                            timeout=0.7, poll=0.05)
        finally:
            t.cancel()
            os.dup2(saved, 1)
            os.close(saved)
        self.assertTrue(flipped.is_set(),
                        "MUST-HIT: the flip never ran, so the wait was never "
                        "observed across a change of destination")

    def test_a_sink_that_goes_DEAF_mid_wait_stops_consuming(self):
        """THE READING IS TAKEN BESIDE EACH DELIVERY, NOT ONCE BEFORE THE WAIT.
        A follower waits without bound and fd 1 can change under it. Sampled
        once at a usable pipe, it would keep consuming rows into that fd after
        the harness moved it to the discard -- the silent lost wake, reached
        through the fence built to stop it.

        The positive control is the FIRST row: it is consumed while fd 1 is
        the pipe and its bytes are read back from the pipe, which proves the
        follower was delivering. The second row is planted after fd 1 became
        /dev/null and must still be waiting for a usable consumer."""
        self.addressed("delivered while the pipe was live")
        rfd, wfd = os.pipe()
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            self._wait_while_fd1_flips(
                wfd, null, then_address="planted after fd 1 went deaf")
            os.close(wfd)
            piped = os.read(rfd, 65536).decode("utf-8", "replace")
        finally:
            os.close(rfd)
            os.close(null)
        self.assertIn("delivered while the pipe was live", piped,
                      "MUST-HIT: the follower delivered nothing while fd 1 was "
                      "a live pipe, so a withhold below proves nothing")
        self.assertNotIn("planted after fd 1 went deaf", piped)
        got = seats.deliver_any(session=self.SID, seat=self.SEAT,
                                emit=[].append, sink_usable=True)
        self.assertIsNotNone(
            got, "the row planted after fd 1 became the discard was consumed "
                 "-- the follower is still using the reading it took before "
                 "the wait")
        self.assertIn("planted after fd 1 went deaf", got)

    def test_a_sink_that_becomes_USABLE_mid_wait_resumes_delivering(self):
        """THE OTHER DIRECTION OF THE SAME ONE-TIME SAMPLE: a follower that
        started on the discard withholds, correctly -- and sampled once, it
        would withhold FOREVER after fd 1 became a live pipe, starving a seat
        that can now be woken. The row is planted before the wait so the only
        thing that changes between withhold and delivery is the destination."""
        self.addressed("waiting for the pipe to arrive")
        rfd, wfd = os.pipe()
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            self._wait_while_fd1_flips(null, wfd)
            os.close(wfd)
            piped = os.read(rfd, 65536).decode("utf-8", "replace")
        finally:
            os.close(rfd)
            os.close(null)
        self.assertIn("waiting for the pipe to arrive", piped,
                      "fd 1 became a live pipe during the wait and the row was "
                      "never delivered into it -- the follower is still using "
                      "the deaf reading it took before the wait")

    def test_a_REDIRECTED_stdout_is_classified_not_the_function_name(self):
        """KNOWING WHICH FUNCTION RUNS IS NOT KNOWING WHERE ITS BYTES GO, and
        this arm drives both directions of that because one check keyed on the
        function identity was measured wrong in each of them.

        `_emit_line` calls print(), so it writes to whatever `sys.stdout` is
        BOUND TO at that moment. fd 1 is the usual answer and not a law.

        DIRECTION ONE — fd 1 is a usable PIPE and stdout is redirected to the
        discard. Keying on `stream is _emit_line` plus a stat of fd 1 reads
        USABLE, so the follower commits the row and throws it away: the silent
        lost wake, reached through the very fence built to stop it.

        DIRECTION TWO — fd 1 is /dev/null and stdout is redirected to a live
        in-process collector. The same check reads PROVEN-UNUSABLE and
        withholds from a consumer that would have delivered, which is worse
        than the parent behaviour rather than merely unhelpful."""
        from helm import seats_join

        # DIRECTION ONE. fd 1 is made a REAL PIPE rather than inherited,
        # because under a test runner it may already be a file — and then the
        # refuted check would withhold for the right reason by accident and
        # this direction would pass while proving nothing.
        self.addressed("a redirected discard must not eat this")
        rfd, wfd = os.pipe()
        saved1 = os.dup(1)
        null = open(os.devnull, "w")
        try:
            os.dup2(wfd, 1)
            # MUST-HIT: fd 1 really is an ADMISSIBLE pipe, so a withhold below
            # can only come from classifying the redirected stdout.
            pipe_state, _t = beacons.sink_probe(os.getpid(), fd=1)
            with contextlib.redirect_stdout(null):
                seats_join.wait(seat=self.SEAT, session=self.SID, follow=True,
                                timeout=0.25, poll=0.05)
        finally:
            os.dup2(saved1, 1)
            os.close(saved1)
            os.close(rfd)
            os.close(wfd)
            null.close()
        self.assertEqual(pipe_state, beacons.SINK_ADMISSIBLE,
                         "MUST-HIT: fd 1 was not a usable pipe, so direction "
                         "one is not exercising the false-usable case")
        landed = []
        got = seats.deliver_any(session=self.SID, seat=self.SEAT,
                                emit=landed.append, sink_usable=True)
        self.assertIsNotNone(
            got, "stdout was redirected to /dev/null and the follower still "
                 "consumed the row — the fence classified fd 1, which is not "
                 "where print() was writing")

        # DIRECTION TWO. fd 1 IS the discard, and stdout is a live collector,
        # so this follower can genuinely deliver and must not be withheld from.
        self.addressed("a live collector must receive this")
        collector = io.StringIO()
        saved = os.dup(1)
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 1)
            # MUST-HIT: fd 1 really is the discard, or direction two is not
            # being exercised at all and the assertion below is free.
            state, _tok = beacons.sink_probe(os.getpid(), fd=1)
            with contextlib.redirect_stdout(collector):
                seats_join.wait(seat=self.SEAT, session=self.SID, follow=True,
                                timeout=0.25, poll=0.05)
        finally:
            os.dup2(saved, 1)
            os.close(saved)
            os.close(devnull)
        self.assertEqual(state, beacons.SINK_REFUTES,
                         "MUST-HIT: fd 1 was not the discard, so this "
                         "direction proves nothing")
        self.assertIn(
            "a live collector must receive this", collector.getvalue(),
            "the follower withheld from a usable in-process collector because "
            "fd 1 happened to be /dev/null — worse than not having the fence")

    def test_the_fence_signals_no_other_process_to_do_its_job(self):
        """DO NOT WEAKEN reap=False AUTHORITY TO GET THERE, says the row. The
        fence reads THIS process's own deliverability and withholds its OWN
        commit; it must never reach for a signal, which is the only thing
        reap=False forbids. Asserted by spying the signal doors rather than by
        reading the code."""
        self.addressed("no signal is owed for this")
        killed = []
        with mock.patch.object(os, "kill",
                               lambda *a, **k: killed.append(a)):
            with mock.patch.object(beacons, "stop_superseded",
                                   lambda *a, **k: killed.append(("stop",))):
                self.assertIsNone(seats.deliver_any(
                    session=self.SID, seat=self.SEAT, emit=[].append,
                    sink_usable=False))
        self.assertEqual(killed, [],
                         "withholding a commit reached for authority over "
                         "another process: %r" % (killed,))
