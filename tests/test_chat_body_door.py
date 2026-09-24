#!/usr/bin/env python3
"""`helm chat post|dm` reads its body from stdin, and that read can never end.

THE FAILURE THIS PINS IS A HANG, NOT A WRONG ANSWER, and a hang is the one
outcome no assertion about output can catch. `sys.stdin.read()` returns when
the fd reaches EOF; a live UNIX socket with no writer closing it never does.
Under an agent harness stdin is exactly that, so a `post` or `dm` issued with
no body and no redirect printed nothing, created no row, and sat forever --
reading to every observer as the author forgetting to type.

THE DISCRIMINATOR IS CONTENT, NEVER MERE NON-TTY-NESS. Every timer, hook and
scripted caller sends positionally with stdin at `/dev/null`, which is non-tty
and instantly at EOF; refusing on the fd's SHAPE would break all of them. So
the arms carry both polarities and `/dev/null` is asserted to still pass
through.

THE VERB ARMS RUN A REAL SUBPROCESS WITH THE FD ON REAL FD 0, and that is a
deliberate choice rather than convenience. `resolve_one_body` reads the GLOBAL
`sys.stdin`, so an in-process arm has to swap a global -- and swapping a global
around a thread is a race that BECOMES the bug being tested: if the worker has
not entered the call when the main thread restores, the worker reads the
harness's OWN stdin and blocks there forever, taking interpreter shutdown with
it. Handing the fd to a child on fd 0 has no global to swap and exercises the
shipped entry point.

Every verb arm points HELM_HOME and HELM_CHAT_ROOM at a private directory and
a private room, so a row that IS sent never touches the fleet's rooms. Whether
a body travelled is read from the verb's OWN report -- it prints `helm chat:
id <hex>` when it mints a row -- rather than from a file, because the chat
store is RAM-backed with a write-behind disk mirror on a timer, so an empty
directory means "not flushed yet" as readily as "nothing sent".
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, dispatches  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELM = os.path.join(ROOT, "bin", "helm")
# a hang is unbounded; this only has to outlast a working answer, which is
# measured in tenths of a second
PATIENCE = 15.0


class ThePredicateTest(unittest.TestCase):
    """`stdin_has_a_body_fd` alone: no globals, no threads, no subprocess."""

    def _pair(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        return left, right

    def test_a_socket_with_NO_DATA_is_not_a_body_and_EOF_still_is(self):
        left, _right = self._pair()
        self.assertFalse(chat.stdin_has_a_body_fd(left, 0.05),
                         "a socket with no writer was treated as a body, "
                         "which is the read that never returns")
        other, far = self._pair()
        far.sendall(b"a body arrived\n")
        self.assertTrue(chat.stdin_has_a_body_fd(other, 0.05),
                        "a socket carrying bytes was not seen as a body")
        with open(os.devnull) as null:
            self.assertTrue(chat.stdin_has_a_body_fd(null, 0.05),
                            "/dev/null stopped counting as ready, so the "
                            "scripted positional pole now refuses")

    def test_an_fd_it_cannot_classify_proceeds_rather_than_refusing(self):
        """UNSELECTABLE STDIN MUST ANSWER READY. This exists to turn a hang
        into a usage error, never to invent a refusal on an fd it has no
        opinion about. No other arm reaches this branch -- they all hand over a
        real socket or a real file -- so without it the except path could
        return False and nothing would notice.
        """
        class Unselectable:
            def fileno(self):
                raise OSError("this object has no usable descriptor")

        self.assertTrue(chat.stdin_has_a_body_fd(Unselectable(), 0.05),
                        "an fd select cannot classify was treated as EMPTY, "
                        "which turns an unclassifiable stdin into a refusal")

    def test_both_doors_share_ONE_readiness_answer(self):
        """resolve_one_body has two doors -- the two-bodies refusal and the
        stdin-is-the-body read -- and they ask the same question of the same
        fd. Asking twice costs a second select window and, worse, lets the two
        answers DISAGREE about one invocation: a byte arriving between them
        would make the refusal door say empty and the read door say full.

        Counted rather than timed, because a timing assertion on a 0.2s window
        is a flake. The stub answers not-ready so nothing reads, and sys.stdin
        is swapped only on THIS thread with no worker involved -- the hazard
        the verb arms exist to avoid.
        """
        calls = []
        real = chat.stdin_has_a_body_fd

        def counting(stream, window=0.2):
            calls.append(window)
            return False

        prior = sys.stdin
        chat.stdin_has_a_body_fd = counting
        try:
            with open(os.devnull) as null:
                sys.stdin = null
                self.assertEqual(chat.resolve_one_body("a message", "post"),
                                 ("a message", None))
                asked_with_text = len(calls)
                calls.clear()
                self.assertEqual(chat.resolve_one_body("", "post"), ("", None))
                asked_without = len(calls)
        finally:
            sys.stdin = prior
            chat.stdin_has_a_body_fd = real

        self.assertEqual(asked_with_text, 1,
                         "the positional path asked readiness more than once")
        self.assertEqual(asked_without, 1,
                         "the stdin-body path asked readiness more than once")

    def test_the_dispatch_twin_asks_THIS_predicate(self):
        """ONE QUESTION, ONE IMPLEMENTATION. `dispatch send` asks the same
        thing of the same fd. Two copies would drift on exactly the details
        that matter -- whether EOF counts as ready, what an unselectable fd
        answers -- and nothing would notice.

        Asserted by SUBSTITUTION rather than by reading the source: replace
        this module's predicate and the other verb must change its answer.
        """
        asked = []
        real = chat.stdin_has_a_body_fd

        def spy(stream, window=0.2):
            asked.append(window)
            return "not a bool at all"

        chat.stdin_has_a_body_fd = spy
        try:
            got = dispatches._stdin_has_a_body_fd("any fd", 0.05)
        finally:
            chat.stdin_has_a_body_fd = real

        self.assertEqual(asked, [0.05],
                         "the dispatch verb did not route through this "
                         "module's predicate, so it carries a second copy")
        self.assertEqual(got, "not a bool at all",
                         "the dispatch verb did not return what this "
                         "predicate answered")


class TheVerbTest(unittest.TestCase):
    """The shipped CLI, with each stdin shape on real fd 0."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="helm-test-chatdoor-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def _post(self, stdin, extra=()):
        """(finished, rc, sent). `sent` is the verb's own report that it minted
        a row. finished False IS the hang this file pins."""
        env = dict(os.environ, HELM_HOME=self.home,
                   HELM_CHAT_NAME="seat-under-test", HELM_CHAT_ROOM="room-under-test")
        p = subprocess.Popen([HELM, "chat", "post"] + list(extra), stdin=stdin,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             cwd=ROOT, env=env)
        try:
            out, _ = p.communicate(timeout=PATIENCE)
            return True, p.returncode, b"helm chat: id " in (out or b"")
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            return False, None, False

    def test_a_socket_with_no_data_answers_instead_of_reading_forever(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        finished, rc, sent = self._post(left)
        self.assertTrue(finished,
                        "the verb is still reading a socket that will never "
                        "reach EOF; this is the hang, and it has no output to "
                        "assert on")
        self.assertEqual(rc, 2, "an empty body must reach the usage error")
        self.assertFalse(sent, "nothing may be sent when there is no body")

    def test_a_body_on_the_socket_is_still_read_and_sent(self):
        """The polarity that proves the guard discriminates rather than
        refusing every socket."""
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.sendall(b"a body arrived\n")
        right.shutdown(socket.SHUT_WR)
        finished, rc, sent = self._post(left)
        self.assertTrue(finished)
        self.assertEqual(rc, 0)
        self.assertTrue(sent, "a real body on a socket must still travel")

    def test_the_scripted_devnull_pole_still_sends_its_positional(self):
        """Every timer and hook sends positionally with stdin at /dev/null.
        It is non-tty and READY, because an fd at EOF is readable, so it must
        pass through untouched."""
        with open(os.devnull) as null:
            finished, rc, sent = self._post(null, ("a scripted message",))
        self.assertTrue(finished)
        self.assertEqual(rc, 0)
        self.assertTrue(sent)

    def test_a_heredoc_shaped_pipe_still_sends(self):
        r, w = os.pipe()
        os.write(w, b"a piped body\n")
        os.close(w)
        try:
            finished, rc, sent = self._post(r)
        finally:
            os.close(r)
        self.assertTrue(finished)
        self.assertEqual(rc, 0)
        self.assertTrue(sent)

    def test_two_bodies_still_refuse_rather_than_discard_one(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.sendall(b"the piped body\n")
        finished, rc, sent = self._post(left, ("the positional body",))
        self.assertTrue(finished)
        self.assertEqual(rc, 2,
                         "a positional body alongside real piped bytes must "
                         "refuse; choosing either discards the other")
        self.assertFalse(sent)


if __name__ == "__main__":
    unittest.main()
