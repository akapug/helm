#!/usr/bin/env python3
"""The beacon-timeout rung (task/3404): a Monitor that arms an inbox beacon
runs for the harness's whole 30-minute cap, never less.

WHY. The harness kills a Monitor at its `timeout_ms`, 300000 (5 minutes) when
the call omits it, capped at 1800000. Every place helm tells a seat how to arm
its beacon renders `seats_advice.beacon_monitor`, which carries the cap, and a
local seat still armed 48 of its 152 beacons at 300000, 31 of them in one
hour. A 5-minute lease lapses six times as often, and each lapse is a chance
to end a turn with no beacon, which the stop guard now refuses once per fresh
stop for a local seat (task/3382). Advice had already been given at every
surface, so the call itself is refused and the refusal carries the corrected
call.

THE MATRIX. Refused: a beacon Monitor under the cap, and one with no
timeout_ms. A beacon is ONE simple command the shell runs: helm (by any path,
after assignments or `env`), then `chat wait`, with `--follow` or `--replace`
as a word of that same command. Passed, each the refusal's must-miss: a beacon
at the cap or over it, a Monitor that is not a beacon whatever its timeout (a
log tail, a gate waiter, a one-shot wait, a Monitor that only greps FOR the
beacon's words or echoes them), a Monitor whose words only RESEMBLE a beacon
(a path holding `chat` and `wait` under `tail --follow=name`, a one-shot wait
chained to an unrelated `tail --follow`), any input the rung cannot read (fail
open), and every Bash call. The corrected call carries the seat's command
whole at any width: json-quoted, never cut and never named. The hook arms
drive the SHIPPED entry, `cmd_argv_guard`, and one arm runs the real entry
script to prove the refusal adds no import a passing beacon does not load.

THE CALL IS A COMPLETE MONITOR INPUT (task/3435). It carries the characters a
seat typed, never a json escape of them (5 measured arms carried an em dash),
and it carries `description`, which the Monitor tool requires beside
`timeout_ms`: the seat's own when the refused call had one, the fixed one
otherwise, so a seat copying the call invents nothing. Each field reads back
through json as the value the seat must pass (tests/_monitorcall.py).
"""
import contextlib
import io
import json
import os as _os
import subprocess
import sys as _sys
import unittest
from unittest import mock

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-beacon-timeout-", var="HELM_HOME")

from helm import chat, seats_advice  # noqa: E402
from tests._monitorcall import last_call, monitor_input  # noqa: E402

SEAT = "zed"
CAP = seats_advice.BEACON_TIMEOUT_MS
BEACON = "helm chat wait --seat %s --follow" % SEAT
ROTATE = BEACON + " --replace"
# The spellings measured in the local seats' own Monitor calls: a lane's own
# helm after a cd, stderr folded in, and one with no --seat (the name comes
# from the seat's environment, and it arms a beacon all the same).
LANE = "cd /tmp/lane && ./bin/helm chat wait --seat %s --follow 2>&1" % SEAT
NAMELESS = "./bin/helm chat wait --follow 2>&1"
# The widest spelling the census measured: the seat's name in its own
# environment, a rotation, and the room's noise filtered out of the stream.
CENSUS = ('HELM_CHAT_NAME=%s HELM_CELL_PROFILE=%s helm chat wait --seat %s '
          '--follow --replace 2>&1 | /usr/bin/grep --line-buffered -v -E '
          '" TRIAGE |proxywatch"' % (SEAT, SEAT, SEAT))
# A beacon wider than any a seat types (the corrected call carries it whole):
# a quoted directory with a space, stderr folded, and a pattern holding both
# a double quote and a backslash, so its json literal escapes both.
WIDE = ('cd "/tmp/%s dir" && HELM_CHAT_NAME=%s ./bin/helm chat wait --seat %s '
        '--follow 2>&1 | /usr/bin/grep --line-buffered -v -E "a\\\\b|%s"'
        % ("d" * 180, SEAT, SEAT, "q" * 40))
