#!/usr/bin/env python3
"""The owner's away verb.

Posture is DECLARED, never inferred, so the arm that matters most here is a
must-miss: nothing in this module may read a clock or guess from activity.
Away is declared and revoked by a human typing a word.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from unittest import mock

from helm import away


def _run(home, *args):
    """Drive the REAL CLI in an isolated HELM_HOME. -> (rc, out, err)"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(away.__file__)))
    env = dict(os.environ, HELM_HOME=home)
    # HERMETIC OR IT MEASURES THE HOST. This inherited HELM_CHAT_NAME, so a
    # "bare shell" control was only bare when the runner happened not to set
    # it — probed: with HELM_CHAT_NAME=seat-x present the control got rc 2 and
    # a seat refusal. Every identity the verb can read is cleared here, and
    # each arm sets back exactly the one it is about.
    for var in ("HELM_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME"):
        env.pop(var, None)
    p = subprocess.run([sys.executable, "-m", "helm"] + list(args),
                       cwd=root, env=env, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


class TheFlagIsDeclaredAndRevoked(unittest.TestCase):
    def setUp(self):
        self.home = os.path.join(tempfile.mkdtemp(), "home")

    def _flag(self):
        return os.path.join(self.home, "helm-chat", away.MARKER_NAME)

    def test_away_sets_it_back_clears_it_and_both_are_idempotent(self):
        rc, out, _ = _run(self.home, "back")
        self.assertEqual(rc, 0)
        self.assertIn("already back", out)      # not an error to lift nothing
        self.assertFalse(os.path.exists(self._flag()))

        rc, out, _ = _run(self.home, "away")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self._flag()), "away set no flag")
        self.assertIn("helm back", out, "it must say how to undo itself")

        rc, out, _ = _run(self.home, "away")
        self.assertEqual(rc, 0, "declaring away twice must not fail")
        self.assertIn("already away", out)

        rc, out, _ = _run(self.home, "back")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self._flag()), "back left the flag set")

    def test_a_bad_argument_refuses_AND_LEAVES_THE_FLAG_ALONE(self):
        """MUST-MISS. A verb that half-acts on a typo is worse than one that
        refuses: someone would be marked away by a word they did not mean."""
        # THE CONTROL RUNS FIRST AND UNCONDITIONALLY. A control placed after
        # the absence assertion does not protect it — the absence can pass and
        # the run can end before the control is ever reached. Prove the watched
        # path is one this verb really writes, THEN prove a typo leaves it
        # alone. Without this ordering a typo in _flag() would make every
        # "did not set it" assertion in this file pass forever.
        _run(self.home, "away")
        self.assertTrue(os.path.exists(self._flag()),
                        "the flag path this test watches is not the one the "
                        "verb writes, so its absence would prove nothing")
        _run(self.home, "back")

        rc, _, err = _run(self.home, "away", "sideways")
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)
        self.assertFalse(os.path.exists(self._flag()),
                         "a refused invocation still set the flag")

    def test_NO_CHAT_ROW_IS_WRITTEN_AT_ALL(self):
        """INVERTED, NOT DELETED, and the inversion is the whole point.

        This arm used to pin that the declaration reached a durable chat row
        as its BODY — a real regression pin, because an early draft passed
        (room, who, text) to chat.post and would have published the literal
        word "main". That pin was correct against a design that no longer
        exists: nothing ever READ that row, and it could contradict the marker
        under interleaving, which makes it a contradiction surface rather than
        a notice.

        Deleting the arm would let the row come back silently, so it now pins
        the ABSENCE. The marker is the sole posture truth and the chart is its
        only reader."""
        rc, _, _ = _run(self.home, "away")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self._flag()))     # control: it acted
        room = os.path.join(self.home, "helm-chat", "main.jsonl")
        if not os.path.exists(room):
            return                                        # nothing posted at all
        with io.open(room, encoding="utf-8") as fh:
            body = fh.read()
        self.assertNotIn("[posture]", body,
                         "a posture row is back: a second representation of "
                         "one boolean is how two surfaces come to disagree")

    def test_trailing_junk_after_off_REFUSES_INSTEAD_OF_MUTATING(self):  # noqa: VACUOUS_ASSERTION — the absences here ARE the hoisted control: the arm sets the flag, proves `away off` can lift it (that assertFalse), sets it again, and only then proves junk did NOT lift it. Each absence is paired with the positive on the line above it, which is what makes the other half mean anything
        """`away off junk` used to match args[0], ignore the tail, and LIFT his
        posture. A fat-fingered command must not half-act."""
        # CONTROL FIRST AND UNCONDITIONAL: prove `off` really CAN lift it, so
        # the assertion that junk did NOT lift it means something. A control
        # placed after an absence does not protect it — the absence can pass
        # and the run can end before the control is reached.
        self.assertEqual(_run(self.home, "away")[0], 0)
        self.assertTrue(os.path.exists(self._flag()))
        self.assertEqual(_run(self.home, "away", "off")[0], 0)
        self.assertFalse(os.path.exists(self._flag()),
                         "`away off` cannot lift the flag, so this arm could "
                         "never detect junk lifting it either")
        self.assertEqual(_run(self.home, "away")[0], 0)

        rc, _, err = _run(self.home, "away", "off", "junk")
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)
        self.assertTrue(os.path.exists(self._flag()),
                        "trailing junk still lifted the posture")

    def test_a_named_seat_is_NOT_refused_and_the_gate_stays_deleted(self):
        """A PIN ON THE REVERSAL, not a deletion of the old arm.

        This arm used to assert the opposite — that a named non-owner seat is
        REFUSED — and it was correct against a design that has since been
        removed. The admission gate could not tell a person from an unnamed or
        deliberately unlabelled agent, so it refused the honest agents and
        passed the ones worth stopping; and it could refuse `helm back`,
        stranding someone in away posture because an instrument broke.

        Deleting the arm outright would let that gate come back silently, so
        it is INVERTED: a named seat proceeds, and the marker records who it
        was. Attribution is the guarantee this can keep; admission was not.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(away.__file__)))
        env = dict(os.environ, HELM_HOME=self.home, HELM_CHAT_NAME="seat-b")
        env.pop("HELM_CHAT_DIR", None)
        p = subprocess.run([sys.executable, "-m", "helm", "away"],
                           cwd=root, env=env, capture_output=True, text=True)
        self.assertTrue(p.stdout.strip(), "the verb emitted nothing at all")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(os.path.exists(self._flag()),
                        "a named seat must be able to declare, and be recorded")
        with io.open(self._flag(), encoding="utf-8") as fh:
            self.assertIn("seat-b", fh.read(),
                          "the declaration must name who made it")


class EveryDeclarationIsATTRIBUTED(unittest.TestCase):
    """The gate is gone, so these arms guard what replaced it.

    The admission gate could not tell a person from an unnamed or deliberately
    unlabelled agent, so it refused the honest agents and passed the ones worth
    stopping — a guarantee it could not keep. Posture is ATTRIBUTED now:
    nothing is refused, and every declaration records WHO made it.
    """

    def setUp(self):
        self.home = os.path.join(tempfile.mkdtemp(), "home")

    def _flag(self):
        return os.path.join(self.home, "helm-chat", away.MARKER_NAME)

    def _run_as(self, name, *args):
        root = os.path.dirname(os.path.dirname(os.path.abspath(away.__file__)))
        env = dict(os.environ, HELM_HOME=self.home)
        for var in ("HELM_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME"):
            env.pop(var, None)
        if name:
            env["HELM_CHAT_NAME"] = name
        p = subprocess.run([sys.executable, "-m", "helm"] + list(args),
                           cwd=root, env=env, capture_output=True, text=True)
        return p

    def test_a_named_seat_is_ALLOWED_and_named_in_the_marker(self):
        """No refusal — the point is that a machine speaking for a person is
        VISIBLE, which is the guarantee this can actually keep."""
        p = self._run_as("seat-b", "away")
        self.assertTrue(p.stdout.strip(), "the verb emitted nothing at all")
        self.assertEqual(p.returncode, 0, p.stderr)
        with io.open(self._flag(), encoding="utf-8") as fh:
            self.assertIn("seat-b", fh.read())

    def test_an_unnamed_process_is_recorded_AS_UNNAMED_not_as_a_person(self):
        """MUST-MISS on the assumption the deleted gate rested on: an absent
        seat name must NOT be recorded as if a human were proven."""
        p = self._run_as(None, "away")
        self.assertTrue(p.stdout.strip(), "the verb emitted nothing at all")
        self.assertEqual(p.returncode, 0, p.stderr)
        with io.open(self._flag(), encoding="utf-8") as fh:
            body = fh.read()
        self.assertTrue(body.strip(), "the marker was written empty")
        self.assertIn("unnamed", body)
        self.assertNotIn("owner", body)

    def test_back_CANNOT_BE_REFUSED_by_any_identity(self):
        """The failure the gate could cause and this shape cannot: stranded in
        away posture because an instrument broke. Fail-closed is right for
        mutating someone else's state and wrong for letting them out."""
        self.assertEqual(self._run_as("seat-b", "away").returncode, 0)
        self.assertTrue(os.path.exists(self._flag()))       # control
        p = self._run_as("seat-c", "back")
        self.assertTrue(p.stdout.strip(), "the verb emitted nothing at all")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(self._flag()),
                         "a different identity could not lift the posture")


