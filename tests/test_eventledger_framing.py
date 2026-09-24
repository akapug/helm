"""The two ledger readers must agree, byte for byte, on every framing.

THERE IS ONE READER. `checked_rows` is a thin adapter onto `_rows_from_fd`,
so it cannot disagree with it about framing — a shared row DEFINITION is not
enough, because two machines can agree perfectly about what a row is and
still frame differently. The consequence for these tests is direct and easy
to get wrong: comparing `checked_rows` against `_rows_from_fd` compares a
function with ITSELF, and 416 such comparisons prove only that the code is
deterministic. So the matrix asserts against `_expected` — the SPECIFICATION,
written out by hand and built from no production helper. It is not a rival
reader, and agreement with it is not "two readers agree"; it is the reader
matching what the grammar says.

THE FAILURE THIS FILE EXISTS TO PREVENT: an incremental reader that clears
its buffer at MAX_EVENT_BYTES and then reads the following bytes as a fresh
line will, on an over-long line whose later chunk holds valid JSON before its
newline, return id=forged AND id=good where the buffered reader returns only
id=good. An authority reader that can INVENT a row is worse than any overrun,
and a shared row definition does not prevent it: that shares the grammar and
leaves the FRAMING free to differ.
"""
import io
import json
import tempfile
import shutil
import os
import time
import unittest
from unittest import mock

from helm import eventledger, projscope


def _row(rid):
    return ('{"id": "%s"}' % rid).encode()


def _forgeable(chunk):
    """An over-long line whose tail holds a VALID object, positioned so an
    unguarded buffer-clear lands EXACTLY on that object's first byte.

    THE ALIGNMENT IS THE WHOLE FIXTURE. A reader that drops its buffer the
    moment it passes MAX_EVENT_BYTES with no newline resumes at the START OF
    THE NEXT CHUNK, so the forged object is reachable only when the head's
    length is a MULTIPLE OF THE CHUNK SIZE — otherwise the clear lands
    mid-padding, the residue never parses, and the arm is inert while looking
    careful. A head of 65568 bytes, for instance, is divisible by none of the
    chunk sizes this matrix uses: with it, both the dedicated arm below and
    the matrix's own "overlong then forgeable tail" case run at eight sizes,
    pass, and are blind to a reader that forges. The alignment is therefore
    ASSERTED by every caller, never assumed.

    Returns (data, head_len) so the caller can ASSERT the property rather
    than trust this docstring's arithmetic.
    """
    head = b'{"id": "x", "pad": "' + b"A" * (
        eventledger.MAX_EVENT_BYTES + 10) + b'"}'
    head += b"A" * ((-len(head)) % chunk)      # round UP to a chunk multiple
    data = head + _row("forged") + b"\n" + _row("good") + b"\n"
    return data, len(head)