# Beacons carrying characters outside ASCII, as seats type them: an em dash
# in a grep pattern (5 measured arms), and a character past the BMP, which a
# json escape splits into two surrogates.
FILTER = BEACON + " 2>&1 | /usr/bin/grep --line-buffered -v "
EMDASH = FILTER + "'proxywatch \u2014 idle'"
ASTRAL = FILTER + "'\U0001F4A4'"
# The description every `monitor()` input carries: a seat's OWN.
OWN = "seat inbox beacon"
# Monitors whose WORDS a beacon uses, where no one simple command is helm
# running `chat wait` with a beacon flag of its own: each must pass.
RESEMBLES = (
    # `chat` and `wait` are pieces of a path; --follow=name is tail's flag
    "tail --follow=name /tmp/chat/wait.log",
    # a one-shot wait (no beacon flag) chained to an unrelated follow
    "helm chat wait --seat x; tail --follow f",
    # echo prints its words; it runs none of them
    "echo helm chat wait --follow",
)


def monitor(command, **extra):
    """A Monitor tool_input as the harness sends it."""
    return dict({"command": command, "description": OWN}, **extra)


def call(command, description=OWN):
    """The corrected call the refusal must carry: the seat's own command and
    description, unchanged, at the cap."""
    return "Monitor(command: %s, description: %s, timeout_ms: %d)" % (
        json.dumps(command, ensure_ascii=False),
        json.dumps(description, ensure_ascii=False), CAP)