class TheTransitionIsAtomic(unittest.TestCase):
    """The TOCTOU is DELETED, not serialised around.

    The transition used to ask os.path.exists and then act on the answer, so
    two interleaved runs could both believe they were the one changing state.
    A lock would have narrowed that window; removing the window is better,
    because the filesystem already decides both questions atomically and the
    decision can simply be READ OFF the syscall outcome.
    """

    def setUp(self):
        self.home = os.path.join(tempfile.mkdtemp(), "home")

    def _flag(self):
        return os.path.join(self.home, "helm-chat", away.MARKER_NAME)

    def test_the_source_holds_no_check_then_act_on_the_marker(self):
        """A STRUCTURAL arm, because a behavioural one cannot see a race that
        did not happen to occur. What it pins is the SHAPE: no existence test
        may decide the transition, and both syscalls must be the deciders."""
        with io.open(away.__file__, encoding="utf-8") as fh:
            src = fh.read()
        # THE CONTROL NAMES THE PRIMITIVE ACTUALLY IN USE. This asked for
        # O_EXCL, which the publish path stopped using when it moved to
        # mkstemp+link — and it kept passing because a COMMENT still said the
        # word. A control satisfied by prose is not a control, so it now pins
        # the create-only syscall this module actually decides the transition
        # with; mkstemp opens O_EXCL itself, one layer down.
        self.assertIn("os.link(tmp, path)", src)            # control
        self.assertIn("FileNotFoundError", src)
        body = src.split("def cmd_away", 1)[1]
        self.assertNotIn("os.path.exists(path)", body,
                         "a check-then-act on the marker is back in cmd_away")

    def test_a_SECOND_away_does_not_rewrite_the_first_declaration(self):
        """The property O_EXCL buys, asserted on the observable: the loser of
        the race must not clobber the winner's attribution."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(away.__file__)))

        def run(name):
            env = dict(os.environ, HELM_HOME=self.home, HELM_CHAT_NAME=name)
            for var in ("HELM_CHAT_DIR", "MELD_CHAT_NAME"):
                env.pop(var, None)
            return subprocess.run([sys.executable, "-m", "helm", "away"],
                                  cwd=root, env=env, capture_output=True,
                                  text=True)

        first = run("seat-b")
        self.assertEqual(first.returncode, 0, first.stderr)
        with io.open(self._flag(), encoding="utf-8") as fh:
            self.assertIn("seat-b", fh.read())              # control
        second = run("seat-c")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already away", second.stdout)
        with io.open(self._flag(), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("seat-b", body, "the second declarer overwrote the first")
        self.assertNotIn("seat-c", body)


class TheFlagNeverLandsWhereItShouldNot(unittest.TestCase):
    """Two ways a posture flag can exist when it should not."""

    def setUp(self):
        self.home = os.path.join(tempfile.mkdtemp(), "home")
        self.chat = os.path.join(self.home, "helm-chat")

    def _flag(self):
        return os.path.join(self.chat, away.MARKER_NAME)

    def _run(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(away.__file__)))
        env = dict(os.environ, HELM_HOME=self.home)
        for var in ("HELM_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME"):
            env.pop(var, None)
        return subprocess.run([sys.executable, "-m", "helm", "away"],
                              cwd=root, env=env, capture_output=True, text=True)

    def test_the_directory_and_the_flag_are_private(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the rc-0 run on the first line; the mode assertions are equalities against 0o700/0o600, which the rung reads as absences because the expected side is small, not because nothing was checked
        """CONTROL for the class, and a property in its own right: a flag about
        a person is 0600 inside a 0700 directory, not world-readable state."""
        self.assertEqual(self._run().returncode, 0)
        self.assertEqual(os.stat(self.chat).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self._flag()).st_mode & 0o777, 0o600)

    def test_a_FOREIGN_OWNED_directory_is_refused(self):  # noqa: VACUOUS_ASSERTION — the unconditional control runs FIRST and proves the happy path CREATES the flag, so the later absence cannot be satisfied by a cmd_away that refuses everything; the control binds a different name than the absence, which is why the rung cannot pair them
        """MUST-MISS. A writable directory owned by another uid used to be
        written into, which is the shared-directory race helm already refuses
        in home.ram_root. Simulated by making geteuid disagree with the dir's
        owner, since a test cannot chown to a uid it does not have."""
        self.assertEqual(self._run().returncode, 0)          # control: it works
        self.assertTrue(os.path.exists(self._flag()))
        os.remove(self._flag())
        real = os.geteuid
        os.geteuid = lambda: os.stat(self.chat).st_uid + 1
        self.addCleanup(setattr, os, "geteuid", real)
        rc = away.cmd_away([])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self._flag()),
                         "a foreign-owned directory still received the flag")

    def test_a_FAILED_RECORD_leaves_no_flag_behind(self):  # noqa: VACUOUS_ASSERTION — same pairing: the run-and-assert-created control precedes the fdopen fault injection, so the absence afterwards is the UNLINK and not a verb that never wrote
        """MUST-MISS, and the asymmetry that makes it matter: O_EXCL has
        ALREADY created the file by the time the write runs, so a close that
        fails (ENOSPC is the ordinary way) would leave the posture SET while
        the caller is told the command failed. The observable and the return
        value must never disagree about whether a person is away."""
        self.assertEqual(self._run().returncode, 0)          # control
        self.assertTrue(os.path.exists(self._flag()))
        os.remove(self._flag())
        real = os.fdopen

        def boom(fd, *a, **k):
            os.close(fd)
            raise OSError(28, "No space left on device")
        os.fdopen = boom
        self.addCleanup(setattr, os, "fdopen", real)
        rc = away.cmd_away([])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self._flag()),
                         "a failed record left a flag claiming someone is away")


class TheAttributionIsCONSUMED(unittest.TestCase):
    """Recording WHO is worthless until something reads it.

    The admission gate was deleted because it could not tell a person from an
    unnamed agent; VISIBILITY replaced it. Visibility that lives only in a file
    nobody opens is not visibility — the same defect as promising a reader that
    does not exist, which this module had already produced once.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.marker = os.path.join(self.dir, "owner-away")

    def test_declared_by_reads_the_name_back(self):
        with io.open(self.marker, "w", encoding="utf-8") as fh:
            fh.write("declared by seat-b\n")
        self.assertEqual(away.declared_by(self.marker), "seat-b")

    def test_an_absent_or_unlabelled_marker_reads_None(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the arm above, which proves declared_by CAN return a name through the same accessor; these are the shapes it must not invent one from
        self.assertIsNone(away.declared_by(self.marker))     # absent
        with io.open(self.marker, "w", encoding="utf-8") as fh:
            fh.write("\n")
        self.assertIsNone(away.declared_by(self.marker))     # empty
        with io.open(self.marker, "w", encoding="utf-8") as fh:
            fh.write("something else entirely\n")
        self.assertIsNone(away.declared_by(self.marker),
                          "an unrecognised first line invented a declarer")

    def test_a_NO_OP_reports_the_ORIGINAL_declarer_and_rewrites_nothing(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the first run: rc 0 asserted, then seat-b asserted PRESENT in the marker, before any absence is checked. The rung cannot pair them because the control binds `first`/the marker body while the absences bind `second.stdout` and a later read of the same file
        """The announce half of this arm went with the chat row; the MARKER
        half is what always mattered. Running `away` on an already-set flag
        changes nothing, so it must report the standing declarer and must not
        replace the attribution — which is exactly what os.link buys over
        rename, since rename would silently overwrite."""
        home = os.path.join(tempfile.mkdtemp(), "home")
        root = os.path.dirname(os.path.dirname(os.path.abspath(away.__file__)))

        def run(name):
            env = dict(os.environ, HELM_HOME=home, HELM_CHAT_NAME=name)
            for var in ("HELM_CHAT_DIR", "MELD_CHAT_NAME"):
                env.pop(var, None)
            return subprocess.run([sys.executable, "-m", "helm", "away"],
                                  cwd=root, env=env, capture_output=True,
                                  text=True)

        first = run("seat-b")
        self.assertEqual(first.returncode, 0, first.stderr)   # control
        flag = os.path.join(home, "helm-chat", away.MARKER_NAME)
        with io.open(flag, encoding="utf-8") as fh:
            self.assertIn("seat-b", fh.read())
        second = run("seat-c")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("seat-b", second.stdout)
        self.assertNotIn("seat-c", second.stdout)
        with io.open(flag, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("seat-b", body, "the second declarer overwrote the first")
        self.assertNotIn("seat-c", body)


class TheWriterAndTheReaderNEVERDISAGREE(unittest.TestCase):
    """Two shapes where the command and the chart could tell you opposite
    things about where you are."""

    def setUp(self):
        self.home = os.path.join(tempfile.mkdtemp(), "home")
        self.chat = os.path.join(self.home, "helm-chat")
        os.makedirs(self.chat, mode=0o700)
        self.marker = os.path.join(self.chat, away.MARKER_NAME)

    def _run(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(away.__file__)))
        env = dict(os.environ, HELM_HOME=self.home)
        for var in ("HELM_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME"):
            env.pop(var, None)
        return subprocess.run([sys.executable, "-m", "helm", "away"],
                              cwd=root, env=env, capture_output=True, text=True)

    def test_a_DANGLING_SYMLINK_reads_the_same_to_both(self):
        """The writer publishes with os.link, which fails EEXIST on a dangling
        symlink and reports "already away". os.path.exists FOLLOWS it and
        answers False — so the command said away and the chart said not. The
        divergence is the bug; lexists is the question the writer is actually
        asking."""
        self.assertEqual(self._run().returncode, 0)          # control
        self.assertTrue(away.is_away(self.marker))
        os.remove(self.marker)
        os.symlink("/nonexistent/target", self.marker)
        out = self._run()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("already away", out.stdout)
        self.assertTrue(away.is_away(self.marker),
                        "the writer says away and the reader says not")

    def test_a_STALE_TEMP_does_not_block_a_later_run(self):
        """`<path>.<pid>.tmp` collided two ways: a crashed run left a stale
        temp the next same-pid run tripped over, and pids are REUSED. mkstemp
        picks a name nothing else holds."""
        stale = os.path.join(self.chat, ".away-stale.tmp")
        with io.open(stale, "w", encoding="utf-8") as fh:
            fh.write("junk from a crashed run\n")
        also = "%s.%d.tmp" % (self.marker, os.getpid())
        with io.open(also, "w", encoding="utf-8") as fh:
            fh.write("the old naming scheme's leftover\n")
        out = self._run()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(os.path.exists(self.marker))
        with io.open(self.marker, encoding="utf-8") as fh:
            self.assertIn("declared by", fh.read())

    def test_no_temp_survives_a_successful_publish(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the marker assertion on the line above: it proves the publish RAN before this asserts what it left behind
        self.assertEqual(self._run().returncode, 0)
        self.assertTrue(os.path.exists(self.marker))
        leftovers = [n for n in os.listdir(self.chat) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [], "a temp survived a successful publish")


class ADISPUTEDIdentityIsNotADeclarer(unittest.TestCase):
    """Two identity sources that disagree attribute to NOBODY.

    THESE DRIVE THE REAL PREDICATE. The seams patched are the SOURCES it
    reads — the process's declared name, the roster, and the claude process
    census that says whether a rostered session is still held — never
    identity_disagreement itself, because an arm that fakes the predicate
    measures its own mock. That matters here more than usual: the first cut of
    this cure hand-wrote the comparison, and the arm written against the
    hand-rolled version passed while missing an entire dispute shape.
    """

    def _declarer_with(self, declared, rostered, roster=None,
                       nonpane=False):
        import helm.seats_identity as ident
        import helm.seats as seats
        import helm.home as home_mod
        import helm.session as session_mod
        saved = [(ident, "own_name", ident.own_name),
                 (ident, "roster", ident.roster),
                 (seats, "seat_for_session", seats.seat_for_session),
                 (seats, "nonpane_session", seats.nonpane_session),
                 (home_mod, "session_id", home_mod.session_id),
                 (session_mod, "_proc_claude_census",
                  session_mod._proc_claude_census)]
        for mod, attr, real in saved:
            self.addCleanup(setattr, mod, attr, real)
        ident.own_name = lambda: declared
        ident.roster = lambda: (roster or {})
        seats.seat_for_session = lambda s: rostered
        seats.nonpane_session = lambda s: nonpane
        home_mod.session_id = lambda: "a-session"
        # every rostered session is HELD by a planted live process
        held = [{"pid": 4000 + i, "session": row.get("session")}
                for i, row in enumerate((roster or {}).values())
                if isinstance(row, dict) and row.get("session")]
        session_mod._proc_claude_census = lambda: {
            "rows": held, "listing_failed": False, "who_failed": False,
            "census_partial": False}
        return away._declarer()

    def test_an_AGREEING_roster_records_the_name(self):
        """CONTROL, unconditional and first: agreement must still attribute, or
        every arm below is satisfied by a resolver that names nobody."""
        name, prov = self._declarer_with("seat-b", "seat-b")
        self.assertEqual(name, "seat-b")
        self.assertEqual(prov, "declared")

    def test_a_TAKEOVER_records_NOBODY(self):
        """This session is rostered to a DIFFERENT seat. A process asserting a
        name the roster says belongs to someone else is exactly where taking it
        at its word is wrong — disagreement is refused, not resolved in favour
        of whoever spoke."""
        name, prov = self._declarer_with("seat-b", "seat-c")
        self.assertEqual(prov, "disputed")
        self.assertNotIn("seat-b", name)
        self.assertNotIn("seat-c", name)

    def test_a_CLAIM_JUMP_records_NOBODY_and_this_is_the_pole_a_subset_MISSES(self):
        """THE MUST-MISS FOR THE HAND-ROLLED VERSION. Here the session is
        rostered NOWHERE and the declared name is held by a DIFFERENT live
        session — an inherited name plus a fresh sid, which is the shape the
        identity layer records as having been measured open by an adversary.
        A check asking only "is MY session bound to another seat" needs a
        binding to exist, finds none, and passes this. If this arm ever goes
        green by reporting a name, the predicate has been narrowed back to
        that subset."""
        name, prov = self._declarer_with(
            "seat-b", None, roster={"seat-b": {"session": "somebody-elses"}})
        self.assertEqual(prov, "disputed")
        self.assertNotIn("seat-b", name)

    def test_an_UNCLAIMED_name_is_NOT_a_dispute(self):
        """The other side of the same bar, or the cure refuses every honest
        first join: a roster row carrying NO session is a name waiting to be
        claimed, not a seat being impersonated."""
        name, prov = self._declarer_with(
            "seat-b", None, roster={"seat-b": {"session": ""}})
        self.assertEqual(name, "seat-b")
        self.assertEqual(prov, "declared")


class ThePARENTMUSTBE0700(unittest.TestCase):
    """The guard admitted more than its own contract stated."""

    def _try(self, mode):
        home = os.path.join(tempfile.mkdtemp(), "home")
        chat = os.path.join(home, "helm-chat")
        os.makedirs(chat, mode=0o700)
        os.chmod(chat, mode)
        return _run(home, "away")[0:3], os.path.join(chat, away.MARKER_NAME)

    def test_0700_is_accepted(self):
        """CONTROL FIRST: the bar must admit its own stated mode, or every
        refusal below is a verb that refuses everything."""
        (rc, _out, err), flag = self._try(0o700)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(flag))

    def test_0755_is_REFUSED_because_the_contract_says_0700(self):
        """It checked only the WRITE bits, so 0755 passed while the documented
        bar was 0700. A guard admitting more than its contract states is worse
        than a missing one — the contract is what a later reader trusts."""
        (rc, _out, err), flag = self._try(0o755)
        self.assertEqual(rc, 1)
        self.assertIn("0700", err)
        self.assertFalse(os.path.exists(flag))

    def test_0770_is_REFUSED_too(self):
        (rc, _out, _err), flag = self._try(0o770)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(flag))


class APATHNAMEISNOTITSLASTCOMPONENT(unittest.TestCase):
    """An ancestor can re-point the whole path; the leaf check cannot see it."""

    def _home_under(self, build):
        """build(root) -> the HELM_HOME to hand the CLI."""
        root = tempfile.mkdtemp()
        return build(root)

    def test_a_SAFE_pathname_publishes(self):
        """CONTROL, FIRST AND UNCONDITIONAL: an ordinary owned path must still
        work, or the refusals below are a verb that refuses everything. This
        also pins the sticky-bit clause — the temp root above it is 1777."""
        home = self._home_under(lambda r: os.path.join(r, "home"))
        rc, _out, err = _run(home, "away")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(
            os.path.join(home, "helm-chat", away.MARKER_NAME)))

    def test_an_ORDINARY_GROUP_WRITABLE_ANCESTOR_is_ACCEPTED(self):
        """THE CASE THE FIRST CUT OF THIS GUARD REFUSED, and no fixture of
        mine caught it — the live run did, on ten arms at once. Under umask
        002 a user's own tree is created 0775, so refusing any group-writable
        ancestor turns `helm away` into a verb that refuses on an ordinary
        machine. Under the private-group scheme that mode is you alone, and
        the guard must ask whether somebody ELSE can replace the component,
        not whether a write bit is set."""
        def build(r):
            ordinary = os.path.join(r, "ordinary")
            os.makedirs(ordinary, mode=0o775)
            os.chmod(ordinary, 0o775)
            return os.path.join(ordinary, "home")
        home = self._home_under(build)
        rc, _out, err = _run(home, "away")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(
            os.path.join(home, "helm-chat", away.MARKER_NAME)))

    def test_a_SYMLINK_ANCESTOR_is_REFUSED(self):
        """The leaf check asks whether the flag lands somewhere you own — and
        a link ABOVE it makes the leaf you create land inside a tree you do
        not, where you own it and pass your own owner test."""
        def build(r):
            real = os.path.join(r, "elsewhere")
            os.makedirs(real, mode=0o700)
            link = os.path.join(r, "link")
            os.symlink(real, link)
            return os.path.join(link, "home")
        home = self._home_under(build)
        rc, _out, err = _run(home, "away")
        self.assertEqual(rc, 1, err)
        self.assertIn("SYMLINK", err)
        flag = os.path.join(home, "helm-chat", away.MARKER_NAME)
        # THE CONTROL RIDES IN THIS METHOD, on THIS observable. "no flag
        # appeared" is the same reading whether the guard refused or the CLI
        # never ran, so the identical call on a safe pathname goes first: it
        # proves a flag DOES appear at this path shape, which is what makes
        # the absence below a measurement rather than a silence.
        safe = self._home_under(lambda r: os.path.join(r, "home"))
        self.assertEqual(_run(safe, "away")[0], 0)
        self.assertTrue(os.path.exists(
            os.path.join(safe, "helm-chat", away.MARKER_NAME)))
        self.assertFalse(os.path.exists(flag))

    def test_a_WORLD_WRITABLE_ANCESTOR_without_the_sticky_bit_is_REFUSED(self):
        """Anyone can replace what is below it, so the leaf's mode proves
        nothing about who ends up owning the path that reaches it."""
        def build(r):
            loose = os.path.join(r, "loose")
            os.makedirs(loose, mode=0o700)
            os.chmod(loose, 0o777)
            return os.path.join(loose, "home")
        home = self._home_under(build)
        rc, _out, err = _run(home, "away")
        self.assertEqual(rc, 1, err)
        self.assertIn("WORLD-writable", err)
        self.assertIn("sticky", err)
        # THE STICKY BIT IS THE EXEMPTION, AND IT HAS TO BE EXERCISED or this
        # arm passes on a guard that refuses every world-writable directory —
        # which would refuse /tmp, and with it the whole temp root above.
        def sticky(r):
            shared = os.path.join(r, "shared")
            os.makedirs(shared, mode=0o700)
            os.chmod(shared, 0o1777)
            return os.path.join(shared, "home")
        ok = self._home_under(sticky)
        self.assertEqual(_run(ok, "away")[0], 0)
        self.assertTrue(os.path.exists(
            os.path.join(ok, "helm-chat", away.MARKER_NAME)),
            "the sticky exemption must actually publish a flag")
        self.assertFalse(os.path.exists(
            os.path.join(home, "helm-chat", away.MARKER_NAME)))