def _expected(data, strict, skip_blank):
    """THE EXPECTED ROWS AND THE EXPECTED REASON, spelled out here.

    THIS IS NOT A SECOND READER AND MUST NOT BE DESCRIBED AS ONE. `checked_rows`
    is a thin adapter onto `_rows_from_fd`, so comparing the two is a function
    compared with ITSELF, and a matrix of 416 such comparisons proves only
    that the code is deterministic. What replaces it is not a rival implementation whose
    agreement would mean something; it is the SPECIFICATION, written out by
    hand, and the matrix asserts the reader against it.

    Nothing here is built from `_row_verdict` or any other production helper,
    for the ordinary reason that expectations transcribed from the code under
    test are not expectations.

    THE GRAMMAR IT SPECIFIES, in the order the reader must apply it: a line
    ends at b"\n" and nothing else; the bytes after the final newline are an
    UNTERMINATED TAIL and are not a line at any length; `MAX_EVENT_BYTES`
    bounds the line INCLUDING its terminator; the size bound is answered
    before the blank exemption, which is answered before JSON. The reason
    string is part of the specification, not a diagnostic: it separates
    REJECTED from UNREADABLE for a caller and it reaches an operator verbatim.
    """
    out = []
    parts = data.split(b"\n")
    parts.pop()                       # the unterminated tail is not a line
    line = 0
    for part in parts:
        line += 1
        if len(part) >= eventledger.MAX_EVENT_BYTES:
            if strict:
                return [], (eventledger.CORRUPT_PREFIX
                            + "%d exceeds %d bytes"
                            % (line, eventledger.MAX_EVENT_BYTES))
            continue
        if skip_blank and not part.strip():
            continue
        try:
            row = json.loads(part.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if strict:
                return [], (eventledger.CORRUPT_PREFIX
                            + "%d is not valid UTF-8 JSON" % line)
            continue
        if not (isinstance(row, dict) and row.get("id")):
            if strict:
                return [], (eventledger.CORRUPT_PREFIX
                            + "%d is not an object with a non-empty id" % line)
            continue
        out.append(row)
    return out, None


class ReaderEquivalenceTest(unittest.TestCase):
    CHUNKS = (1, 2, 7, 63, 64, 65, 4096, 1 << 20)

    def cases(self, chunk):
        """Inputs built FOR a chunk size, not compared against a fixed list.

        The forgery case is only reachable when the over-long head's length
        is a multiple of the chunk (see `_forgeable`), so a `cases()` that
        ignored the chunk would carry the named defect as an INERT fixture:
        the matrix would contain the case, run it at eight sizes, and still be
        blind to a reader that forges. Build the input from the axis it
        depends on.
        """
        big = b"A" * (eventledger.MAX_EVENT_BYTES + 10)
        good, other = _row("good"), _row("other")
        forgeable, _head = _forgeable(chunk)
        return {
            "empty": b"",
            "one row": good + b"\n",
            "two rows": good + b"\n" + other + b"\n",
            "unterminated tail": good + b"\n" + b'{"id": "torn"',
            "blank line": good + b"\n\n" + other + b"\n",
            "corrupt middle": good + b"\n{bad}\n" + other + b"\n",
            "corrupt terminated tail": good + b"\n{bad}\n",
            "bare CR inside a row": b'{"id": "a\rb"}\n' + good + b"\n",
            # THE FORGERY CASE: an over-long line whose tail is valid JSON,
            # aligned so an unguarded clear lands on that JSON's first byte.
            "overlong then forgeable tail": forgeable,
            "overlong at EOF unterminated":
                good + b"\n" + b'{"id": "y", "pad": "' + big,
            "two overlong lines in a row":
                b"x" * (eventledger.MAX_EVENT_BYTES + 5) + b"\n"
                + b"y" * (eventledger.MAX_EVENT_BYTES + 5) + b"\n"
                + good + b"\n",
            # An over-long run of WHITESPACE, terminated, so
            # `skip_blank` and the over-long discard decide the same line.
            # It must span more than two chunks for the discard state to
            # survive a read boundary, which is where the two readers can
            # part company without either of them looking wrong alone.
            "overlong blank line then a row":
                good + b"\n" + b" " * (eventledger.MAX_EVENT_BYTES + 10)
                + b"\n" + other + b"\n",
            "overlong blank line at EOF":
                good + b"\n" + b" " * (eventledger.MAX_EVENT_BYTES + 10),
        }

    def test_the_reader_matches_the_specified_semantics_at_every_chunk(self):
        checked = 0
        for size in self.CHUNKS:
            for name, data in self.cases(size).items():
                for strict in (False, True):
                    # SKIP_BLANK IS AN AXIS. It was False for this whole
                    # matrix and True in one short tolerant arm, so
                    # `strict and skip_blank` — the pair that decides an
                    # over-long BLANK line — was never run, and the readers
                    # disagreed on it at chunk 1 and 7 while agreeing at 64
                    # and 4096. The rule: size bound before
                    # blank exemption, and chunk size never changes
                    # authority.
                    for skip_blank in (False, True):
                        # THE SPECIFICATION, NOT `checked_rows`.
                        # `checked_rows` delegates to the very function under
                        # test, so it cannot disagree with it about anything
                        # and a matrix built on it measures determinism.
                        want, want_reason = _expected(
                            data, strict, skip_blank)
                        with mock.patch.object(
                                eventledger, "_READ_CHUNK", size):
                            got, reason = eventledger._rows_from_fd(
                io.BytesIO(data), strict, skip_blank)
                        checked += 1
                        # THE EXACT REASON, NOT `bool(reason)`. The string is
                        # observable: it separates REJECTED from UNREADABLE
                        # for a caller and it reaches an operator verbatim.
                        # Comparing truthiness lets the two readers agree that
                        # SOMETHING was wrong while naming different lines and
                        # different faults, which is most of what a framing
                        # disagreement looks like from the outside.
                        self.assertEqual(
                            (got, reason), (want, want_reason),
                            "the reader does not match the specified "
                            "semantics on %r at chunk=%d strict=%s "
                            "skip_blank=%s: got=%r expected=%r"
                            % (name, size, strict, skip_blank, got, want))
        self.assertGreater(checked, 100,
                           "MUST-HIT: the matrix did not run, so agreement "
                           "was never compared (%d comparisons)" % checked)

    def test_an_overlong_line_cannot_forge_a_row_from_its_tail(self):  # noqa: VACUOUS_ASSERTION — the fixture control (the tail parses as
        # id=forged) is per-chunk BY CONSTRUCTION, so it cannot leave
        # the loop; the unconditional guard is the `proved == 3`
        # must-hit below it, which fails if the loop ran short.
        """THE NAMED CASE, kept as its own arm so a regression says WHAT broke
        rather than only that the matrix disagreed.

        A reader that drops its buffer at `MAX_EVENT_BYTES` and resumes at
        the next chunk returns ['forged', 'good'] here at every chunk size
        below; the guarded reader returns ['good']. A MISALIGNED fixture
        returns ['good'] either way and so distinguishes nothing, which is why
        the alignment below is asserted rather than assumed.
        """
        proved = 0
        for chunk in (32, 64, 4096):
            data, head_len = _forgeable(chunk)
            self.assertEqual(
                head_len % chunk, 0,
                "FIXTURE IS INERT: the forged object must begin exactly "
                "where an unguarded buffer-clear would land, i.e. at a "
                        "multiple "
                "of chunk=%d, but the head is %d bytes" % (chunk, head_len))
            # POSITIVE CONTROL ON THE FIXTURE ITSELF. `["good"]` is also
            # what a fixture with nothing forgeable in it produces, so the
            # arm must first prove there IS a forgery available to make:
            # the bytes following the aligned head are a VALID object, and
            # they are what an unguarded reader would parse as a fresh row.
            forged = data[head_len:data.index(b"\n", head_len)]
            self.assertEqual(
                json.loads(forged).get("id"), "forged",
                "FIXTURE IS INERT: nothing forgeable follows the head at "
                "chunk=%d, so asserting the forgery is absent proves "
                "nothing (%r)" % (chunk, forged[:60]))
            with mock.patch.object(eventledger, "_READ_CHUNK", chunk):
                got, _reason = eventledger._rows_from_fd(
                io.BytesIO(data), False, False)
            ids = [r.get("id") for r in got]
            self.assertEqual(ids, ["good"],
                             "the incremental reader invented a row from the "
                             "tail of an over-long line at chunk=%d: %r"
                             % (chunk, ids))
            proved += 1
        # UNCONDITIONAL MUST-HIT. Everything above is inside the loop, so an
        # empty or skipped chunk set would make this arm pass having compared
        # nothing — the same inertness, one level up, that the arm exists to
        # fix. The count is asserted OUTSIDE the loop for that reason.
        self.assertEqual(proved, 3,
                         "MUST-HIT: the forgery arm ran %d of 3 chunk sizes, "
                         "so absence of a forgery was never established"
                         % proved)

    def test_an_overlong_blank_line_is_not_a_blank_line(self):
        """THE GRAMMAR, as its own arm: complete line -> SIZE BOUND ->
        blank exemption -> JSON.

        `skip_blank` exempts BOUNDED SEPARATORS — the empty lines a normal
        append produces — never an arbitrarily large malformed write. A 65KB
        run of spaces is a torn or padded record, and folding it away silently
        under strict is the same family as inventing a row out of an over-long
        line's tail. The answer must also be IDENTICAL at every chunk size:
        before this rule it depended on whether the terminator happened to
        arrive in the read that crossed the bound.
        """
        data = (_row("good") + b"\n"
                + b" " * (eventledger.MAX_EVENT_BYTES + 10) + b"\n"
                + _row("other") + b"\n")
        seen = set()
        for chunk in (1, 7, 64, 4096, 1 << 20):
            with mock.patch.object(eventledger, "_READ_CHUNK", chunk):
                rows, reason = eventledger._rows_from_fd(
                io.BytesIO(data), True, True)
            self.assertEqual(rows, [], "strict must return NO rows: %r" % rows)
            self.assertIn("exceeds", reason or "",
                          "an over-long blank line must be refused for its "
                          "SIZE at chunk=%d, not exempted: %r"
                          % (chunk, reason))
            seen.add(reason)
        self.assertEqual(len(seen), 1,
                         "chunk size changed the authority answer: %r" % seen)
        # UNCONDITIONAL POSITIVE CONTROL, and it is the one that makes the
        # refusals above mean SIZE. A BOUNDED separator must still be
        # exempted; if skip_blank were simply broken, every assertion above
        # would pass for the wrong reason and this arm would be proving that
        # blank lines are refused rather than that OVER-LONG ones are.
        bounded = _row("good") + b"\n" + b"   \n" + _row("other") + b"\n"
        rows, reason = eventledger._rows_from_fd(
                io.BytesIO(bounded), True, True)
        self.assertIsNone(reason,
                          "a bounded blank separator must still be exempted "
                          "under skip_blank: %r" % reason)
        self.assertEqual([r["id"] for r in rows], ["good", "other"],
                         "the control lost rows: %r" % rows)

    def test_skip_blank_agrees_too(self):
        data = _row("a") + b"\n\n   \n" + _row("b") + b"\n"
        want, _ = eventledger.checked_rows(data, skip_blank=True)
        with mock.patch.object(eventledger, "_READ_CHUNK", 3):
            got, _ = eventledger._rows_from_fd(
                io.BytesIO(data), False, True)
        self.assertEqual(got, want)
        self.assertTrue(want, "MUST-HIT: no rows survived, so agreeing on "
                              "nothing proves nothing")

    def test_dense_rows_do_not_scale_worse_than_the_buffer_reader(self):  # noqa: VACUOUS_ASSERTION — the two must-hits ARE the unconditional
        # positives: the fixture must carry thousands of rows and BOTH
        # paths must frame the same number of them. A ratio arm has no
        # empty observable to control for; its failure mode is comparing
        # two different jobs, which is what those two assertions close.
        """THE FRAMER MUST NOT PAY PER ROW FOR THE BYTES BEHIND IT.

        THE FIXTURE IS THE MINIMAL ROW, and the choice is the arm. Row size
        is the axis this defect hides behind: per-row cost is paid once per
        ROW and amortised over the row's BYTES, so a fat row dilutes it by
        exactly its own length: a 94-byte row is 11,155 rows per MiB and
        b'{"id":1}\n' is 116,508, so a per-row cost that shows as 1.30x on the
        second reads as 1.07x on the first. Only the denser number is about
        the framer. 1MiB of the minimal row is the densest a valid ledger
        gets, and therefore the only honest denominator.

        TWO WAYS TO PAY PER ROW, and this arm sees both. Slicing the remainder
        off the front for every row (`raw, buf = buf[:cut+1], buf[cut+1:]`)
        rebuilds the SHRINKING SUFFIX each time and copies hundreds of
        megabytes per 64KiB chunk. Scanning it with a per-row `find` avoids
        the copying and still pays a PYTHON-LEVEL step per row, which at this
        density is the whole gap. One `bytes.split` per chunk pays neither.

        The arm is a RATIO against the same work done another way, not a wall
        clock: an absolute threshold measures the box, and this file has to
        pass on hardware nobody has bought yet.
        """
        row = b'{"id":1}'
        data = (row + b"\n") * (1024 * 1024 // (len(row) + 1))
        self.assertGreater(data.count(b"\n"), 100000,
                           "MUST-HIT: the fixture is not DENSE — this arm is "
                           "about per-row cost and a fat row hides it")

        def quietest_ratio(a, b, n=7):
            """THE MINIMUM OF THE PER-ROUND RATIOS, not a ratio of minima.

            This arm runs on a shared build node beside other suites, so the
            question is not "how fast is this box" but "which round was quiet
            enough to compare in". A ratio of two independently-minimised
            timings can pair a quiet baseline with a spiked framer and report
            a number NEITHER ROUND OBSERVED, and on this ceiling that is
            enough to redden a framer that is in fact within it.

            Each round times both sides ADJACENTLY and divides them there, so
            a spike lands in one round's ratio and that round is discarded by
            the min. The two timings that survive were taken microseconds
            apart under the same load.
            """
            best = None
            for _ in range(n):
                t = time.perf_counter(); a(); da = time.perf_counter() - t
                t = time.perf_counter(); b(); db = time.perf_counter() - t
                r = da / db
                best = r if best is None else min(best, r)
            return best

        rows, reason = eventledger.checked_rows(data)
        self.assertIsNone(reason)
        self.assertEqual(len(rows), data.count(b"\n"),
                         "MUST-HIT: the reader did not frame every row, so "
                         "the timing below is of the wrong work")
        # THE BASELINE IS THE WHOLE-BUFFER READER'S ACTUAL SHAPE: one split
        # over the entire buffer with the grammar INLINE. Routing it through
        # `_row_verdict` instead would put the framer's own per-row call on
        # BOTH sides of the ratio, which shrinks every difference this arm
        # exists to see: a framer paying one extra Python-level step per row
        # scores ~1.22 against that baseline and ~1.33 against this one, and
        # only the second separation is wide enough to hold a ceiling.
        def baseline():
            out = []
            loads = json.loads
            for raw in data.split(b"\n"):
                if not raw:
                    continue
                try:
                    row_ = loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(row_, dict) and row_.get("id"):
                    out.append(row_)
            return out
        # AND IT MUST AGREE ROW FOR ROW, not just in count. An inline grammar
        # is a SECOND grammar by construction; the thing this file exists to
        # prevent is two of them disagreeing, so the baseline is pinned to the
        # framer's own output before either is timed.
        self.assertEqual(baseline(), rows,
                         "MUST-HIT: the baseline frames different rows, so "
                         "the ratio compares two different jobs")
        ratio = quietest_ratio(
            lambda: eventledger.checked_rows(data), baseline)
        self.assertLess(ratio, 1.15,
                        "the framer is %.2fx the whole-buffer cost on the "
                        "densest valid rows — it is paying PER ROW again "
                        "(a suffix copy, a per-row scan, or a per-row "
                        "global lookup)" % ratio)


class ReaderExpiryTest(unittest.TestCase):
    """AN EXPIRY LEAVES THIS MODULE AS A TYPE, NOT AS A SENTENCE."""

    def _ledger(self, body=b'{"id": "a"}\n'):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "ledger.jsonl")
        with open(path, "wb") as fh:
            fh.write(body)
        return path

    def test_an_expiry_escapes_the_readers_own_fail_open(self):
        """THE BROAD HANDLER WOULD TURN THE RAISE BACK INTO A STRING.

        `checked_events_with_identity` ends in `except Exception as exc:
        return None, str(exc), None` -- a fail-open written for I/O errors.
        `projscope.Expired` is an ordinary exception, so without an explicit
        re-raise that handler catches this function's OWN deadline checks and
        converts the timeout into an unreadable-ledger STRING. The reader then
        LOOKS like it raises and does not, which is worse than never having
        raised: every consumer written against the type is silently bypassed.
        """
        path = self._ledger()
        with self.assertRaises(projscope.Expired):
            eventledger.checked_events_with_identity(
                path, strict=True, deadline=-1.0)
        # MUST-HIT: a genuinely unreadable ledger still comes back as a
        # STRING through that same handler, or this arm is satisfied by a
        # reader that has started raising on everything.
        rows, unavailable, identity = eventledger.checked_events_with_identity(
            os.path.join(os.path.dirname(path), "nested", "nope.jsonl"),
            strict=True)
        self.assertIsNone(identity)
        gone = self._ledger()
        os.chmod(gone, 0)
        self.addCleanup(os.chmod, gone, 0o600)
        rows2, unavailable2, _id2 = eventledger.checked_events_with_identity(
            gone, strict=True)
        self.assertIsNone(rows2, "an unreadable ledger returned rows")
        self.assertIsInstance(
            unavailable2, str,
            "MUST-HIT: an ordinary unreadable ledger no longer comes back as "
            "a string, so the raise above says nothing about the handler")

    def test_ambient_deadline_reaches_all_reader_entrypoints(self):  # noqa: VACUOUS_ASSERTION — unbudgeted control below
        path = self._ledger()
        with projscope.scope(deadline=0):
            with self.assertRaises(projscope.Expired):
                eventledger.checked_rows(b'{"id": "a"}\n')
            with self.assertRaises(projscope.Expired):
                eventledger.checked_events(path, strict=True)
            with self.assertRaises(projscope.Expired):
                eventledger.checked_events_with_identity(path, strict=True)

    def test_reader_open_cannot_wait_on_a_fifo_swap(self):
        path = self._ledger()
        rows, unavailable = eventledger.checked_events(path, strict=True)
        rows2, unavailable2, identity = (
            eventledger.checked_events_with_identity(path, strict=True))
        self.assertEqual([row["id"] for row in rows], ["a"])
        self.assertEqual([row["id"] for row in rows2 or ()], ["a"])
        self.assertIsNone(unavailable)
        self.assertIsNone(unavailable2)
        self.assertIsNotNone(identity)
        self.assertIsNotNone(eventledger.ledger_identity(path))

        def hostile(reader):
            target = self._ledger()
            prepare = eventledger._prepare
            open_ = eventledger.os.open
            close = eventledger.os.close
            seen = []

            def swap(path_, create=False):
                ready = prepare(path_, create)
                os.unlink(ready)
                os.mkfifo(ready)
                seen.append("swapped")
                return ready

            def guarded_open(path_, flags, *args, **kwargs):
                if path_ == target:
                    self.assertTrue(
                        flags & os.O_NONBLOCK,
                        "the FIFO open would wait forever without O_NONBLOCK")
                    seen.append("opened")
                return open_(path_, flags, *args, **kwargs)

            def tracked_close(fd):
                close(fd)
                seen.append("closed")

            with mock.patch.object(eventledger, "_prepare",
                                   side_effect=swap), \
                    mock.patch.object(eventledger.os, "open",
                                      side_effect=guarded_open), \
                    mock.patch.object(eventledger.os, "close",
                                      side_effect=tracked_close), \
                    mock.patch.object(eventledger, "_rows_from_fd") as parse:
                result = reader(target)
            self.assertEqual(seen, ["swapped", "opened", "closed"],
                             "MUST-HIT: the validated path was not swapped to "
                             "a real FIFO and rejected after open")
            parse.assert_not_called()
            return result

        pair = hostile(lambda target: eventledger.checked_events(target))
        self.assertEqual(pair[0], [])
        self.assertIn("not a private regular file", pair[1])
        triple = hostile(
            lambda target: eventledger.checked_events_with_identity(target))
        self.assertIsNone(triple[0])
        self.assertIn("not a private regular file", triple[1])
        self.assertIsNone(triple[2])
        self.assertIsNone(hostile(eventledger.ledger_identity))

    def test_reader_open_refuses_without_required_nonblock(self):  # noqa: VACUOUS_ASSERTION — valid-reader controls are above
        path = self._ledger()
        with mock.patch.object(eventledger.openflags.os, "O_NONBLOCK", None), \
                mock.patch.object(eventledger.os, "open") as opened:
            self.assertIsNone(eventledger.ledger_identity(path))
            rows, unavailable = eventledger.checked_events(path)
            self.assertEqual(rows, [])
            self.assertIn("O_NONBLOCK", unavailable)
            rows2, unavailable2, identity = (
                eventledger.checked_events_with_identity(path))
            self.assertIsNone(rows2)
            self.assertIn("O_NONBLOCK", unavailable2)
            self.assertIsNone(identity)
        opened.assert_not_called()

    def test_pair_reader_expiry_outranks_absence(self):  # noqa: VACUOUS_ASSERTION — final assertion proves the non-expired result
        missing = os.path.join(tempfile.mkdtemp(), "missing.jsonl")
        self.addCleanup(shutil.rmtree, os.path.dirname(missing), True)
        with mock.patch.object(eventledger, "_past",
                               side_effect=(False, True)):
            with projscope.scope(deadline=1):
                with self.assertRaises(projscope.Expired):
                    eventledger.checked_events(missing)
        self.assertEqual(eventledger.checked_events(missing), ([], None),
                         "MUST-HIT: absence remains known empty when observed "
                         "inside the budget")

    def test_pair_reader_checks_expiry_after_descriptor_cleanup(self):  # noqa: VACUOUS_ASSERTION — in-budget I/O control follows
        path = self._ledger()
        closed = []
        close = os.close

        def tracked_close(fd):
            close(fd)
            closed.append(True)

        with mock.patch.object(eventledger.os, "fstat",
                               side_effect=OSError("unreadable")), \
                mock.patch.object(eventledger.os, "close",
                                  side_effect=tracked_close), \
                mock.patch.object(eventledger, "_past",
                                  side_effect=lambda _deadline: bool(closed)):
            with projscope.scope(deadline=1):
                with self.assertRaises(projscope.Expired):
                    eventledger.checked_events(path)
        with mock.patch.object(eventledger.os, "fstat",
                               side_effect=OSError("unreadable")):
            rows, unavailable = eventledger.checked_events(path)
        self.assertEqual(rows, [])
        self.assertIn("OSError: unreadable", unavailable,
                      "MUST-HIT: an in-budget I/O failure remains UNKNOWN")

    def test_an_unbudgeted_read_never_raises(self):
        """THE ARM THAT KEEPS THIS FROM BEING A TAX ON EVERY EXISTING CALLER.

        Only a caller with an explicit or ambient deadline can be refused.
        Outside either scope, every existing reader behaves exactly as it did.
        """
        path = self._ledger()
        rows, unavailable, identity = eventledger.checked_events_with_identity(
            path, strict=True)
        self.assertEqual([r["id"] for r in rows or []], ["a"])
        self.assertIsNone(unavailable)
        self.assertIsNotNone(identity)
        self.assertEqual(eventledger.checked_rows(b'{"id": "a"}\n')[0],
                         [{"id": "a"}])


if __name__ == "__main__":
    unittest.main()
