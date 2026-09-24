"""Real installed CLI/scope dispatch on fake payloads and mutation owners."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import builtins
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from helm import (cli, hooks, record, hookrun, hookoutcome, posttoolrun,
                  toolwhisper, seats, seats_cli, seats_rename, hooklatency)
from helm.inject import _ledger


def _capture_append(rows, row):
    """Own persistence only; copy actual runtime rows, never synthesize them.

    Real append/rotation/read-only persistence is covered by test_hooklatency.
    This fixture keeps every unresolved open/network/process tripwire armed.
    """
    rows.append(deepcopy(row))
    return True


def _assert_event_capture(rows):
    assert rows, "telemetry capture is empty"
    assert all(hooklatency._valid(row) for row in rows), "invalid captured telemetry row"
    event = [row for row in rows if row["stage"] == "event"]
    assert [row["kind"] for row in event] == ["START", "END"], "missing event registration or terminal"
    start, end = event
    assert all(start[key] == end[key] for key in
               ("event_id", "span_id", "parent_id", "writer", "seat", "mode", "observer")), "uncorrelated event terminal"
    assert start["parent_id"] is None and start["mode"] == "composite"
    assert all(row["event_id"] == start["event_id"] for row in rows), "foreign captured event"
    assert end["sequence"] > start["sequence"]
    assert not hooklatency.active(), "telemetry context leaked"
    assert hooklatency.entry_allowed(), "dispatch context leaked"


class PosttoolScopeTest(unittest.TestCase):
    def _snapshot(self):
        return {"env": dict(os.environ), "streams": (sys.stdin, sys.stdout, sys.stderr),
                "argv": list(sys.argv), "cwd": os.getcwd(),
                "fds": [(fcntl.fcntl(fd, fcntl.F_GETFD), fcntl.fcntl(fd, fcntl.F_GETFL))
                        for fd in (0, 1, 2)],
                "handler": signal.getsignal(signal.SIGALRM),
                "timer": signal.getitimer(signal.ITIMER_REAL), "at": time.monotonic()}

    def _assert_restored(self, before):
        after = self._snapshot()
        for key in ("env", "argv", "cwd", "fds", "handler"):
            self.assertEqual(after[key], before[key], key)
        for actual, expected in zip(after["streams"], before["streams"]):
            self.assertIs(actual, expected)
        self.assertEqual(after["timer"][1], before["timer"][1])
        elapsed = after["at"] - before["at"]
        self.assertGreaterEqual(after["timer"][0], before["timer"][0] - elapsed - 0.05)
        self.assertLessEqual(after["timer"][0], before["timer"][0] + 0.05)

    def _dispatch(self, args, payload, active_timer=False):
        """Keep real CLI; assert state BEFORE removing test patches/FD capture."""
        denied = []

        def refuse(owner):
            def fail(*args, **kwargs):
                denied.append(owner)
                raise AssertionError("unresolved I/O owner: " + owner)
            return fail

        reads = []

        class Input(io.TextIOWrapper):
            def read(self, *args):
                result = super().read(*args)
                reads.append(result)
                return result

        self.input_reads = reads
        with tempfile.TemporaryFile() as output, ExitStack() as stack:
            incoming = Input(io.BytesIO(payload.encode("utf-8")), encoding="utf-8")
            text, errors = io.StringIO(), io.StringIO()
            stack.enter_context(mock.patch.object(sys, "stdin", incoming))
            stack.enter_context(mock.patch.object(sys, "stdout", text))
            stack.enter_context(mock.patch.object(sys, "stderr", errors))
            old_fd = os.dup(1)
            old_handler = signal.getsignal(signal.SIGALRM)
            old_timer = signal.getitimer(signal.ITIMER_REAL)
            alarms = []
            try:
                os.dup2(output.fileno(), 1)
                signal.signal(signal.SIGALRM, lambda *unused: alarms.append(True))
                signal.setitimer(signal.ITIMER_REAL, 30 if active_timer else 0,
                                 0.25 if active_timer else 0)
                before = self._snapshot()
                with ExitStack() as guard:
                    for obj, name in ((builtins, "open"), (os, "open"), (os, "listdir"),
                                      (os, "scandir"), (os, "system"),
                                      (subprocess, "Popen"), (socket.socket, "connect"),
                                      (socket.socket, "connect_ex")):
                        guard.enter_context(mock.patch.object(obj, name, side_effect=refuse(name)))
                    rc = cli.main(args)
                    # A swallowed guard denial is still a failing test, not a
                    # clean fail-open outcome. Check before any fixture cleanup.
                    self.assertEqual(denied, [])
                    if args == ["hooks", "run", "PostToolUse", "--installed", "--hook-json"]:
                        _assert_event_capture(self.telemetry_rows)
                    self.assertFalse(hooklatency.active())
                    self.assertTrue(hooklatency.entry_allowed())
                    self._assert_restored(before)
                    self.assertEqual(alarms, [])
                output.seek(0)
                return rc, output.read(), text.getvalue(), errors.getvalue()
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old_handler)
                signal.setitimer(signal.ITIMER_REAL, *old_timer)
                os.dup2(old_fd, 1)
                os.close(old_fd)
                incoming.close()

    @contextmanager
    def _owners(self, outside=False, failure=None, quiet=False):
        trace = {k: [] for k in ("stages", "scope", "outcomes", "text", "bytes",
                                "record", "prepare", "delivery", "commit", "order", "projects", "telemetry")}
        self.telemetry_rows = trace["telemetry"]
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            helm = Path(root) / "helm"
            cwd = Path(root) / ("other" if outside else "helm") / "work"
            cwd.mkdir(parents=True)
            (helm / "bin").mkdir(parents=True, exist_ok=True)
            before_cwd = os.getcwd()
            os.chdir(cwd)
            stack.callback(os.chdir, before_cwd)
            stack.enter_context(mock.patch.dict(os.environ, {
                "HOME": root, "HELM_HOME": root + "/state", "HELM_NO_TREE_WARNING": "1"}))
            event = {"session_id": "scope-session-範囲", "tool_name": "Edit", "cwd": str(cwd),
                     "tool_input": {"file_path": "écriture/範囲.py", "new_string": "naïve λ"},
                     "tool_response": {"stdout": "réussi 雪"}}

            def project(path):
                trace["projects"].append(path)
                return "helm" if Path(path).is_relative_to(helm) else "other"

            def record_io(data):
                trace["order"].append("record")
                trace["record"].append(deepcopy(data))
                if failure == "record":
                    raise ValueError("RECORDER_FAILURE_SENTINEL")

            def prepare_io(session):
                trace["order"].append("prepare")
                trace["prepare"].append((session, deepcopy(trace["record"])))
                if session and not quiet:
                    return {"session": session, "rule": "fixture-rule", "text": "WHISPER 範囲 λ"}
                return None

            def delivery_io(**kwargs):
                trace["order"].append("delivery")
                trace["delivery"].append({k: v for k, v in kwargs.items() if k != "emit"})
                if failure == "delivery":
                    raise ValueError("DELIVERY_FAILURE_SENTINEL")
                if kwargs["session"] and not quiet:
                    kwargs["emit"]("DELIVERY naïve 雪")
                    trace["order"].append("receipt")

            def commit_io(candidate):
                trace["order"].append("commit")
                trace["commit"].append(deepcopy(candidate))

            real_stage = posttoolrun._Event.stage
            real_scope = hooks.hook_skips_here
            real_parse = record.parse_event
            real_bytes = seats_cli._hook_stdin_plain

            def stage(obj, phase, spec, payload):
                trace["stages"].append((phase, deepcopy(spec), payload))
                result = real_stage(obj, phase, spec, payload)
                trace["outcomes"].append((phase, deepcopy(result)))
                return result

            def scope(verb, rest):
                result = real_scope(verb, rest)
                pair = posttoolrun.current()
                trace["scope"].append((pair.phase if pair else "outer", verb, tuple(rest), result))
                return result

            def parse(raw):
                trace["text"].append((posttoolrun.current().phase, raw))
                return real_parse(raw)

            def byte_read():
                trace["bytes"].append((posttoolrun.current().phase, sys.stdin.buffer.getvalue()))
                return real_bytes()

            for obj, name, replacement in (
                    (hooklatency, "append", lambda row: _capture_append(trace["telemetry"], row)),
                    (hooks, "helm_bin", lambda: str(helm / "bin/helm")),
                    (_ledger, "project_for_cwd", project),
                    (record, "_record", record_io),
                    (toolwhisper, "prepare_for_pair", prepare_io),
                    (toolwhisper, "commit_for_pair", commit_io),
                    (seats, "resolve_homing", lambda *a, **kw: ("main", "operator")),
                    (seats, "_assert_own_seat", lambda *a, **kw: ("fixture-seat", None)),
                    (seats_cli.actors, "grant_on_behalf", lambda *a, **kw: (None, None)),
                    (seats_rename, "recover_seat_rename", lambda: True),
                    (seats_cli, "_record_posttool_delegation", lambda event: None),
                    (seats_cli, "deliver_any", delivery_io),
                    (posttoolrun._Event, "stage", stage),
                    (hooks, "hook_skips_here", scope),
                    (record, "parse_event", parse),
                    (seats_cli, "_hook_stdin_plain", byte_read)):
                stack.enter_context(mock.patch.object(obj, name, replacement))
            yield trace, event
            self.assertIs(record._record, record_io)
            self.assertIsNone(posttoolrun.current())

    def _installed(self, payload, **kwargs):
        return self._dispatch(["hooks", "run", "PostToolUse", "--installed", "--hook-json"], payload, **kwargs)

    def _assert_dispatch(self, trace, payload, outside=False):
        self.assertEqual([p for p, _, _ in trace["stages"]], ["record", "prepare", "delivery"])
        self.assertEqual([s["timeout"] for _, s, _ in trace["stages"]], [10, 2, 2])
        self.assertEqual([raw for _, _, raw in trace["stages"]], [payload] * 3)
        self.assertEqual(self.input_reads, [payload])
        self.assertEqual(trace["text"], [("record", payload)])
        self.assertEqual(trace["bytes"], [] if outside else [
            ("prepare", payload.encode("utf-8")), ("delivery", payload.encode("utf-8"))])
        self.assertEqual([(p, verb, skipped) for p, verb, _, skipped in trace["scope"]], [
            ("outer", "hooks", False), ("record", "record", False),
            ("prepare", "chat", outside), ("delivery", "chat", outside)])
        self.assertEqual(len(trace["projects"]), 4)

    def test_in_scope_unicode_replay_and_real_handler_order(self):
        with self._owners() as (trace, event):
            payload = json.dumps(event, ensure_ascii=False)
            rc, raw, text, errors = self._installed(payload)
            self._assert_dispatch(trace, payload)
            self.assertEqual(trace["record"], [event])
            self.assertEqual(trace["prepare"], [(event["session_id"], [event])])
            self.assertEqual(trace["delivery"][0]["session"], event["session_id"])
            self.assertEqual(trace["delivery"][0]["cwd"], event["cwd"])
            self.assertEqual(trace["order"], ["record", "prepare", "delivery", "receipt", "commit"])
            self.assertEqual(trace["commit"][0]["session"], event["session_id"])
            context = json.loads(raw)["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(context, "WHISPER 範囲 λ\n\nDELIVERY naïve 雪")
            self.assertEqual([o["status"] for _, o in trace["outcomes"]], [hookoutcome.ANSWERED] * 3)
            self.assertEqual((rc, text, errors), (0, "", ""))

    def test_outside_scope_records_without_prepare_or_delivery(self):
        with self._owners(outside=True) as (trace, event):
            payload = json.dumps(event, ensure_ascii=False)
            rc, raw, text, errors = self._installed(payload)
            self._assert_dispatch(trace, payload, outside=True)
            self.assertEqual(trace["record"], [event])
            self.assertEqual(trace["order"], ["record"])
            for key in ("prepare", "delivery", "commit"):
                self.assertEqual(trace[key], [])
            self.assertEqual([o["status"] for _, o in trace["outcomes"]],
                             [hookoutcome.ANSWERED, hookoutcome.SKIPPED, hookoutcome.SKIPPED])
            self.assertTrue(all("scoped outside" in o["why"] for _, o in trace["outcomes"][1:]))
            self.assertEqual((raw, text, errors), (b"", "", ""))
            self.assertEqual(rc, 0)

    def test_bad_or_missing_payload_is_not_a_scope_skip(self):
        # A payload that is empty or does not parse names no thread, so
        # delivery spends nothing and says why on stderr, the channel the
        # harness keeps out of the model's context. `{}` parses.
        for payload, unread in (("", True), ("{broken 雪", True),
                                ("{}", False), ("[]", True)):
            with self.subTest(payload=payload), self._owners() as (trace, event):
                rc, raw, text, errors = self._installed(payload)
                self._assert_dispatch(trace, payload)
                self.assertEqual(trace["record"], [])
                self.assertEqual(trace["prepare"], [(None, [])])
                self.assertIsNone(trace["delivery"][0]["session"])
                self.assertIs(trace["delivery"][0]["sink_usable"] is False, unread)
                self.assertEqual(trace["commit"], [])
                self.assertEqual([o["status"] for _, o in trace["outcomes"]], [hookoutcome.ANSWERED] * 3)
                self.assertEqual((raw, text), (b"", ""))
                if unread:
                    self.assertIn("delivery skipped: the hook payload is "
                                  "unreadable", errors)
                else:
                    self.assertEqual(errors, "")
                self.assertEqual(rc, 0)

    def test_swallowed_recorder_failure_is_unchecked_not_skipped(self):
        with self._owners(failure="record") as (trace, event):
            payload = json.dumps(event, ensure_ascii=False)
            rc, raw, text, errors = self._installed(payload)
            self._assert_dispatch(trace, payload)
            self.assertEqual(trace["record"], [event])
            self.assertEqual(trace["order"], ["record", "prepare", "delivery", "receipt", "commit"])
            self.assertEqual(trace["outcomes"][0][1]["status"], hookoutcome.UNCHECKED)
            document = json.loads(raw)
            self.assertIn("RECORDER_FAILURE_SENTINEL", document["systemMessage"])
            self.assertIn("DELIVERY naïve 雪", document["hookSpecificOutput"]["additionalContext"])
            self.assertIn("RECORDER_FAILURE_SENTINEL", errors)
            self.assertEqual((rc, text), (0, ""))

    def test_swallowed_delivery_failure_does_not_publish_whisper(self):
        with self._owners(failure="delivery") as (trace, event):
            payload = json.dumps(event, ensure_ascii=False)
            rc, raw, text, errors = self._installed(payload)
            self._assert_dispatch(trace, payload)
            self.assertEqual(trace["order"], ["record", "prepare", "delivery"])
            self.assertEqual(trace["delivery"][0]["session"], event["session_id"])
            self.assertEqual(trace["commit"], [])
            self.assertEqual(trace["outcomes"][2][1]["status"], hookoutcome.UNCHECKED)
            self.assertIn("DELIVERY_FAILURE_SENTINEL", json.loads(raw)["systemMessage"])
            self.assertNotIn("WHISPER", raw.decode("utf-8"))
            self.assertIn("DELIVERY_FAILURE_SENTINEL", errors)
            self.assertEqual((rc, text), (0, ""))

    def test_healthy_quiet_payload_runs_all_handlers_without_output(self):
        with self._owners(quiet=True) as (trace, event):
            payload = json.dumps(event, ensure_ascii=False)
            rc, raw, text, errors = self._installed(payload)
            self._assert_dispatch(trace, payload)
            self.assertEqual(trace["record"], [event])
            self.assertEqual(trace["order"], ["record", "prepare", "delivery"])
            self.assertEqual(trace["commit"], [])
            self.assertEqual((raw, text, errors), (b"", "", ""))
            self.assertEqual(rc, 0)

    def test_existing_timer_refuses_input_without_stealing_timer(self):
        with self._owners() as (trace, event):
            payload = json.dumps(event, ensure_ascii=False)
            rc, raw, text, errors = self._installed(payload, active_timer=True)
            self.assertEqual(self.input_reads, [])
            self.assertEqual(trace["stages"], [])
            self.assertEqual(trace["record"], [])
            self.assertIn("existing alarm prevents", errors)
            self.assertEqual((raw, text), (b"", ""))
            self.assertEqual(rc, 0)

    def test_installed_parser_refuses_unsupported_population(self):
        cases = (
            ["PostToolUseFailure", "--installed", "--hook-json"],
            ["Stop", "--installed", "--hook-json"],
            ["PostToolUse", "--installed"],
            ["PostToolUse", "--installed", "--hook-json", "--tool", "Read"],
            ["PostToolUse", "--installed", "--hook-json", "--tool", ""],
            ["PostToolUse", "--installed", "--hook-json", "--unknown"],
            ["PostToolUse", "Stop", "--installed", "--hook-json"],
        )
        for tail in cases:
            with self.subTest(tail=tail), mock.patch.dict(os.environ, {"HELM_NO_TREE_WARNING": "1"}):
                scopes = []
                real_scope = hooks.hook_skips_here

                def observe(verb, rest):
                    result = real_scope(verb, rest)
                    scopes.append((verb, tuple(rest), result))
                    return result

                with mock.patch.object(hooks, "hook_skips_here", side_effect=observe):
                    rc, raw, text, errors = self._dispatch(["hooks", "run"] + tail, '{"sentinel":"範囲"}')
                self.assertEqual(scopes, [("hooks", tuple(["run"] + tail), False)])
                self.assertEqual(raw, b"")
                self.assertEqual(text, "")
                self.assertTrue(errors)
                self.assertEqual(rc, 2)
