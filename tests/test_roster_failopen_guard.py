#!/usr/bin/env python3
"""What the roster reader ACTUALLY does at its failure boundary.

WHY THIS FILE IS SMALL, AND WAS NOT. Three rounds of it carried a static AST
scanner that refused tests mocking the reader to raise. A cross-family review
retired it on four counts and every count was right: it was UNSOUND (a helper
that raises through a callee reads as safe), INCOMPLETE (keyword-form patch
targets, nested test directories, and four assignment idioms all bypassed it),
it produced FALSE POSITIVES on safe value-lists, and it governed the wrong
LAYER — test spellings rather than the production consumers that actually turn
an absent answer into a decision. Reconstructing unittest.mock semantics from
an AST was the wrong instrument for the question.

AND ITS PREMISE WAS FALSE, which is the part worth keeping in front of the next
reader. The scanner existed because "the reader cannot raise, so a mock that
raises tests fiction". That is TRUE of pk.read_json and FALSE of roster(),
and both are pinned below as executed facts rather than as prose. The author
of that claim "verified" it by executing pk.read_json — a NEIGHBOUR of the
claim, not the claim — and built three rounds of machinery on the difference.

What survives is the part that was never in dispute: run the real functions at
their real boundaries and record what comes back, so nobody has to reason about
it from the source again.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helm import pk, seats_common  # noqa: E402


class ReadJsonSwallowsEverythingTest(unittest.TestCase):
    """pk.read_json CANNOT raise: `except Exception: return default`. Every
    consumer therefore receives a value that cannot distinguish a missing file
    from a corrupt one, and that indistinguishability is the whole of
    task/886."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="helm-test-failopen-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self.dir, name)

    def test_every_real_failure_mode_returns_the_default_and_never_raises(self):
        malformed = self._path("bad.json")
        with open(malformed, "w", encoding="utf-8") as f:
            f.write("{ this is not json\n")
        isdir = self._path("adir.json")
        os.mkdir(isdir)
        denied = self._path("denied.json")
        with open(denied, "w", encoding="utf-8") as f:
            f.write("{}")
        os.chmod(denied, 0)
        # An unreadable file is only unreadable to a user the mode applies to.
        # Root ignores it, so this sub-case must SAY it was skipped rather than
        # pass silently and be counted as coverage it did not provide. The
        # first cut computed this flag and then said NOTHING when it was
        # false — a skip nobody can see is indistinguishable from a case that
        # ran, which is the same silent-absence shape this whole row is about.
        try:
            with open(denied, encoding="utf-8"):
                denied_blocks = False
        except OSError:
            denied_blocks = True
        if not denied_blocks:
            sys.stderr.write(
                "test_roster_failopen_guard: SKIPPING the unreadable-file "
                "case — this process can read a chmod-000 file (running as "
                "root?), so that mode is not a barrier here and the case "
                "would prove nothing. The other three still ran.\n")

        sentinel = {"sentinel": True}
        cases = [("missing", self._path("nope.json")),
                 ("malformed", malformed), ("is-a-directory", isdir)]
        if denied_blocks:
            cases.append(("unreadable", denied))
        for label, path in cases:
            self.assertIs(pk.read_json(path, sentinel), sentinel,
                          "%s did not return the default" % label)
        self.assertGreaterEqual(len(cases), 3, "control: cases were built")

        # POSITIVE CONTROL on the same call, so the defaults above are the
        # reader declining and not a stub that always declines.
        good = self._path("good.json")
        with open(good, "w", encoding="utf-8") as f:
            f.write('{"seat": 1}')
        self.assertEqual(pk.read_json(good, sentinel), {"seat": 1})

    def test_a_falsy_document_is_indistinguishable_from_a_failure(self):
        """The quieter half. roster() ends in `or {}`, so a valid JSON null,
        0, [] or "" also arrives as an empty dict — a caller cannot tell a
        corrupt roster from an empty one from a JSON null.

        DRIVEN THROUGH roster() ITSELF, not through a hand-written copy of its
        expression. The first cut of this arm asserted on
        `pk.read_json(p, {}) or {}` — the same characters roster() contains,
        which is precisely the substitution that cost this lane three rounds:
        an ADJACENT symbol feels identical to the claim and is not it. If
        roster() ever stops ending in `or {}`, only this version notices."""
        prior = os.environ.get("HELM_CHAT_DIR")
        os.environ["HELM_CHAT_DIR"] = self.dir

        def restore():
            if prior is None:
                os.environ.pop("HELM_CHAT_DIR", None)
            else:
                os.environ["HELM_CHAT_DIR"] = prior
        self.addCleanup(restore)

        p = seats_common.roster_path()
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"seat-a": {}}')
        self.assertTrue(seats_common.roster(),
                        "control: roster() must read a real document, or the "
                        "empties below prove only that the path is wrong")
        for doc in ("null", "0", "[]", '""'):
            with open(p, "w", encoding="utf-8") as f:
                f.write(doc)
            self.assertEqual(seats_common.roster(), {},
                             "%s should reach the caller as an empty roster,"
                             " indistinguishable from a read failure" % doc)


