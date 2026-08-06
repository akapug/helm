#!/usr/bin/env python3
"""`helm dispatch send` must accept its body on STDIN, because prose contains
backticks and argv goes through a shell.

THE CLASS, observed five times in one evening across two model families. A
dispatch body is prose. Passed as argv inside a double-quoted shell string, bash
performs COMMAND SUBSTITUTION on backticks: a message explaining `kind` became a
message with `kind` EXECUTED, its (empty) output spliced in, and a stray
"kind: command not found" on stderr. Four of mine went out mangled, and codex-3's
reply carried its own "disregard its mangled command examples".

It is not merely mangling. It is arbitrary command execution selected by the
CONTENT of a message — the same shape as the delimiter bug landed the night
before, one layer up.

`helm chat post` had already solved this in exactly this spot, with a stdin path.
The literal shell routes are a pipe or a quoted-delimiter heredoc; an unquoted
heredoc still substitutes. `dispatch send` lacked the same stdin door. These tests
pin it open.
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import dispatches as D  # noqa: E402

NASTY = ('explaining `kind` and $(command substitution) and $HOME and "quotes"')


class _Stdin(io.StringIO):
    """A non-tty stdin, which is what a pipe or heredoc looks like."""
    def isatty(self):
        return False


class _Tty(io.StringIO):
    def isatty(self):
        return True


class StdinBodyTest(unittest.TestCase):
    def _send(self, argv, stdin):
        """Run cmd_dispatch and capture what reached send()."""
        seen = {}

        def fake_send(recipient, lane, message, ref, **kw):
            seen.update(recipient=recipient, lane=lane, message=message, ref=ref,
                        kwargs=kw)
            return {"id": "x" * 32, "recipient": recipient, "lane": lane}, None, True
        with mock.patch.object(D, "send", side_effect=fake_send), \
             mock.patch.object(sys, "stdin", stdin), \
             mock.patch.object(D, "_deadline", return_value=(3600, None)):
            rc = D.cmd_dispatch(argv)
        return rc, seen

    def test_a_piped_body_reaches_send_BYTE_FOR_BYTE(self):
        rc, seen = self._send(["send", "codex-3", "probe", "--ref", "a" * 40, "--kind", "review"],
                              _Stdin(NASTY + "\n"))
        self.assertEqual(rc, 0)
        self.assertEqual(seen["message"], NASTY,
                         "the literal stdin path must not transform the body")
        for tok in ("`kind`", "$(command substitution)", "$HOME", '"quotes"'):
            self.assertIn(tok, seen["message"], tok)

    def test_an_argv_body_still_works_and_WINS_over_stdin(self):
        """Backwards compatibility is not optional — every existing caller passes
        argv. A body on the command line must be used even when something is
        piped, or a stray pipe would silently replace an intended message."""
        rc, seen = self._send(["send", "codex-3", "probe", "the argv body",
                               "--ref", "a" * 40, "--kind", "review"],
                              _Stdin("the piped body\n"))
        self.assertEqual(rc, 0)
        self.assertEqual(seen["message"], "the argv body")

    def test_a_literal_force_word_in_argv_prose_does_not_authorize_a_fork(self):
        rc, seen = self._send(
            ["send", "codex-3", "probe", "please", "use", "--force",
             "carefully", "--ref", "a" * 40, "--kind", "review",
             "--new-work"],
            _Stdin("ignored pipe\n"))
        self.assertEqual(rc, 0)
        self.assertEqual(seen["message"], "please use --force carefully")
        self.assertFalse(seen["kwargs"]["force"])

    def test_a_trailing_force_flag_reaches_an_argv_body_send(self):
        rc, seen = self._send(
            ["send", "codex-3", "probe", "the body", "--ref", "a" * 40,
             "--kind", "review", "--new-work", "--force"],
            _Stdin("ignored pipe\n"))
        self.assertEqual(rc, 0)
        self.assertEqual(seen["message"], "the body")
        self.assertTrue(seen["kwargs"]["force"])

    def test_a_trailing_force_flag_reaches_a_stdin_body_send(self):
        rc, seen = self._send(
            ["send", "codex-3", "probe", "--ref", "a" * 40,
             "--kind", "review", "--new-work", "--force"],
            _Stdin("the piped body\n"))
        self.assertEqual(rc, 0)
        self.assertEqual(seen["message"], "the piped body")
        self.assertTrue(seen["kwargs"]["force"])

    def test_a_TTY_stdin_is_never_read(self):
        """An interactive caller who forgets the body must get usage, not a hang
        waiting on a terminal that will never send EOF."""
        rc, seen = self._send(["send", "codex-3", "probe", "--ref", "a" * 40, "--kind", "review"],
                              _Tty(""))
        self.assertEqual(rc, 2)
        self.assertEqual(seen, {})

    def test_an_EMPTY_pipe_is_usage_not_an_empty_dispatch(self):
        """`... < /dev/null` must not send a blank obligation to a seat."""
        rc, seen = self._send(["send", "codex-3", "probe", "--ref", "a" * 40, "--kind", "review"],
                              _Stdin("   \n"))
        self.assertEqual(rc, 2)
        self.assertEqual(seen, {})

    def test_multiline_bodies_survive(self):
        body = "line one\nline two with `backticks`\nline three"
        rc, seen = self._send(["send", "codex-3", "probe", "--ref", "a" * 40, "--kind", "review"],
                              _Stdin(body + "\n"))
        self.assertEqual(rc, 0)
        self.assertEqual(seen["message"], body)

    def test_stdin_is_NOT_consumed_by_the_add_verb(self):
        """`add` takes no message at all; reading stdin there would swallow a
        pipe the caller intended for something else in a shell pipeline.

        THE FIRST VERSION OF THIS TEST WAS WEAK and I flagged it to the reviewer
        rather than let it pass: it asserted only `add.called`, which stays true
        whether or not stdin was drained. A test for "X is not consumed" must
        inspect X afterwards. It also mocked a return contract `add()` does not
        have — it returns the row, not (row, err) — which is a mock testing my
        belief instead of the code."""
        stdin = _Stdin("stray input meant for the next stage of a pipeline")
        with mock.patch.object(sys, "stdin", stdin), \
             mock.patch.object(D, "add", return_value=({"id": "y" * 32,
                                                       "recipient": "codex-3",
                                                       "lane": "l"}, None)) as add, \
             mock.patch.object(D, "_deadline", return_value=(3600, None)):
            rc = D.cmd_dispatch(["add", "codex-3", "lane", "--ref", "a" * 40, "--kind", "review"])
        self.assertEqual(rc, 0)
        self.assertTrue(add.called)
        # THE ACTUAL ASSERTION: the pipe is still there for whoever wanted it.
        self.assertEqual(stdin.tell(), 0, "add consumed stdin")
        self.assertEqual(stdin.read(),
                         "stray input meant for the next stage of a pipeline")
        # and the body never reached add() as a message — add takes none
        self.assertNotIn("stray input", str(add.call_args))

    def test_the_usage_line_advertises_the_stdin_route(self):
        """A door nobody knows about does not close the class — the four mangled
        dispatches happened because the safe route was not visible at the point
        of use."""
        self.assertIn("stdin", D.USAGE)


class MalformedOptionTest(unittest.TestCase):
    """P1 from codex-3: I inserted the stdin branch ABOVE the error check, and
    `_parse` returns pos=None on a bad option — so len(pos) tracebacked before
    the check two lines down could run. Three clean usage errors became a
    TypeError. The ordering was the entire defect, and only a NON-TTY stdin
    reaches the branch, which is why nothing else caught it."""

    def _run(self, argv, stdin_text="a body"):
        with mock.patch.object(sys, "stdin", _Stdin(stdin_text)), \
             mock.patch.object(D, "send") as send:
            rc = D.cmd_dispatch(argv)
        return rc, send.called

    def test_an_unknown_option_is_usage_not_a_traceback(self):
        rc, called = self._run(["send", "codex-3", "lane", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertFalse(called)

    def test_an_unknown_option_before_a_valid_tail_is_not_recast_as_prose(self):
        rc, called = self._run(
            ["send", "codex-3", "lane", "body", "--bogus",
             "--ref", "a" * 40, "--kind", "review", "--new-work"])
        self.assertEqual(rc, 2)
        self.assertFalse(called)

    def test_a_value_less_option_is_usage_not_a_traceback(self):
        rc, called = self._run(["send", "codex-3", "lane", "--ref"])
        self.assertEqual(rc, 2)
        self.assertFalse(called)

    def test_a_bare_double_dash_is_usage_not_a_traceback(self):
        rc, called = self._run(["send", "codex-3", "lane", "--"])
        self.assertEqual(rc, 2)
        self.assertFalse(called)


class ReadBombTest(unittest.TestCase):
    """P2 from codex-3, and strictly stronger than the tell()==0 check I wrote:
    a stdin whose read() RAISES proves read was never CALLED, where a position
    check only proves it was not advanced."""

    class _Bomb(io.StringIO):
        def isatty(self):
            return False

        def read(self, *a):
            raise AssertionError("stdin.read() was called when it must not be")

    def test_add_never_touches_stdin(self):
        with mock.patch.object(sys, "stdin", self._Bomb()), \
             mock.patch.object(D, "add", return_value=({"id": "y" * 32,
                                                       "recipient": "c", "lane": "l"}, None)), \
             mock.patch.object(D, "_deadline", return_value=(3600, None)):
            self.assertEqual(D.cmd_dispatch(["add", "c", "l", "--ref", "a" * 40, "--kind", "review"]), 0)

    def test_send_with_an_ARGV_body_never_touches_stdin(self):
        """argv-wins must not merely PREFER argv — it must not read at all."""
        with mock.patch.object(sys, "stdin", self._Bomb()), \
             mock.patch.object(D, "send",
                               return_value=({"id": "x" * 32, "recipient": "c",
                                              "lane": "l"}, None, True)), \
             mock.patch.object(D, "_deadline", return_value=(3600, None)):
            self.assertEqual(D.cmd_dispatch(
                ["send", "c", "l", "the body", "--ref", "a" * 40, "--kind", "review"]), 0)


if __name__ == "__main__":
    unittest.main()


class VerdictPolarityRequiredTest(unittest.TestCase):
    """A NEW verdict must DECLARE its direction.

    Omitting the flag used to record UNDECLARED, which is the right REPLAY
    behaviour for rows written before polarity existed and the wrong DEFAULT for
    a fresh write nobody intends. Re-measured 2026-07-31 from the ledger itself:
    28 of 314 verdicts are genuinely UNDECLARED. A verdict is immutable, so none
    can ever gain the direction its writer omitted.

    The requirement therefore lives at BOTH entrances: the CLI refuses before
    calling the owner, and mark_verdict returns an actionable domain refusal to
    any future library caller. Replay never calls mark_verdict; historical rows
    stay untouched through _replay_polarity().
    """

    def _verdict(self, argv):
        called = {}

        def fake_mark(rid, tip, evidence, polarity=None, basis=None):
            # MIRRORS THE REAL SIGNATURE, basis included: a double that drifts
            # from the function it stands in for stops testing that function.
            called.update(rid=rid, polarity=polarity, basis=basis)
            return {"id": rid, "tip": tip, "polarity": polarity,
                    "basis": basis, "announce": "n/a"}, None
        err = io.StringIO()
        with mock.patch.object(D, "mark_verdict", side_effect=fake_mark), \
             contextlib.redirect_stderr(err), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = D.cmd_dispatch(argv)
        return rc, called, err.getvalue()

    def test_a_verdict_without_a_polarity_flag_is_REFUSED(self):
        rc, called, err = self._verdict(
            ["verdict", "a" * 32, "b" * 40, "looks", "fine", "to", "me"])
        self.assertEqual(rc, 2, "an undeclared verdict must not be recorded")
        self.assertEqual(called, {},
                         "mark_verdict must never be reached without a polarity")
        self.assertIn("DECLARE the polarity", err)
        # the refusal must say WHY it cannot be fixed later, since that is the
        # whole cost — a verdict is immutable
        self.assertIn("IMMUTABLE", err)

    def test_each_declared_polarity_still_records(self):
        for p in ("approve", "fix", "supersede"):
            rc, called, _err = self._verdict(
                ["verdict", "a" * 32, "b" * 40, "--" + p, "--measured",
                 "evidence", "here"])
            self.assertEqual(rc, 0, "declared %s must record" % p)
            self.assertEqual(called.get("polarity"), p)
            # the basis reaches the writer too, not merely the parser — the
            # flag I just added would otherwise be pinned only by rc 0
            self.assertEqual(called.get("basis"), "measured")

    def test_mark_verdict_default_is_an_actionable_refusal_not_a_write(self):
        """The default exists only to return a domain error instead of raising
        TypeError. Replay has its own compatibility path and never calls it."""
        out, why = D.mark_verdict("a" * 32, "b" * 40, "evidence")
        self.assertIsNone(out)
        self.assertIn("polarity is required", why)
        self.assertIn("can never be retired", why)
