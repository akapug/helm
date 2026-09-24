#!/usr/bin/env python3
"""`helm dispatch send` must accept its body on STDIN, because prose contains
backticks and argv goes through a shell.

THE CLASS, observed five times in one evening across two model families. A
dispatch body is prose. Passed as argv inside a double-quoted shell string, bash
performs COMMAND SUBSTITUTION on backticks: a message explaining `kind` became a
message with `kind` EXECUTED, its (empty) output spliced in, and a stray
"kind: command not found" on stderr. Four of mine went out mangled, and the reviewer's
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
import inspect
import io
import os
import sys
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import tests  # noqa: E402,F401 — plant the canonical suite environment
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
    """P1 from review: I inserted the stdin branch ABOVE the error check, and
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
    """P2 from review, and strictly stronger than the tell()==0 check I wrote:
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


class VerdictPolarityRequiredTest(unittest.TestCase):
    """New verdicts declare immutable polarity and any blocking exit answer."""

    PATH = "helm/dispatches.py"
    CASES = (
        ("approve", []),
        ("fix", [PATH]),
        ("supersede", [PATH]),
        ("concur", []),
    )

    def _verdict(self, argv):
        err = io.StringIO()
        signature = inspect.signature(D.mark_verdict)

        def fake_mark(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            called = bound.arguments
            paths = called.get("worse_than_main_paths") or []
            return ({"id": called["rid"], "tip": called["reviewed_tip"],
                     "polarity": called.get("polarity"),
                     "basis": called.get("basis"),
                     "exit_answer": "worse-than-main" if paths else None,
                     "worse_than_main_paths": paths, "announce": "n/a"}, None)

        with mock.patch.object(D, "mark_verdict", autospec=True,
                               side_effect=fake_mark) as mark, \
             mock.patch.object(D, "_verdict_land_nudge"), \
             mock.patch.object(D, "_verdict_author_nudge"), \
             contextlib.redirect_stderr(err), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = D.cmd_dispatch(argv)
        called = {}
        if mark.call_args:
            self.assertEqual(mark.call_count, 1)
            bound = signature.bind(*mark.call_args.args, **mark.call_args.kwargs)
            bound.apply_defaults()
            called = dict(bound.arguments)
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

    def test_each_declared_polarity_delivers_its_exit_answer(self):
        """Only blocking polarities carry a named worse-than-main path."""
        self.assertIn("approve", D.POLARITIES)
        self.assertIn("fix", D._EXIT_QUESTION_POLARITIES)
        self.assertEqual(D.POLARITIES,
                         ("approve", "fix", "supersede", "concur"))
        self.assertEqual(D._EXIT_QUESTION_POLARITIES, ("fix", "supersede"))
        observed = []
        for p, paths in self.CASES:
            argv = ["verdict", "a" * 32, "b" * 40, "--" + p, "--measured"]
            if paths:
                argv += ["--worse-than-main", paths[0]]
            if p == "fix":
                # A FIX NAMING NO CURE STATES WHY. This sweep is about the
                # EXIT ANSWER, so it carries the one recorded reason rather
                # than meeting a door it is not measuring.
                argv += ["--no-patch-because", "a design finding for a meld"]
            rc, called, _err = self._verdict(argv + ["evidence", "here"])
            observed.append((rc, called.get("polarity"), called.get("basis"),
                             called.get("bind_author"),
                             called.get("worse_than_main_paths")))
        self.assertEqual([row[:4] for row in observed], [
            (0, "approve", "measured", True),
            (0, "fix", "measured", True),
            (0, "supersede", "measured", True),
            (0, "concur", "measured", True),
        ])
        delivered = dict((row[1], row[-1]) for row in observed)
        self.assertIn(self.PATH, delivered["fix"])
        self.assertEqual(delivered["fix"], [self.PATH])
        self.assertEqual(delivered["supersede"], [self.PATH])
        self.assertEqual(delivered["approve"], [])
        self.assertEqual(delivered["concur"], [])

    def test_wrong_exit_answers_are_refused(self):
        refusals = []
        for p, paths in self.CASES:
            if paths:
                refusals.append((p, ["--imperfect"],
                                 "IMPERFECT IS NOT A BLOCK"))
            else:
                refusals += [
                    (p, ["--imperfect"], "only FIX/SUPERSEDE"),
                    (p, ["--worse-than-main", self.PATH],
                     "only FIX/SUPERSEDE"),
                ]
        observed = []
        for p, answer, marker in refusals:
            rc, called, err = self._verdict(
                ["verdict", "a" * 32, "b" * 40, "--" + p,
                 "--measured"] + answer + ["evidence", "here"])
            observed.append((p, answer[0], rc, called, marker in err))
        rc, called, _err = self._verdict(
            ["verdict", "a" * 32, "b" * 40, "--fix", "--measured",
             "--worse-than-main", self.PATH,
             "--no-patch-because", "a design finding for a meld",
             "evidence", "here"])
        observed.append(("fix", "--worse-than-main", rc,
                         called.get("worse_than_main_paths"), True))
        self.assertEqual(observed[-1],
                         ("fix", "--worse-than-main", 0, [self.PATH], True))
        self.assertEqual(observed[:-1],
                         [(p, answer[0], 2, {}, True)
                          for p, answer, _marker in refusals])

    def test_mark_verdict_default_is_an_actionable_refusal_not_a_write(self):
        """The default exists only to return a domain error instead of raising
        TypeError. Replay has its own compatibility path and never calls it."""
        out, why = D.mark_verdict("a" * 32, "b" * 40, "evidence")
        self.assertIsNone(out)
        self.assertIn("polarity is required", why)
        self.assertIn("can never be retired", why)


if __name__ == "__main__":
    unittest.main()
