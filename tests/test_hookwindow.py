"""The hook END window (task/3259): hours of END rows where the raw stream
holds minutes, three raw readings of the box taken at every END and stored
null when they are not a real reading, and `helm hooks latency --hours H`,
which prints what the stream holds and judges nothing. Private fake roots; no
hook installation, no network, no live /proc reading in any assertion."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from helm import cli, hooklatency as latency, hookrun, home, hookwindow

HOUR = 3600 * 10**9
CALM = {"load1": 3.0, "cpus": 8, "psi_cpu_some_avg10": 5.0}


class HookWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(
            latency, "path", lambda: str(self.root / "ledger")))
        self.stack.enter_context(patch.object(home, "chat_name",
                                              lambda: "fake-seat"))
        # HELM_SCRATCH_GC=0: these arms name the Stop hook's spans, and the
        # scratch tripwire holds every such module to a disarmed reaper.
        self.stack.enter_context(patch.dict(os.environ,
                                            {"HELM_NO_TREE_WARNING": "1",
                                             "HELM_SCRATCH_GC": "0"}))
        self.reading = dict(CALM)
        self.real_read_box = hookwindow.read_box
        self.stack.enter_context(patch.object(
            hookwindow, "read_box", lambda proc=None: dict(self.reading)))
        self.addCleanup(lambda: self.assertFalse(latency.active()))

    def rows(self):
        out = []
        for n in range(hookwindow.GENERATIONS + 1):
            name = hookwindow.path() + ("." + str(n) if n else "")
            if os.path.exists(name):
                out += [json.loads(line) for line in
                        Path(name).read_text().splitlines() if line.strip()]
        return out

    def plant(self, now, hours, extra=()):
        """Stop event rows every 5 min, from 150 s before `now` back over
        `hours`: half a step off every window edge, so a reader whose clock
        is a few ms later counts the same rows. Returns how many it wrote."""
        rows = [{"v": 1, "t": now - (150 + n * 300) * 10**9, "seat": "s",
                 "ev": "Stop", "st": "event", "ms": 200.0, "out": "completed",
                 **CALM} for n in range(int(hours * 12) + 1)]
        dest = hookwindow.path()
        # THE LOCK FILE EXISTS, as it does beside every stream a real writer
        # made: a reader takes its snapshot under that lock.
        Path(dest + ".lock").touch()
        with open(dest, "a") as f:
            for r in sorted(rows + list(extra), key=lambda r: r["t"]):
                f.write(json.dumps(r) + "\n")
        return len(rows) + len(extra)

    def fresh(self):
        for name in self.root.iterdir():
            os.unlink(name)

    def test_it_rolls_by_age_keeps_two_hours_and_stays_bounded(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are the exact list of generation files present, a kept span of at least 2 h, and exactly GENERATIONS files after the byte roll
        """ROTATED BY AGE: a generation rolls once its first row is an hour
        old, so three of them always reach back 2 h. BOUNDED: never more than
        GENERATIONS files, and a burst rolls on bytes before any file passes
        MAX_BYTES."""
        start = 1_800_000_000 * 10**9
        end = {"kind": "END", "stage": "event", "event": "Stop", "seat": "s",
               "outcome": "completed", "wall_ms": 12.0}
        for minute in range(0, 5 * 60 + 1, 5):
            self.assertTrue(hookwindow.note(
                dict(end, time_ns=start + minute * 60 * 10**9)))
        names = [hookwindow.path() + ("." + str(n) if n else "")
                 for n in range(hookwindow.GENERATIONS + 1)]
        present = [n for n in names if os.path.exists(n)]
        self.assertEqual(names[:hookwindow.GENERATIONS], present)
        stamps = [r["t"] for r in self.rows()]
        self.assertGreaterEqual(max(stamps) - min(stamps), 2 * HOUR)
        self.assertLess(max(stamps) - min(stamps), 3 * HOUR + 1,
                        "a fourth hour was kept: the stream is not bounded "
                        "by its generations")
        got = hookwindow.report(2.0, now_ns=max(stamps))
        self.assertGreaterEqual(got["reach_hours"], 2.0, got)
        self.assertEqual((min(stamps), max(stamps)),
                         (got["first_row_ns"], got["last_row_ns"]))
        # THE BYTE BACKSTOP, on a fresh stream.
        for name in present:
            os.unlink(name)
        with patch.object(hookwindow, "MAX_BYTES", 1024):
            for n in range(60):
                hookwindow.note(dict(end, time_ns=start + n))
            sizes = [os.path.getsize(n) for n in names if os.path.exists(n)]
        self.assertEqual(hookwindow.GENERATIONS, len(sizes))
        self.assertTrue(all(s <= 1024 for s in sizes), sizes)

    def test_a_deeply_nested_header_does_not_stop_the_byte_backstop(self):
        head = b"[" * 1100 + b"0" + b"]" * 1100 + b"\n"
        self.assertLess(len(head), hookwindow.HEAD_BYTES)
        dest = Path(hookwindow.path())
        dest.write_bytes(head)
        now = 1_800_000_000 * 10**9
        end = {"kind": "END", "stage": "event", "event": "Stop", "seat": "s",
               "outcome": "completed", "wall_ms": 1.0, "time_ns": now}
        # The C decoder on newer Python accepts this depth; the stdlib's
        # recursive decoder exercises the same failure on every runner.
        decoder = json.JSONDecoder()
        decoder.scan_once = json.scanner.py_make_scanner(decoder)
        with patch.object(json, "_default_decoder", decoder), \
                patch.object(hookwindow, "MAX_BYTES", len(head)):
            self.assertTrue(hookwindow.note(dict(end)))
            self.assertTrue(hookwindow.note(dict(end, time_ns=now + 1)))
            got = hookwindow.report(1.0, now_ns=now + 1)
        self.assertEqual(head, Path(str(dest) + ".1").read_bytes())
        self.assertEqual(
            [{"v": 1, "t": t, "seat": "s", "ev": "Stop", "st": "event",
              "ms": 1.0, "out": "completed", **CALM} for t in (now, now + 1)],
            [json.loads(line) for line in dest.read_text().splitlines()])
        self.assertEqual((2, 2, 1), (got["rows"], got["rows_in_window"],
                                    got["unreadable_lines"]))

    def test_only_the_rows_it_keeps_are_kept(self):
        """A completed lock span is not kept; the same span timed out is,
        with where it was and its ids; every kept row carries the three raw
        readings under their own names."""
        base = {"kind": "END", "event": "PostToolUse", "seat": "s",
                "time_ns": 1_800_000_000 * 10**9, "wall_ms": 3.0,
                "event_id": "e1", "span_id": "s2", "parent_id": "s1",
                "blocked_in": "x.py:1:f"}
        self.assertFalse(hookwindow.note(dict(base, stage="lock-seats",
                                              outcome="acquired")))
        self.assertFalse(hookwindow.note(dict(base, kind="START",
                                              stage="event", outcome=None)))
        self.assertTrue(hookwindow.note(dict(base, stage="lock-seats",
                                             outcome="timeout")))
        self.assertTrue(hookwindow.note(dict(base, stage="delivery",
                                             outcome="completed")))
        kept = self.rows()
        self.assertEqual(["lock-seats", "delivery"], [r["st"] for r in kept])
        self.assertEqual(("e1", "s2", "s1", "x.py:1:f"),
                         tuple(kept[0][k] for k in ("eid", "sp", "par", "bi")))
        self.assertEqual([CALM, CALM],
                         [{k: r[k] for k in hookwindow.FIELDS} for r in kept])
        self.assertNotIn("bi", kept[1], "a completed row carries no incident "
                                        "fields")

    def test_the_readings_parse_the_kernel_files(self):
        proc = self.root / "proc"
        (proc / "pressure").mkdir(parents=True)
        (proc / "loadavg").write_text("11.77 12.44 11.05 17/9524 621441\n")
        (proc / "pressure" / "cpu").write_text(
            "some avg10=33.52 avg60=30.09 avg300=16.45 total=1\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        with patch.object(os, "cpu_count", lambda: 16):
            got = self.real_read_box(str(proc))   # the real reader, not the stub
        self.assertEqual({"load1": 11.77, "cpus": 16,
                          "psi_cpu_some_avg10": 33.52}, got)
        # NO PSI IS NO READING, never a zero that claims a calm box.
        empty = self.root / "bare"
        empty.mkdir()
        self.assertEqual((None, None),
                         tuple(self.real_read_box(str(empty))[k] for k in
                               ("load1", "psi_cpu_some_avg10")))

    def test_a_reading_that_is_not_a_real_number_is_stored_null_never_a_crash(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are the typed control row and the count of bad cases each written with null fields; the loop runs over a fixed table
        """HUGE, NEGATIVE, NON-FINITE OR NON-NUMERIC: each reading is stored
        as null and the row is still written, through the real reader and the
        real writer. A cpu count past any box, a load past the kernel's task
        limit and a share past 100 are no readings either."""
        # OUTSIDE the ledger root, which `fresh` empties file by file.
        procdir = tempfile.TemporaryDirectory()
        self.addCleanup(procdir.cleanup)
        proc = Path(procdir.name)
        (proc / "pressure").mkdir()
        row = {"kind": "END", "stage": "stop-guard", "event": "Stop",
               "seat": "s", "outcome": "timeout", "wall_ms": 20000.0,
               "time_ns": 1_800_000_000 * 10**9, "event_id": "e",
               "span_id": "a", "parent_id": None, "blocked_in": "x.py:1:f"}

        def written(load, psi, cpus):
            self.fresh()
            (proc / "loadavg").write_text(load)
            (proc / "pressure" / "cpu").write_text(psi)
            with patch.object(hookwindow, "read_box", self.real_read_box), \
                    patch.object(hookwindow, "PROC", str(proc)), \
                    patch.object(os, "cpu_count", cpus):
                self.assertTrue(hookwindow.note(dict(row)))
            (kept,) = self.rows()
            return {k: kept[k] for k in hookwindow.FIELDS}

        # CONTROL: real readings are kept, typed.
        got = written("12.50 1 1 1/1 1\n", "some avg10=33.50 avg60=1\n",
                      lambda: 16)
        self.assertEqual({"load1": 12.5, "cpus": 16,
                          "psi_cpu_some_avg10": 33.5}, got)
        self.assertEqual((float, int, float),
                         tuple(type(got[k]) for k in hookwindow.FIELDS))

        def refuse():
            raise OSError("no cpu count")

        bad = {
            "non-numeric": ("abc 1 1 1/1 1\n", "some avg10=abc\n",
                            lambda: "8"),
            "not a finite number": ("nan 1 1 1/1 1\n", "some avg10=nan\n",
                                    lambda: 8.0),
            "infinite": ("inf 1 1 1/1 1\n", "some avg10=1e999\n",
                         lambda: True),
            "negative": ("-1.0 1 1 1/1 1\n", "some avg10=-3.0\n",
                         lambda: -4),
            "huge": ("5000000.0 1 1 1/1 1\n", "some avg10=150.00\n",
                     lambda: 1 << 70),
            "past every bound": ("1e999 1 1 1/1 1\n", "some avg10=100.01\n",
                                 lambda: 0),
            "empty or raising": ("", "", refuse),
        }
        seen = []
        for name, (load, psi, cpus) in bad.items():
            with self.subTest(case=name):
                self.assertEqual(dict.fromkeys(hookwindow.FIELDS),
                                 written(load, psi, cpus))
                seen.append(name)
        self.assertEqual(list(bad), seen)

    def test_the_box_is_read_before_any_row_is_written(self):  # noqa: VACUOUS_ASSERTION — the order of the reads and the writes IS the contract under test, compared whole in one unconditional assertEqual
        """THE READING IS THE BOX AT THE END, NOT AFTER THIS PROCESS'S OWN
        WRITES: for each kept END the readings are taken before the raw
        append, the incident copy and the window append, and a START row
        takes none."""
        order = []

        def read(proc=None):
            order.append("read")
            return dict(CALM)

        with patch.object(hookwindow, "read_box", read), \
                patch.object(latency, "append",
                             lambda row: order.append(
                                 ("append", row["kind"], row["stage"])) or True), \
                patch.object(latency, "_retain",
                             lambda start, end: order.append(
                                 ("retain", end["stage"]))), \
                patch.object(hookwindow, "_append",
                             lambda dest, wire, now: order.append(
                                 ("window", json.loads(wire)["st"])) or True):
            with latency.event_scope("standalone", "Stop"), \
                    latency.stage("stop-guard"):
                latency.mark("timeout")
        self.assertEqual([("append", "START", "event"),
                          ("append", "START", "stop-guard"),
                          "read", ("append", "END", "stop-guard"),
                          ("retain", "stop-guard"), ("window", "stop-guard"),
                          "read", ("append", "END", "event"),
                          ("retain", "event"), ("window", "event")], order)

    def test_a_timeout_through_the_real_writer_lists_every_timed_out_row(self):
        """The timeout marks its span and both ancestors; the window keeps
        all three and the print lists all three, under one event id, each
        with where it was and the readings taken at its END. It picks no
        innermost span and labels nothing."""
        where = "proxywatch.py:469:delivery_state_guard"
        with latency.event_scope("composite"):
            with latency.stage("delivery"):
                with latency.stage("lock-proxywatch"):
                    latency.mark("timeout")
                    latency.blocked_in(where)
        got = hookwindow.report(1.0)
        self.assertEqual(["lock-proxywatch", "delivery", "event"],
                         [i["stage"] for i in got["timeouts"]])
        self.assertEqual(1, len({i["event_id"] for i in got["timeouts"]}))
        self.assertEqual([(where, CALM)] * 3,
                         [(i["where"], {k: i[k] for k in hookwindow.FIELDS})
                          for i in got["timeouts"]])
        self.assertNotIn("label", json.dumps(got))
        hook = next(h for h in got["hooks"]
                    if (h["event"], h["stage"]) == ("PostToolUse", "event"))
        self.assertEqual((1, 1, 0), (hook["rows"], hook["timeouts"], hook["n"]))

    def test_the_alarm_frame_reaches_the_window(self):
        """The frame the SIGALRM handler captured is what the window rows
        name, on the path where the timeout PROPAGATES through the spans."""
        def the_place_it_was_stuck():
            exc = hookrun._Timeout()
            exc.where = hookrun._where(sys._getframe())
            raise exc

        with self.assertRaises(hookrun._Timeout):
            with latency.event_scope("standalone", "Stop"), \
                    latency.stage("stop-guard"):
                the_place_it_was_stuck()
        got = hookwindow.report(1.0)["timeouts"]
        self.assertEqual(["stop-guard", "event"], [i["stage"] for i in got])
        for i in got:
            self.assertIn("the_place_it_was_stuck", i["where"])

    def test_a_parent_cycle_or_a_repeated_span_hides_no_timeout(self):
        """A MALFORMED TREE CANNOT HIDE A TIMEOUT: the print lists every
        timed-out row, so two spans naming each other as parent and a span
        id written twice are all still listed."""
        now = time.time_ns()
        timeout = {"v": 1, "seat": "s", "ev": "Stop", "st": "stop-guard",
                   "ms": 20000.0, "out": "timeout", "eid": "e",
                   "bi": "x.py:1:f", **CALM}
        cycle = [dict(timeout, t=now - 3 * 10**9, sp="a", par="b"),
                 dict(timeout, t=now - 2 * 10**9, sp="b", par="a"),
                 dict(timeout, t=now - 10**9, sp="a", par="b")]
        self.plant(now, 1.0, extra=cycle)
        got = hookwindow.report(2.0, now_ns=now)
        self.assertEqual([("a", "b"), ("b", "a"), ("a", "b")],
                         [(i["span_id"], i["parent_id"])
                          for i in got["timeouts"]])

    def test_an_unreadable_line_is_counted_never_a_crash_or_a_row(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are the count of malformed lines each seen as one unreadable line with the planted rows intact, and the writer's refused row; the loop runs over a fixed table
        """A line that is not exactly what the writer writes -- a malformed
        or non-finite number, a duplicate key, a wrong type, a missing id, a
        reading out of its domain -- is one unreadable line: never a row,
        never a stamp, never a crash of the verb."""
        now = time.time_ns()
        good = {"v": 1, "t": now - HOUR, "seat": "s", "ev": "Stop",
                "st": "event", "ms": 200.0, "out": "completed", **CALM}
        timeout = dict(good, t=now - 10**9, st="stop-guard", out="timeout",
                       eid="e", sp="a", par=None, bi="x.py:1:f")
        bad = {
            "unknown stage": json.dumps(dict(good, st="nope")),
            "bool stamp": json.dumps(dict(good, t=True)),
            "huge stamp": json.dumps(dict(timeout, t=2**70)),
            "negative stamp": json.dumps(dict(good, t=-5)),
            "digits past the limit": json.dumps(good).replace(
                '"t": %d' % good["t"], '"t": ' + "9" * 5000),
            "NaN wall": json.dumps(dict(good, ms=float("nan"))),
            "overflowing wall": json.dumps(good).replace('"ms": 200.0',
                                                         '"ms": 1e999'),
            "duplicate key": json.dumps(good).replace(
                '"ms": 200.0', '"ms": 200.0, "ms": 5000.0'),
            "string load": json.dumps(dict(timeout, load1="30")),
            "negative load": json.dumps(dict(timeout, load1=-1.0)),
            "huge cpus": json.dumps(dict(timeout, cpus=2**70)),
            "bool cpus": json.dumps(dict(timeout, cpus=True)),
            "float cpus": json.dumps(dict(timeout, cpus=8.0)),
            "share over 100": json.dumps(dict(timeout,
                                              psi_cpu_some_avg10=150.0)),
            "list outcome": json.dumps(dict(good, out=["completed"])),
            "timeout without its event id": json.dumps(
                {k: v for k, v in timeout.items() if k != "eid"}),
            "an id with a space": json.dumps(dict(timeout, sp="a b")),
            "a missing reading": json.dumps(
                {k: v for k, v in good.items() if k != "cpus"}),
            "extra key": json.dumps(dict(good, extra=1)),
            "true version": json.dumps(dict(good, v=True)),
            "not an object": "[1, 2]",
            "deep nesting": "[" * 100000 + "]" * 100000,
        }
        seen = []
        for name, line in bad.items():
            with self.subTest(row=name):
                self.fresh()
                planted = self.plant(now, 1.5)
                with open(hookwindow.path(), "a") as f:
                    f.write(line + "\n")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(0, cli.main(["hooks", "latency",
                                                  "--hours", "2"]))
                self.assertIn("unreadable lines 1;", out.getvalue())
                got = hookwindow.report(2.0, now_ns=now)
                self.assertEqual((1, planted, []),
                                 (got["unreadable_lines"], got["rows"],
                                  got["timeouts"]), name)
                seen.append(name)
        self.assertEqual(list(bad), seen)
        # THE WRITER HOLDS ITS ROW TO THE SAME PREDICATE: a row the reader
        # would refuse is not written.
        self.fresh()
        self.assertFalse(hookwindow.note({
            "kind": "END", "stage": "event", "event": "NotAHook", "seat": "s",
            "outcome": "completed", "wall_ms": 1.0, "time_ns": now}))
        self.assertEqual([], self.rows())

    def test_the_verb_prints_the_rows_the_quantiles_and_the_raw_readings(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are the Stop row regex, the first and last row times, the timeout line with its readings, and the counts in the JSON
        now = time.time_ns()
        timeout = {"v": 1, "t": now - 60 * 10**9, "seat": "s", "ev": "Stop",
                   "st": "stop-guard", "ms": 20000.0, "out": "timeout",
                   "eid": "e", "sp": "a", "par": None, "bi": "x.py:1:f",
                   **CALM}
        self.plant(now, 2.5, extra=[timeout])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.main(["hooks", "latency", "--hours", "2"]))
        text = out.getvalue()
        self.assertRegex(text, r"\nStop +event +24 +24 +200\.0 +200\.0 +200\.0")
        first = now - (150 + 30 * 300) * 10**9
        self.assertIn("rows from %s to %s" % (hookwindow._iso(first),
                                             hookwindow._iso(now - 60 * 10**9)),
                      text)
        self.assertIn("load1=3.0 cpus=8 psi_cpu_some_avg10=5.0  x.py:1:f", text)
        # IT JUDGES NOTHING: no bar, no verdict, no label.
        for word in ("MET", "BAR", "BOX", "HOOK timeout", "UNATTRIBUTED"):
            self.assertNotIn(word, text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.main(["hooks", "latency", "--hours", "2",
                                          "--json"]))
        got = json.loads(out.getvalue())
        self.assertEqual((32, 25, 0, first), (got["rows"], got["rows_in_window"],
                                              got["unreadable_lines"],
                                              got["first_row_ns"]))
        self.assertEqual(["stop-guard"], [i["stage"] for i in got["timeouts"]])

    def test_the_verb_refuses_what_it_cannot_answer(self):  # noqa: VACUOUS_ASSERTION — each of the seven fixed argv lists asserts rc 2; the empty root is the no-write absence, and the print arm above is its positive control through the same verb
        for argv in (["--hours"], ["--hours", "0"], ["--hours", "nan"],
                     ["--hours", "inf"], ["--hours", "two"],
                     ["--hours", "2", "--since", "2026-09-28T00:00:00Z"],
                     ["--hours", "2", "--bogus"]):
            with self.subTest(argv=argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(2, cli.main(["hooks", "latency"] + argv))
        self.assertEqual([], list(self.root.iterdir()),
                         "a refused or read-only verb created state")

    def test_the_writer_never_raises_and_a_busy_lock_gives_the_row_up(self):
        end = {"kind": "END", "stage": "event", "event": "Stop", "seat": "s",
               "outcome": "completed", "wall_ms": 1.0,
               "time_ns": 1_800_000_000 * 10**9}
        self.assertTrue(hookwindow.note(dict(end)))          # control
        with open(hookwindow.path() + ".lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertFalse(hookwindow.note(dict(end)))
        self.assertEqual(1, len(self.rows()))
        with patch.object(hookwindow, "_append",
                          side_effect=OSError("disk full")), \
                patch.object(hookwindow, "read_box",
                             side_effect=OSError("no proc")):
            self.assertFalse(hookwindow.note(dict(end)))
            with latency.event_scope("standalone", "Stop"), \
                    latency.stage("stop-guard"):
                pass                    # the hook itself is untouched
        # BOTH SPANS of the hook, the event and its stop-guard, completed.
        self.assertEqual(2, latency.report()["counts"]["completed"])
        self.assertEqual(1, len(self.rows()))


if __name__ == "__main__":
    unittest.main()