class Refusal(unittest.TestCase):
    """The predicate: (the timeout as given, the command) or None."""

    def test_a_beacon_under_the_cap_is_found_with_its_timeout(self):
        self.assertEqual(
            chat.beacon_timeout_refusal(monitor(BEACON, timeout_ms=300000)),
            (300000, BEACON, OWN))

    def test_a_beacon_with_no_timeout_is_found_as_omitted(self):
        self.assertEqual(chat.beacon_timeout_refusal(monitor(BEACON)),
                         (None, BEACON, OWN))
        self.assertEqual(
            chat.beacon_timeout_refusal(monitor(BEACON, timeout_ms=None)),
            (None, BEACON, OWN))

    def test_the_seats_own_description_is_found_and_nothing_else_is(self):  # noqa: VACUOUS_ASSERTION — the first assertion finds a non-None description on the same predicate, unconditionally; every arm of a finite tuple literal executes
        """task/3435: the corrected call reuses the description the refused
        call carried, verbatim. Only a string with a character to show is
        one: a missing, blank or non-string description is None, and the
        message falls to the fixed one."""
        own = "  zed wakes \u2014 inbox  "
        self.assertEqual(chat.beacon_timeout_refusal(
            monitor(BEACON, timeout_ms=300000, description=own)),
            (300000, BEACON, own))
        self.assertEqual(chat.beacon_timeout_refusal(
            {"command": BEACON, "timeout_ms": 300000}), (300000, BEACON, None))
        for description in (None, "", " \n\t", 7, ["x"], True):
            with self.subTest(description=description):
                self.assertEqual(chat.beacon_timeout_refusal(
                    monitor(BEACON, timeout_ms=300000,
                            description=description)),
                    (300000, BEACON, None))

    def test_every_measured_spelling_is_a_beacon(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts an exact non-None tuple
        for command in (BEACON, ROTATE, LANE, NAMELESS, CENSUS, WIDE,
                        "HELM_CHAT_NAME=%s helm chat wait --seat %s --follow"
                        % (SEAT, SEAT),
                        # a room tap arms a beacon too, and is ruled under
                        # the cap like any other
                        "helm chat wait --any --follow",
                        "env HELM_CHAT_NAME=%s /opt/h/bin/helm chat wait "
                        "--seat %s --follow" % (SEAT, SEAT),
                        'helm chat wait --seat "$SEAT" --follow'):
            with self.subTest(command=command):
                self.assertEqual(
                    chat.beacon_timeout_refusal(
                        monitor(command, timeout_ms=600000)),
                    (600000, command, OWN))

    def test_a_beacon_at_or_over_the_cap_passes(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal of the same command under the cap
        self.assertIsNotNone(chat.beacon_timeout_refusal(
            monitor(BEACON, timeout_ms=CAP - 1)))
        for ms in (CAP, float(CAP), 3600000):
            with self.subTest(ms=ms):
                self.assertIsNone(chat.beacon_timeout_refusal(
                    monitor(BEACON, timeout_ms=ms)))

    def test_a_monitor_that_is_not_a_beacon_passes_at_any_timeout(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal of a beacon at the same timeout
        self.assertIsNotNone(chat.beacon_timeout_refusal(
            monitor(BEACON, timeout_ms=300000)))
        for command in (
                "tail -f /tmp/app.log | grep --line-buffered ERROR",
                "tail --follow=name /tmp/app.log",
                "fab gate --join k1 --generation 3 --repo /tmp/r",
                "fab tail hub 12345",
                # a one-shot delivery registers no beacon
                "helm chat wait --seat %s" % SEAT,
                # these only NAME the beacon's words: a grep pattern and an
                # echo argument are data the shell hands to programs that
                # never run them
                "tail -f /tmp/app.log | grep --line-buffered '%s'" % BEACON,
                "echo '%s'" % BEACON,
                'grep -E "chat wait --follow" /tmp/x.log',
                # a one-shot wait beside a tail whose flag is not a beacon's
                "helm chat wait --seat x && tail -f /tmp/log"):
            for extra in ({"timeout_ms": 300000}, {}, {"timeout_ms": 5000}):
                with self.subTest(command=command, extra=extra):
                    self.assertIsNone(chat.beacon_timeout_refusal(
                        monitor(command, **extra)))

    def test_a_beacon_is_one_helm_invocation_not_its_words_anywhere(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal of a beacon at the same timeout; every arm of a finite tuple literal executes
        """Codex's F1: the words `chat`, `wait` and `--follow` standing
        anywhere in the command are not a beacon. Each of these was refused
        while the rung read the sidechain rung's presence of the three."""
        self.assertIsNotNone(chat.beacon_timeout_refusal(
            monitor(BEACON, timeout_ms=300000)))
        for command in RESEMBLES:
            for extra in ({"timeout_ms": 300000}, {}):
                with self.subTest(command=command, extra=extra):
                    self.assertIsNone(chat.beacon_timeout_refusal(
                        monitor(command, **extra)))

    def test_text_the_shell_reader_cannot_settle_passes(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal of the same beacon spelled so the reader settles it
        """A miss here costs only a short lease, so a spelling the reader
        cannot read the way bash does fails OPEN: an unclosed quote."""
        self.assertIsNotNone(chat.beacon_timeout_refusal(
            monitor(BEACON + " 'x'", timeout_ms=300000)))
        self.assertIsNone(chat.beacon_timeout_refusal(
            monitor(BEACON + " 'x", timeout_ms=300000)))

    def test_input_it_cannot_read_passes(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal of a readable beacon at the same timeout
        self.assertIsNotNone(chat.beacon_timeout_refusal(
            monitor(BEACON, timeout_ms=300000)))
        for tool_input in (None, "text", [BEACON], 7,
                           {"description": "no command at all"},
                           {"command": None, "timeout_ms": 300000},
                           {"command": [BEACON], "timeout_ms": 300000},
                           {"ws": {"url": "wss://x"}, "timeout_ms": 300000},
                           monitor(BEACON, timeout_ms="300000"),
                           monitor(BEACON, timeout_ms=True),
                           monitor(BEACON, timeout_ms=[300000]),
                           monitor(BEACON, timeout_ms=float("nan"))):
            with self.subTest(tool_input=tool_input):
                self.assertIsNone(chat.beacon_timeout_refusal(tool_input))


class Message(unittest.TestCase):
    """The refusal's words: what it found, and the corrected call whole."""

    def test_the_canonical_beacon_gets_the_one_arming_instruction(self):
        """With no description of its own, the canonical command's corrected
        call IS the advice, byte for byte: one home, one fixed description."""
        text = chat.beacon_timeout_message((300000, BEACON, None))
        self.assertTrue(text.startswith("[helm argv-guard] BLOCKED: "), text)
        self.assertIn("timeout_ms 300000", text)
        self.assertEqual(last_call(text), seats_advice.beacon_monitor(SEAT))
        self.assertEqual(
            last_call(chat.beacon_timeout_message((300000, ROTATE, None))),
            seats_advice.beacon_monitor(SEAT, replace=True))

    def test_an_omitted_timeout_names_the_harness_default(self):
        text = chat.beacon_timeout_message((None, BEACON, OWN))
        self.assertIn("no timeout_ms", text)
        self.assertIn("300000", text)
        self.assertIn(call(BEACON), text)

    def test_the_seats_own_command_is_carried_unchanged(self):
        self.assertIn(call(LANE),
                      chat.beacon_timeout_message((600000, LANE, OWN)))
        quoted = 'cd "/tmp/a b" && helm chat wait --seat %s --follow' % SEAT
        self.assertIn(call(quoted),
                      chat.beacon_timeout_message((600000, quoted, OWN)))

    def test_a_non_ascii_command_is_carried_byte_for_byte(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts the command's own characters PRESENT before the escape's absence
        """task/3435, meta-claude's read: the json literal escaped every
        character outside ASCII, so 5 measured arms carrying an em dash were
        handed back a command that round-trips through json but is not the
        text the seat typed. The call carries the characters themselves, and
        each field still reads back through json as the seat's own."""
        for command in (EMDASH, ASTRAL):
            for description in (None, "zed \u2014 inbox \U0001F514"):
                with self.subTest(command=command, description=description):
                    text = chat.beacon_timeout_message(
                        (300000, command, description))
                    self.assertIn('"%s"' % command, text)
                    self.assertNotIn("\\u", text)
                    got = monitor_input(last_call(text))
                    self.assertEqual(got["command"], command)
                    self.assertEqual(got["description"], description
                                     or seats_advice.BEACON_DESCRIPTION)

    def test_the_corrected_call_is_a_complete_monitor_input(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts an exact non-empty dict
        """task/3435: the Monitor tool REQUIRES description beside
        timeout_ms, and a corrected call without one makes the seat invent
        it. The call carries the seat's own description when the refused
        call had one (quotes, a backslash, a newline and a percent sign
        included), the fixed one otherwise, and reads back field for field
        as the whole input the tool takes, on one line."""
        wide = 'zed "inbox" \\ wakes\n\u2014 at 100%% %s' % ("w" * 300)
        for given in (None, 300000):
            for description, expect in (
                    (OWN, OWN), (wide, wide),
                    (None, seats_advice.BEACON_DESCRIPTION)):
                with self.subTest(given=given, description=expect[:12]):
                    text = chat.beacon_timeout_message(
                        (given, LANE, description))
                    self.assertEqual(
                        monitor_input(last_call(text)),
                        {"command": LANE, "description": expect,
                         "timeout_ms": CAP})

    def test_a_wide_command_is_carried_whole_and_round_trips(self):  # noqa: VACUOUS_ASSERTION — the placeholder's absence is read on the same text whose whole call is asserted present first, unconditionally
        """Codex's F2: a placeholder in the corrected call is no call at all.
        The refusal carries the command at ANY width, and the command field
        of the call it prints reads back, through json, as the seat's own."""
        self.assertGreater(len(WIDE), 300)
        text = chat.beacon_timeout_message((600000, WIDE, OWN))
        self.assertIn(call(WIDE), text)
        self.assertNotIn("<the same command>", text)
        for given in (None, 300000):
            with self.subTest(given=given):
                text = chat.beacon_timeout_message((given, WIDE, OWN))
                self.assertIn(call(WIDE), text)
                self.assertEqual(monitor_input(last_call(text)),
                                 {"command": WIDE, "description": OWN,
                                  "timeout_ms": CAP})


class HookTest(unittest.TestCase):
    """The shipped entry: exit 2 and the corrected call on a refusal, exit 0
    on every must-miss."""

    def hook(self, tool="Monitor", agent=None, raw=None, **tool_input):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "session_id": "sess-beacon-timeout", "cwd": "/tmp",
                   "tool_use_id": "toolu_beacon", "tool_input": tool_input}
        if agent:
            payload["agent_id"] = agent
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(_sys, "stdin", io.StringIO(
                    raw if raw is not None else json.dumps(payload))), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue() + err.getvalue()

    def test_a_five_minute_beacon_is_refused_with_the_corrected_call(self):
        rc, out = self.hook(**monitor(BEACON, timeout_ms=300000))
        self.assertEqual(rc, 2, out)
        self.assertIn("[helm argv-guard] BLOCKED", out)
        self.assertIn("timeout_ms 300000", out)
        self.assertIn(call(BEACON), out)

    def test_a_beacon_with_no_timeout_is_refused(self):
        rc, out = self.hook(**monitor(ROTATE))
        self.assertEqual(rc, 2, out)
        self.assertIn("no timeout_ms", out)
        self.assertIn(call(ROTATE), out)

    def test_a_call_with_no_description_is_handed_the_advice_whole(self):
        """task/3435: nothing of the seat's to reuse, so the corrected call
        is the one arming instruction, the fixed description included."""
        rc, out = self.hook(command=BEACON, timeout_ms=300000)
        self.assertEqual(rc, 2, out)
        self.assertEqual(last_call(out.rstrip("\n")),
                         seats_advice.beacon_monitor(SEAT))

    def test_an_em_dash_beacon_is_refused_with_its_command_byte_for_byte(self):
        """task/3435 through the shipped entry, fed as the harness feeds it:
        raw UTF-8 text, the em dash unescaped."""
        raw = json.dumps({"hook_event_name": "PreToolUse",
                          "tool_name": "Monitor", "cwd": "/tmp",
                          "session_id": "sess-beacon-timeout",
                          "tool_use_id": "toolu_beacon",
                          "tool_input": monitor(EMDASH, timeout_ms=300000)},
                         ensure_ascii=False)
        self.assertIn("\u2014", raw)
        rc, out = self.hook(raw=raw)
        self.assertEqual(rc, 2, out)
        self.assertIn(call(EMDASH), out)
        self.assertNotIn("\\u2014", out)
        self.assertEqual(monitor_input(last_call(out.rstrip("\n"))),
                         {"command": EMDASH, "description": OWN,
                          "timeout_ms": CAP})

    def test_a_lane_beacon_under_the_cap_is_refused_with_its_own_command(self):
        rc, out = self.hook(**monitor(LANE, timeout_ms=600000))
        self.assertEqual(rc, 2, out)
        self.assertIn(call(LANE), out)

    def test_the_census_spellings_are_refused_with_their_own_command(self):
        rc, out = self.hook(**monitor(CENSUS))
        self.assertEqual(rc, 2, out)
        self.assertIn(call(CENSUS), out)
        for command in (CENSUS, WIDE, "helm chat wait --any --follow"):
            with self.subTest(command=command):
                rc, out = self.hook(**monitor(command, timeout_ms=300000))
                self.assertEqual(rc, 2, out)
                self.assertIn(call(command), out)

    def test_a_monitor_that_only_resembles_a_beacon_passes(self):  # noqa: VACUOUS_ASSERTION — the refusal arms above drive the same entry to rc 2; every arm of a finite tuple literal executes
        for command in RESEMBLES:
            for extra in ({"timeout_ms": 300000}, {}):
                with self.subTest(command=command, extra=extra):
                    rc, out = self.hook(**monitor(command, **extra))
                    self.assertEqual(rc, 0, out)
                    self.assertNotIn("BLOCKED", out)

    def test_a_beacon_at_the_cap_passes(self):  # noqa: VACUOUS_ASSERTION — the refusal arms above drive the same entry with the same command to rc 2
        rc, out = self.hook(**monitor(BEACON, timeout_ms=CAP))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("BLOCKED", out)

    def test_a_monitor_that_is_not_a_beacon_passes(self):  # noqa: VACUOUS_ASSERTION — the refusal arms above drive the same entry to rc 2; every arm of a finite tuple literal executes
        for command in ("tail -f /tmp/app.log | grep --line-buffered ERROR",
                        "fab gate --join k1 --generation 3 --repo /tmp/r",
                        "tail -f /tmp/app.log | grep --line-buffered '%s'"
                        % BEACON):
            for extra in ({"timeout_ms": 300000}, {}):
                with self.subTest(command=command, extra=extra):
                    rc, out = self.hook(**monitor(command, **extra))
                    self.assertEqual(rc, 0, out)
                    self.assertNotIn("BLOCKED", out)

    def test_an_unreadable_payload_passes(self):  # noqa: VACUOUS_ASSERTION — the refusal arms above drive the same entry to rc 2
        for raw in ("not json", json.dumps([1, 2]),
                    json.dumps({"tool_name": "Monitor",
                                "tool_input": "helm chat wait --follow"})):
            with self.subTest(raw=raw):
                rc, out = self.hook(raw=raw)
                self.assertEqual(rc, 0, out)
        rc, out = self.hook(**monitor(BEACON, timeout_ms="300000"))
        self.assertEqual(rc, 0, out)

    def test_a_reader_defect_fails_open(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal of the same call with the reader intact
        self.assertEqual(self.hook(**monitor(BEACON, timeout_ms=300000))[0], 2)
        with mock.patch.object(chat, "beacon_timeout_refusal",
                               side_effect=RuntimeError("defect")):
            rc, out = self.hook(**monitor(BEACON, timeout_ms=300000))
        self.assertEqual(rc, 0, out)

    def test_a_bash_call_is_untouched(self):  # noqa: VACUOUS_ASSERTION — the Monitor refusal arms above drive the same entry with the same command to rc 2
        for extra in ({"run_in_background": True}, {"timeout": 300000},
                      {"timeout_ms": 300000}):
            with self.subTest(extra=extra):
                rc, out = self.hook("Bash", command=BEACON, **extra)
                self.assertEqual(rc, 0, out)
                self.assertNotIn("BLOCKED", out)

    def test_a_subagent_hears_the_sidechain_refusal_not_this_one(self):
        """A subagent may not arm its seat's beacon at any timeout, so this
        rung never tells it how to arm one."""
        rc, out = self.hook(agent="agent-7", **monitor(BEACON,
                                                       timeout_ms=300000))
        self.assertEqual(rc, 2, out)
        self.assertIn("BLOCKED", out)
        self.assertNotIn("timeout_ms 300000", out)
        self.assertNotIn("Monitor(command:", out)


class EntryImportTest(unittest.TestCase):
    """THE HOOK BUDGET, bound structurally as HookEntryBudgetTest binds it: a
    refused beacon loads exactly the modules a passing beacon loads, and never
    the delivery machinery."""

    ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

    def run_hook(self, tool_input):
        probe = (
            "import json, sys, runpy\n"
            "sys.argv = ['helm', 'chat', 'argv-guard', '--hook-json']\n"
            "rc = 0\n"
            "try:\n"
            "    runpy.run_path(%r, run_name='__main__')\n"
            "except SystemExit as e:\n"
            "    rc = e.code or 0\n"
            "sys.stderr.write('PROBE ' + json.dumps({'rc': rc, 'mods': "
            "sorted(sys.modules)}) + '\\n')\n"
            % _os.path.join(self.ROOT, "bin", "helm"))
        payload = json.dumps({"tool_name": "Monitor", "cwd": "/tmp",
                              "session_id": "beacon-timeout-arm",
                              "tool_input": tool_input})
        p = subprocess.run([_sys.executable, "-c", probe], input=payload,
                           capture_output=True, text=True,
                           env=dict(_os.environ, HELM_NO_TREE_WARNING="1"))
        line = [l for l in p.stderr.splitlines() if l.startswith("PROBE ")]
        self.assertTrue(line, "the probe never reported: " + p.stderr[-800:])
        report = json.loads(line[-1][len("PROBE "):])
        return report["rc"], set(report["mods"]), p.stderr

    def test_a_refused_beacon_imports_nothing_a_passing_one_does_not(self):
        rc, refused, err = self.run_hook(monitor(BEACON, timeout_ms=300000))
        self.assertEqual(rc, 2, err[-600:])
        self.assertIn("timeout_ms 300000", err)
        rc, passed, err = self.run_hook(monitor(BEACON, timeout_ms=CAP))
        self.assertEqual(rc, 0, err[-600:])
        self.assertIn("helm.chat", passed)
        self.assertEqual(sorted(refused - passed), [])
        self.assertNotIn("helm.seats", refused)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
