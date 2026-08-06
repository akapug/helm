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
     as an ordinary clean allow. Found by codex-2 attacking the enumeration
     rather than the prose, which is what made it the class and not a case.
     Closed by making the failure LOUD; the stop still passes.
  6. `ROOM_SCAN_CAP` bounding a pass to 16 rooms — DELIBERATE, and the honest
     statement is that coverage is EVENTUAL, not immediate: `_fair_room_slice`
     rotates the unscanned remainder so every room comes up, but at any SINGLE
     stop a row in an unscanned room does not block. A cap that skips work may
     never produce a confident verdict about the work it skipped.
     TWO CORRECTIONS codex-2 measured against this paragraph, both of which
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
     estate is empty.

  9. An unreadable room TAIL reading as inbox-clean — A BUG, found by
     @codex-2 on round three and closed here. `_tail` answered an unreadable
     room with the SAME None it uses for "genuinely nothing new", so a room
     helm could not READ was indistinguishable from an empty one. This is
     escape 4's sentence one layer DOWN: escape 4 fixed the cheap STAT
     precheck, and this is the READ behind it — so the precheck could
     correctly answer DIRTY and the reader still answer empty, and the stop
     read inbox-clean anyway. Only FileNotFoundError is emptiness; every
     other OSError now says the rows are UNKNOWN, not absent, and the pass
     still proceeds.
  8. The GATE HOOK swallowing `timeout`'s rc 124 in silence — A BUG, found by
     codex-2 attacking the enumeration and closed here. The generated Stop
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

A TENTH path outside this set is a finding against the enumeration, which is
the bar this file exists to set — the same bar codex held the runtime-family
class to before it closed by construction. Three rounds have now moved that
bar (4 -> 6 -> 7 -> 8 -> 9), and rounds two and three each moved it again
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
from helm import seats  # noqa: E402

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
        """codex-2's repro: the caller swallowed everything and returned 0, so
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
        in the whole suite without it (@kimi's gate 64f68aab1cf3c570, 6273 run
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
        spec = self._gate_spec()
        cmd = hooks.spec_command(spec)
        # Substitute the real invocation with a stub whose rc we control; the
        # rc-handling tail — the thing under test — is left untouched.
        head = "timeout %d " % spec["timeout"]
        self.assertTrue(cmd.startswith(head), cmd)   # control: shape as assumed
        tail = cmd[len(head):]
        tail = tail[tail.index(";"):]                # everything from `; rc=$?`
        p = subprocess.run(["bash", "-c", "timeout %d %s%s"
                            % (timeout_s, body, tail)],
                           capture_output=True, text=True)
        return p.returncode, p.stderr

    def test_a_timed_out_guard_says_the_stop_was_unchecked(self):
        rc, err = self._run("sleep 30")
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

    @codex-2's review: "cap/clamp/RR-warning mutations all survive". Verified
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
        as 16 scanned 19 — @codex-2 measured it. The budget must subtract the
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
        with mock.patch.object(seats, "_flocked",
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
        """THE NINTH ESCAPE, @codex-2. `_tail` answered an unreadable room
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