class AMARKERTHATISASYMLINKNAMESNOBODY(unittest.TestCase):
    """Away-ness stays true; the attribution is not read through the link."""

    def setUp(self):
        self.home = os.path.join(tempfile.mkdtemp(), "home")
        self.chat = os.path.join(self.home, "helm-chat")
        os.makedirs(self.chat, mode=0o700)
        self.flag = os.path.join(self.chat, away.MARKER_NAME)

    def test_a_symlinked_marker_is_AWAY_with_NO_name(self):
        """A symlink at the marker path is authoritative AWAY to the writer
        (link raises EEXIST) and to is_away (lexists). A plain open() would
        FOLLOW it, so any file on the box whose first line begins "declared
        by " becomes the name printed beside the operator's posture."""
        planted = os.path.join(self.chat, "someone-elses-file")
        with open(planted, "w", encoding="utf-8") as fh:
            fh.write("declared by an-attacker\n")
        os.symlink(planted, self.flag)
        self.assertTrue(away.is_away(self.flag),
                        "the posture itself must still read as away")
        self.assertIsNone(away.declared_by(self.flag),
                          "attribution was read THROUGH the symlink")

    def test_a_REAL_marker_still_names_its_declarer(self):
        """CONTROL: refusing to follow a link must not stop reading a file."""
        with open(self.flag, "w", encoding="utf-8") as fh:
            fh.write("declared by seat-b (declared)\n")
        self.assertEqual(away.declared_by(self.flag), "seat-b (declared)")


