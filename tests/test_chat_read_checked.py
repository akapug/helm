#!/usr/bin/env python3
"""`chat.read_checked` — the stricter door beside the forgiving poll primitive.

`read()` fails open twice over and both are RIGHT for the caller it was written
for: an unreadable room returns ([], 0), the same answer an empty room gives,
and a torn line is skipped in silence. A live tail must behave that way — a
poller that dies on one bad byte stops the fleet.

They are wrong for a caller proving a row ABSENT. `_locate_row` says "no message
matches that id" and sends someone to re-type a perfectly good id, over a lane
nothing could read. So read_checked reports WHY the answer may be short, and
read() delegates to it and drops the fault so every existing caller — the CLI,
--follow, the web GET, the TUI — answers byte-identically.

A reviewer found this file's absence before its content: there was no regression
arm for unreadable, malformed, non-dict, or undecodable rows. Each fault below
is a DIFFERENT cause with a different remedy, and collapsing any two sends an
operator to fix the wrong thing.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import chat  # noqa: E402


class ReadCheckedFaultTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="helm-readchecked-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HELM_HOME": self.dir,
                                           "HELM_CHAT_DIR": self.dir,
                                           "HELM_CHAT_NAME": "zz-reader"})
        env.start(); self.addCleanup(env.stop)

    def _write(self, payload):
        path = chat.room_path("main")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(payload, bytes) else "w"
        with open(path, mode) as f:
            f.write(payload)
        return path

    def _row(self, text):
        return '{"id": "aaaaaaaaaaaa", "text": "%s"}\n' % text

    def test_a_HEALTHY_room_reports_no_fault(self):
        """The pole. Every arm below asserts a fault; without this one, a
        reader that faulted unconditionally would satisfy all of them."""
        self._write(self._row("hello"))
        rows, total, fault = chat.read_checked("main")
        self.assertIsNone(fault)
        self.assertEqual((len(rows), total), (1, 1))

    def test_a_MISSING_room_is_PROVEN_empty_not_a_fault(self):
        """A room nobody has posted to is empty, and saying "I could not look"
        about it would make every honest absence unprovable. Same line
        roster_checked draws between missing and unreadable."""
        rows, total, fault = chat.read_checked("never-posted-to")
        self.assertEqual((rows, total, fault), ([], 0, None))

    def test_an_UNREADABLE_room_is_a_FAULT_not_an_empty_room(self):
        """The whole reason this door exists: ([], 0) from read() is
        indistinguishable from an empty room, and a caller proving absence
        must not treat it as one."""
        path = self._write(self._row("hidden"))
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)
        if os.access(path, os.R_OK):
            self.skipTest("cannot make a file unreadable as this uid")
        rows, _total, fault = chat.read_checked("main")
        self.assertEqual(fault, "unreadable")
        self.assertEqual(rows, [])

    def test_a_MALFORMED_json_line_reports_TORN_ROWS(self):
        self._write(self._row("good") + "{ this is not json\n")
        rows, _total, fault = chat.read_checked("main")
        self.assertEqual(fault, "torn-rows")
        self.assertEqual(len(rows), 1, "the readable row was lost too")

    def test_a_NON_DICT_json_line_reports_TORN_ROWS(self):
        """Valid JSON that is not an object. `_msg` drops it exactly as it
        drops a parse failure, so the count is short and the fault must fire —
        a row that is a bare list carries no id and no text."""
        self._write(self._row("good") + '["not", "an", "object"]\n')
        _rows, _total, fault = chat.read_checked("main")
        self.assertEqual(fault, "torn-rows")

    def test_INVALID_UTF8_reports_UNDECODABLE_even_though_the_json_parses(self):
        """THE ONE A REVIEWER HAD TO FIND, because every count agrees.

        The reader decoded with errors="replace" inline. Invalid bytes inside a
        row become U+FFFD while the JSON around them stays perfectly valid, so
        every line parses, len(msgs) == len(raw), and the torn-rows check —
        the only fault that existed — could never fire. If the corruption lands
        in an ID, `_locate_row` then issues its confident "no message matches"
        over a row that is present and whose identity was silently rewritten:
        the exact false absence this door was built to prevent, on the one
        input where it looked healthiest.
        """
        good = self._row("fine").encode("utf-8")
        torn = b'{"id": "bb\xff\xfebb", "text": "mangled"}\n'
        self._write(good + torn)
        rows, _total, fault = chat.read_checked("main")
        self.assertEqual(fault, "undecodable",
                         "invalid UTF-8 was replaced silently and reported "
                         "clean — every row still parsed, which is exactly why "
                         "a count-based check cannot see it")
        self.assertEqual(len(rows), 2,
                         "the rows must still come back: read() stays "
                         "fail-open and only the VERDICT is stricter")

    def test_read_ANSWERS_IDENTICALLY_and_never_gains_a_third_value(self):
        """read() delegates and DROPS the fault. If it ever propagated one, the
        CLI, --follow, the web GET and the TUI would all start unpacking three
        values from a two-value contract."""
        for payload in (self._row("ok"),
                        self._row("ok") + "{ torn\n",
                        b'{"id": "cc\xffcc"}\n'):
            self._write(payload)
            rows, total, _fault = chat.read_checked("main")
            self.assertEqual(chat.read("main"), (rows, total))


if __name__ == "__main__":
    unittest.main()