class RosterCanRaiseTest(unittest.TestCase):
    """AND roster() IS NOT read_json. It is `pk.read_json(roster_path(), {})`,
    and roster_path() resolves the chat dir FIRST — so it can raise before the
    swallowing reader is ever reached.

    This arm exists because the claim "the reader cannot raise" was asserted
    for three rounds, defended as executed, and was only ever executed against
    pk.read_json. A cross-family reviewer produced the counter-example. It is
    pinned here so the distinction is a fact in the suite rather than a
    sentence someone believes."""

    def test_roster_raises_when_its_own_path_cannot_be_resolved(self):
        gone = tempfile.mkdtemp(prefix="helm-test-vanish-")
        prior_cwd = os.getcwd()
        # BOTH overrides, and the second one is why the first cut of this arm
        # passed by hand and failed in the suite: chat_dir prefers an absolute
        # HELM_CHAT_DIR, and the suite sets one, so the cwd is never consulted
        # and nothing raises. A relative HELM_HOME only reaches the resolver
        # when no CHAT_DIR override outranks it.
        prior = {k: os.environ.get(k) for k in ("HELM_HOME", "HELM_CHAT_DIR")}
        os.chdir(gone)
        os.rmdir(gone)                     # cwd removed under the process
        os.environ["HELM_HOME"] = "relative-home"
        os.environ.pop("HELM_CHAT_DIR", None)

        def restore():
            os.chdir(prior_cwd)
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)

        with self.assertRaises(OSError):
            seats_common.roster()

        # CONTROL, from inside the same test, so the raise above is the
        # removed cwd and not the fixture being broken generally: restore the
        # cwd and the identical call answers with a dict.
        os.chdir(prior_cwd)
        self.assertIsInstance(seats_common.roster(), dict)
        # and the control must be reached through the SAME resolver, not a
        # restored override — HELM_CHAT_DIR stays popped until cleanup.


class RosterAcquiredRefusesAFifoTest(unittest.TestCase):
    """task/2523 r1, P2: roster_acquired opened the roster with a
    blocking open(), so a writerless FIFO where the roster belongs hung every
    caller, and `helm seat composers` became one when its census began judging
    repair strands against the roster. A non-regular file is an UNREADABLE
    roster: ({}, True), answered without waiting for a writer. The read runs on
    a thread with a deadline, and a reader blocked in open() is released by
    opening the FIFO's write end, so a RED arm fails instead of wedging."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="helm-test-roster-fifo-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        prior = os.environ.get("HELM_CHAT_DIR")
        os.environ["HELM_CHAT_DIR"] = self.dir

        def restore():
            if prior is None:
                os.environ.pop("HELM_CHAT_DIR", None)
            else:
                os.environ["HELM_CHAT_DIR"] = prior
        self.addCleanup(restore)
        self.path = seats_common.roster_path()
        self.assertTrue(self.path.startswith(self.dir + os.sep), self.path)

    def _acquired(self):
        import threading
        from helm import seats_roster
        box = {}
        t = threading.Thread(
            target=lambda: box.update(value=seats_roster.roster_acquired()),
            daemon=True)
        t.start()
        t.join(5)
        if t.is_alive():
            for _ in range(50):
                try:
                    os.close(os.open(self.path, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:
                    pass
                t.join(0.2)
                if not t.is_alive():
                    break
            self.fail("roster_acquired blocked opening %s" % self.path)
        return box["value"]

    def test_a_fifo_roster_is_an_unreadable_roster_answered_at_once(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"seat-a": {}}')
        self.assertEqual(self._acquired(), ({"seat-a": {}}, False),
                         "control: a regular roster must still read")
        os.unlink(self.path)
        self.assertEqual(self._acquired(), ({}, False),
                         "control: a missing roster stays proven empty")
        os.mkfifo(self.path)
        self.assertEqual(self._acquired(), ({}, True))

    def test_a_fifo_a_writer_fed_a_valid_roster_is_still_not_the_roster(self):
        """The refusal is the FILE TYPE, not a read that happened to fail: a
        non-blocking read of a writerless FIFO ends at EOF and fails to parse
        anyway, so only a FIFO whose writer already queued a valid roster
        tells the type check apart from the parse."""
        os.mkfifo(self.path)
        writer = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, writer)
        os.write(writer, b'{"seat-a": {}}')
        self.assertEqual(self._acquired(), ({}, True))


if __name__ == "__main__":
    unittest.main()