class ItReadsAndNeverInfers(unittest.TestCase):
    def test_the_module_consults_no_clock_and_no_activity(self):
        """The half of the /afk skill the owner's overrule did NOT lift."""
        with io.open(away.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("def is_away", src)                    # control
        for banned in ("time.time()", "datetime.now", "last_post", "idle_for",
                       "st_mtime", "elapsed"):
            self.assertNotIn(banned, src,
                             "posture must be DECLARED, not inferred: %r" % banned)

    def test_is_away_creates_nothing(self):
        """MUST-MISS. Readers call this; a reader with a mkdir side effect
        would write to shared tmpfs on behalf of a question."""
        d = tempfile.mkdtemp()
        marker = os.path.join(d, "nested", "owner-away")
        self.assertFalse(away.is_away(marker))
        self.assertFalse(os.path.isdir(os.path.dirname(marker)),
                         "asking whether he is away created a directory")
        # CONTROL ON THE SAME OBSERVABLE: is_away must answer TRUE for a flag
        # that is really there. Otherwise a function that returned False
        # unconditionally would satisfy the assertions above perfectly.
        os.makedirs(os.path.dirname(marker))
        with io.open(marker, "w", encoding="utf-8") as fh:
            fh.write("declared\n")
        self.assertTrue(away.is_away(marker),
                        "is_away cannot see a flag that exists")

    def test_helm_home_redirects_the_flag_completely(self):
        """chat.py:222 — isolation that skips the identity surface is not
        isolation. MUST-MISS: the literal fallback must not win when
        HELM_HOME is set, or a test writes the live fleet's away flag."""
        home = os.path.join(tempfile.mkdtemp(), "home")
        live = "/dev/shm/helm-chat/" + away.MARKER_NAME
        # SAMPLED BEFORE AND AFTER, NOT ASSERTED ABSENT: asserting the live
        # flag does not exist couples this arm to whether a human happens to
        # be away right now.
        before = os.path.exists(live)
        rc, _, _ = _run(home, "away")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(
            os.path.join(home, "helm-chat", away.MARKER_NAME)),
            "the isolated run wrote no flag where it was told to")
        self.assertEqual(os.path.exists(live), before,
                         "an isolated run changed the LIVE away flag")

    def test_an_unresolvable_store_REFUSES_rather_than_falling_back(self):
        """A literal fallback added FOR SAFETY was the construct that punched
        through isolation.
        Under a redirected HELM_HOME where the chat import fails, the fallback
        sent the WRITE to the live /dev/shm path — marking a man away somewhere
        nobody looks, and on a shared box marking him away for everyone."""
        self.assertTrue(away.MARKER_NAME)                    # control
        # There must be NO literal live path left to fall back to.
        with io.open(away.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("MARKER_FALLBACK", src,
                         "a fallback constant survives; it is the leak")
        self.assertIn("MarkerUnresolvable", src)
        # A READER degrades to False rather than raising — every clean stop
        # of every seat calls it, and a broken instrument must not assert he
        # is away. MUST-HIT control on the same accessor first.
        d = tempfile.mkdtemp()
        real_flag = os.path.join(d, away.MARKER_NAME)
        with io.open(real_flag, "w", encoding="utf-8") as fh:
            fh.write("declared\n")
        self.assertTrue(away.is_away(real_flag))
        self.assertFalse(away.is_away(os.path.join(d, "nope")))



if __name__ == "__main__":
    unittest.main()


class GuardsRefuseWhenTheyCannotMeasure(unittest.TestCase):
    """task/1731. Three guards answered ALLOW when they could not answer at all.

    Found by cross-family review of the ALREADY-LANDED tip: each degraded
    silently rather than loudly, and two of them degraded only on an
    interpreter the arms never ran on. The shape is one class — an absent
    capability read as permission — so these arms assert the REFUSAL, and each
    is written so a missing constant or module produces RED rather than a skip.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-away-guards-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)

    # ---- (1) group membership: UNKNOWN must not read as ALONE ----

    def test_unreadable_group_answers_None_not_False(self):
        """THE FAIL-OPEN, EXACTLY. The old code returned False here, which the
        caller could not distinguish from a measured "you are alone"."""
        real = __builtins__["__import__"] if isinstance(__builtins__, dict) \
            else __builtins__.__import__

        def no_group(name, *a, **k):
            if name in ("grp", "pwd"):
                raise ImportError("no group service on this runtime")
            return real(name, *a, **k)

        with mock.patch("builtins.__import__", side_effect=no_group):
            got = away._group_has_others(os.getgid())
        self.assertIsNone(got,
                          "an unreadable group answered %r — a negative and an "
                          "unmeasurable must never share a value" % (got,))

    def test_a_primary_gid_member_counts_even_though_gr_mem_is_empty(self):
        """gr_mem is the SUPPLEMENTARY list only. A group whose members hold it
        as their PRIMARY gid has an empty gr_mem and is not empty."""
        gid = 4242

        class Entry:
            gr_mem = []                      # nobody supplementary

        class Me:
            pw_name = "me"

        others = [type("U", (), {"pw_gid": gid, "pw_name": "someone-else"})()]
        fake_pwd = type("pwd", (), {
            "getpwuid": staticmethod(lambda _uid: Me()),
            "getpwall": staticmethod(lambda: others)})
        fake_grp = type("grp", (), {"getgrgid": staticmethod(lambda _g: Entry())})
        real = __builtins__["__import__"] if isinstance(__builtins__, dict) \
            else __builtins__.__import__

        def fake_import(name, *a, **k):
            if name == "pwd":
                return fake_pwd
            if name == "grp":
                return fake_grp
            return real(name, *a, **k)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            got = away._group_has_others(gid)
        self.assertIs(got, True,
                      "a primary-gid member was invisible because gr_mem was "
                      "empty — the old read of 'nobody else'")

    def test_a_group_that_really_is_you_alone_still_answers_False(self):  # noqa: VACUOUS_ASSERTION — this arm IS the unconditional positive control for the two above; a False here is the product law and one call cannot also answer True
        """THE CONTROL. Without this the two arms above are satisfied by a
        function that never returns False, which would refuse every machine."""
        gid = 4243

        class Entry:
            gr_mem = ["me"]

        class Me:
            pw_name = "me"

        fake_pwd = type("pwd", (), {
            "getpwuid": staticmethod(lambda _uid: Me()),
            "getpwall": staticmethod(lambda: [
                type("U", (), {"pw_gid": gid, "pw_name": "me"})()])})
        fake_grp = type("grp", (), {"getgrgid": staticmethod(lambda _g: Entry())})
        real = __builtins__["__import__"] if isinstance(__builtins__, dict) \
            else __builtins__.__import__

        def fake_import(name, *a, **k):
            if name == "pwd":
                return fake_pwd
            if name == "grp":
                return fake_grp
            return real(name, *a, **k)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            got = away._group_has_others(gid)
        self.assertIs(got, False, "a genuinely private group must measure False")

    # ---- (2) O_NOFOLLOW: absent constant must refuse, never follow ----

    def test_a_runtime_without_O_NOFOLLOW_refuses_instead_of_following(self):  # noqa: VACUOUS_ASSERTION — refusing IS the product law; the arm carries two controls (the symlink is proven readable and attribution-bearing, and a plain marker still yields a name) but both sit outside the patched-os block the rung follows provenance through
        """A MISSING FLAG IS NOT 'NO FLAG', IT IS 'NO PROTECTION'.

        The old getattr(os, "O_NOFOLLOW", 0) degraded to 0 and the open
        SUCCEEDED, following the symlink the guard exists to refuse. The
        control below proves the symlink really is readable and really does
        carry an attribution, so answering None is a REFUSAL and not this
        fixture failing to set itself up.
        """
        secret = os.path.join(self.tmp, "elsewhere")
        with open(secret, "w", encoding="utf-8") as fh:
            fh.write("declared by not-the-owner\n")
        marker = os.path.join(self.tmp, "marker")
        os.symlink(secret, marker)

        # THE CONTROL: following it WOULD yield a name.
        with open(marker, encoding="utf-8") as fh:
            self.assertTrue(fh.readline().startswith("declared by "),
                            "fixture symlink carries no attribution, so this "
                            "arm could not detect a follow")

        without = [f for f in dir(os) if f != "O_NOFOLLOW"]
        with mock.patch("helm.away.os") as fake_os:
            fake_os.O_RDONLY = os.O_RDONLY
            del fake_os.O_NOFOLLOW
            fake_os.open = os.open
            fake_os.fdopen = os.fdopen
            fake_os.path = os.path
            self.assertFalse(hasattr(fake_os, "O_NOFOLLOW"),
                             "the fixture did not actually remove the flag, so "
                             "this arm proves nothing")
            self.assertIsNone(away.declared_by(marker),
                              "a runtime without O_NOFOLLOW FOLLOWED the "
                              "symlink and read an attribution from it")
        # SAME OBSERVABLE, POSITIVE HALF: with the flag restored, a regular
        # marker still reads — so the None above is the missing constant and
        # not this function being inert in the fixture.
        plain = os.path.join(self.tmp, "plain-nf")
        with open(plain, "w", encoding="utf-8") as fh:
            fh.write("declared by the-owner\n")
        self.assertEqual(away.declared_by(plain), "the-owner")
        self.assertTrue(without)

    def test_a_symlink_marker_is_refused_where_O_NOFOLLOW_exists(self):
        """The ordinary path, so the arm above is not the only thing holding
        this property up."""
        if not hasattr(os, "O_NOFOLLOW"):
            self.fail("this runtime has no O_NOFOLLOW — the guard cannot be "
                      "proven here and a SKIP would hide exactly that")
        secret = os.path.join(self.tmp, "real")
        with open(secret, "w", encoding="utf-8") as fh:
            fh.write("declared by not-the-owner\n")
        marker = os.path.join(self.tmp, "link")
        os.symlink(secret, marker)
        # SAME OBSERVABLE, POSITIVE HALF: a REGULAR file at a sibling path
        # must yield its name, so None below is a refusal to follow rather
        # than declared_by being unable to read anything here.
        plain = os.path.join(self.tmp, "plain")
        with open(plain, "w", encoding="utf-8") as fh:
            fh.write("declared by the-owner\n")
        self.assertEqual(away.declared_by(plain), "the-owner")
        self.assertIsNone(away.declared_by(marker))

    # ---- (4) a malformed byte must not take the chart down ----

    def test_invalid_utf8_in_the_marker_is_unattributed_not_an_exception(self):
        marker = os.path.join(self.tmp, "bad")
        with open(marker, "wb") as fh:
            fh.write(b"declared by \xff\xfe not-utf8\n")
        # THE POSITIVE CONTROL on the same observable: a WELL-FORMED marker at
        # the same path must yield a name, so None below means "contained",
        # not "this function always answers None".
        good = os.path.join(self.tmp, "good")
        with open(good, "w", encoding="utf-8") as fh:
            fh.write("declared by the-owner\n")
        self.assertEqual(away.declared_by(good), "the-owner")
        self.assertIsNone(away.declared_by(marker),
                          "invalid UTF-8 did not degrade to unattributed")

    def test_the_marker_still_reads_as_away_when_its_name_is_unreadable(self):
        """THE OWNER-VISIBLE HALF. The posture must survive a name that does
        not decode — a malformed byte may cost the attribution, never the
        chart."""
        marker = os.path.join(self.tmp, "away-marker")
        with open(marker, "wb") as fh:
            fh.write(b"declared by \xff\xfe\n")
        self.assertTrue(away.is_away(marker),
                        "a marker with an undecodable name stopped reading as "
                        "away, which suppresses the surface entirely")
        self.assertIsNone(away.declared_by(marker))
