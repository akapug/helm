#!/usr/bin/env python3
"""task/2924 — a SessionStart join killed at its deadline must not half-join.

MEASURED: a fresh pane's `helm chat join --hook-json` overran the 5s
`timeout` in bin/helm-hook. `timeout` sends SIGTERM and Python has no
handler for it, so the process died wherever it was. The roster row had been
written (it is the first write), the per-room cursor baseline had not, and
the banner that tells the seat to arm its beacon was never printed. The seat
showed `pending 138`: the whole room backlog, owed to a seat that had never
read any of it.

Two laws, one arm each, both swept over EVERY point the kill can land:

  * a rostered session always has a cursor in every room the join baselines,
    so a kill leaves either no row or a row whose cursors are set;
  * a banner the join could not print is owed, and the seat's next hook (the
    PostToolUse delivery, or a re-join) prints it once and only once.

The kill is modelled as a BaseException raised at a step boundary. That is
the faithful model of SIGTERM's default action for this question: the writes
already made stay on disk and nothing after the boundary runs. Every arm runs
in a temp HELM_HOME and chat dir; nothing reads the live roster.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (chat, posttoolrun, seats, seats_cli, seats_delivery,  # noqa: E402
                  seats_join, sessions)

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_LOG", "HELM_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "HELM_CHAT_OWNER_NAMES", "HELM_ADOPTED_DIR", "HELM_SEAT_STORAGE",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE",
            "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL",
            "ORCA_PANE_KEY", "CLAUDE_CONFIG_DIR")

SID = "12345678-2222-4333-8444-555555555555"
SEAT = "joiner"
ROOMS = ("main", "proj-a", "proj-b")
BEACON_DIRECTIVE = "First action: arm your beacon"


class _Killed(BaseException):
    """SIGTERM at a step boundary: not an Exception, so no `except Exception`
    on the join path can swallow it and carry on as if nothing happened."""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-joinatomic-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        prior = {k: os.environ.get(k) for k in ENV_KEYS}

        def restore():
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update({
            "HELM_CHAT_ROOM": "main",
            "HELM_CHAT_NODE_URL": "",
            "HELM_CHAT_OWNER_NAMES": "daria",
            "HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted"),
            "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "cfg-home")})
        os.makedirs(os.path.join(self.tmp, "cfg-home", "sessions"))
        homes = mock.patch.object(sessions, "cred_homes", return_value=[])
        homes.start()
        self.addCleanup(homes.stop)
        self.world("w0")

    def world(self, name):
        """A fresh helm home and chat dir under this test's tmp, with the
        backlog seeded. The env it sets is restored by setUp's cleanup."""
        os.environ["HELM_HOME"] = os.path.join(self.tmp, name, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, name, "chat")
        chat._ensure_dir()
        # THE BACKLOG THE LIVE SEAT WAS HANDED: rows addressed to everyone,
        # in every room, posted before this seat existed.
        for r in ROOMS:
            for i in range(3):
                chat.post("@all backlog %d in %s" % (i, r), room=r, who="bob")

    def join(self, **kw):
        with contextlib.redirect_stderr(io.StringIO()):
            return seats_join.join(session=SID, cwd=self.tmp, seat=SEAT,
                                   room="main", **kw)

    def rostered(self):
        return [name for name, row in seats.roster().items()
                if row.get("session") == SID]

    def uncursored(self, seat):
        """Rooms in which this session reads as never having looked."""
        return [r for r in ROOMS
                if seats_delivery._cursor(r, seat, SID) is None
                or seats_delivery._cursor(r, seat, SID, beacon=True) is None]

    @contextlib.contextmanager
    def killed_at(self, point):
        """Kill the join at one boundary: ('roster', 0) just after the roster
        write lands, or ('baseline', n) just after the n-th cursor init."""
        kind, n = point
        real_write, real_init = seats_join.write_roster, seats_join._init_cursor
        calls = [0]

        def write(*a, **k):
            out = real_write(*a, **k)
            if kind == "roster":
                raise _Killed()
            return out

        def init(*a, **k):
            out = real_init(*a, **k)
            calls[0] += 1
            if kind == "baseline" and calls[0] == n:
                raise _Killed()
            return out
        with mock.patch.object(seats_join, "write_roster", write), \
                mock.patch.object(seats_join, "_init_cursor", init):
            yield

    def interrupted_join(self, point=("roster", 0), **kw):
        with self.killed_at(point):
            try:
                self.join(**kw)
            except _Killed:
                return True
        return False


class AKilledJoinNeverLeavesARosteredSeatBlindTest(Base):

    def test_the_blindness_observable_sees_a_seat_that_never_joined(self):  # noqa: VACUOUS_ASSERTION — this IS the positive control: it asserts the reader returns every room, never an empty list
        # POSITIVE CONTROL for every `uncursored(...) == []` below: the same
        # reader, on a seat with no cursor anywhere, names every room.
        self.assertEqual(self.uncursored("seat-b"), list(ROOMS))

    def test_the_uninterrupted_join_rosters_and_cursors_every_room(self):
        # POSITIVE CONTROL on both observables the sweep reads: without a kill
        # the session is rostered and no room is blind, so a sweep point that
        # passes is passing on the law, not on an empty fixture.
        seat, line = self.join()
        self.assertEqual(self.rostered(), [SEAT])
        self.assertEqual(self.uncursored(seat), [])
        self.assertIn(BEACON_DIRECTIVE, line)

    def test_a_kill_just_after_the_roster_write_leaves_every_cursor_set(self):  # noqa: VACUOUS_ASSERTION — rostered() == [SEAT] is asserted first, and test_the_blindness_observable_sees_a_seat_that_never_joined controls the uncursored reader
        self.assertTrue(self.interrupted_join(("roster", 0)))
        self.assertEqual(self.rostered(), [SEAT],
                         "the kill landed after the roster write, so the row "
                         "must be there for this arm to measure anything")
        self.assertEqual(self.uncursored(SEAT), [],
                         "a rostered session with no cursor reads the whole "
                         "backlog as pending (the live 'pending 138')")

    def test_every_kill_point_leaves_no_row_or_a_row_with_every_cursor(self):  # noqa: VACUOUS_ASSERTION — the reached > len(ROOMS) assertion is unconditional, and the uncursored reader has its own positive control arm
        points = [("roster", 0)] + [("baseline", n) for n in range(1, 40)]
        reached = 0
        for point in points:
            with self.subTest(point=point):
                self.world("w-%s-%d" % point)
                if not self.interrupted_join(point):
                    continue        # the join finished before this point
                reached += 1
                if self.rostered():
                    self.assertEqual(self.uncursored(SEAT), [],
                                     "killed at %r: rostered but blind in "
                                     "some room" % (point,))
        self.assertGreater(reached, len(ROOMS),
                           "the sweep must actually land kills inside the "
                           "cursor loop, or it proves nothing about it")

    def test_a_rejoin_after_the_kill_is_idempotent(self):  # noqa: VACUOUS_ASSERTION — seat == SEAT, rostered() == [SEAT] and the banner assertIn are unconditional positives
        self.interrupted_join(("roster", 0))
        seat, line = self.join()
        self.assertEqual(seat, SEAT)
        self.assertEqual(self.rostered(), [SEAT])
        self.assertEqual(self.uncursored(SEAT), [])
        self.assertIn(BEACON_DIRECTIVE, line)


class TheBannerAKillSwallowedIsOwedTest(Base):
    """The banner is owed from the moment the join begins until a hook has
    actually written it out."""

    def hook(self, verb, pair=None):
        out = []
        payload = {"session_id": SID, "cwd": self.tmp, "source": "startup"}

        def emitter(event):
            return lambda line: out.append((event, line))
        token = posttoolrun._CURRENT.set(pair) if pair else None
        try:
            with mock.patch.object(seats_cli, "_hook_stdin",
                                   return_value=payload), \
                    mock.patch.object(seats_cli, "_hook_emit", emitter), \
                    contextlib.redirect_stderr(io.StringIO()):
                seats_cli.cmd(verb, ["--hook-json"] + (
                    ["--seat", SEAT] if verb == "join" else []))
        finally:
            if token is not None:
                posttoolrun._CURRENT.reset(token)
        return out

    def banners(self, out):
        return [line for _e, line in out if BEACON_DIRECTIVE in (line or "")]

    def test_a_completed_join_owes_nothing(self):  # noqa: VACUOUS_ASSERTION — the join banner count of 1 is an unconditional positive on the same banners() reader
        # CONTROL: the owed record exists only while a banner is unprinted.
        with mock.patch("helm.hooks.outside_helm", return_value=False):
            out = self.hook("join")
        self.assertEqual(len(self.banners(out)), 1)
        self.assertIsNone(seats_join.owed_join(SID))
        self.assertEqual(self.banners(self.hook("deliver")), [])

    def test_the_next_delivery_hook_prints_a_banner_the_kill_swallowed(self):  # noqa: VACUOUS_ASSERTION — owed_join is asserted NOT None first, and the first delivery must carry exactly one banner
        self.interrupted_join(("roster", 0), owe_banner=True)
        self.assertIsNotNone(seats_join.owed_join(SID),
                             "a join that never printed its banner owes it")
        out = self.hook("deliver")
        got = self.banners(out)
        self.assertEqual(len(got), 1, out)
        self.assertIn("seat '%s'" % SEAT, got[0])
        self.assertIsNone(seats_join.owed_join(SID))
        self.assertEqual(self.banners(self.hook("deliver")), [],
                         "an owed banner is paid once, not on every tool call")

    def test_a_kill_before_the_roster_write_is_healed_by_the_next_hook(self):  # noqa: VACUOUS_ASSERTION — the delivery must carry one banner and the row must appear, both unconditional
        # The kill lands inside the cursor loop, before the row exists. The
        # next hook replays the join, so the seat ends rostered AND sighted.
        self.assertTrue(self.interrupted_join(("baseline", 1),
                                              owe_banner=True))
        self.assertEqual(self.rostered(), [],
                         "the row is the join's LAST durable write")
        got = self.banners(self.hook("deliver"))
        self.assertEqual(len(got), 1)
        self.assertEqual(self.rostered(), [SEAT])
        self.assertEqual(self.uncursored(SEAT), [])

    def test_a_banner_whose_write_failed_stays_owed(self):
        self.interrupted_join(("roster", 0), owe_banner=True)

        def broken(event):
            def emit(line):
                raise OSError("stdout closed")
            return emit
        with mock.patch.object(seats_cli, "_hook_emit", broken), \
                mock.patch.object(seats_cli, "_hook_stdin", return_value={
                    "session_id": SID, "cwd": self.tmp}), \
                contextlib.redirect_stderr(io.StringIO()):
            seats_cli.cmd("deliver", ["--hook-json"])
        self.assertIsNotNone(seats_join.owed_join(SID),
                             "paid only after the write returns")
        self.assertEqual(len(self.banners(self.hook("deliver"))), 1)

    def test_the_installed_pair_runner_carries_the_banner_in_its_one_response(self):  # noqa: VACUOUS_ASSERTION — exactly one document and the directive in it are unconditional positives
        self.interrupted_join(("roster", 0), owe_banner=True)
        ev = posttoolrun._Event()
        ev.phase = "delivery"
        r, w = os.pipe()
        saved = os.dup(1)
        try:
            os.dup2(w, 1)
            self.hook("deliver", pair=ev)
        finally:
            os.dup2(saved, 1)
            os.close(saved)
            os.close(w)
        with os.fdopen(r) as f:
            docs = [json.loads(l) for l in f.read().splitlines() if l.strip()]
        self.assertEqual(len(docs), 1, "the pair runner speaks exactly once")
        ctx = docs[0]["hookSpecificOutput"]["additionalContext"]
        self.assertIn(BEACON_DIRECTIVE, ctx)
        self.assertIsNone(seats_join.owed_join(SID))

    def test_a_rejoin_after_an_interrupted_join_prints_the_banner_and_pays_it(self):  # noqa: VACUOUS_ASSERTION — the re-join must carry exactly one banner, an unconditional positive
        self.interrupted_join(("roster", 0), owe_banner=True)
        with mock.patch("helm.hooks.outside_helm", return_value=False):
            got = self.banners(self.hook("join"))
        self.assertEqual(len(got), 1)
        self.assertIsNone(seats_join.owed_join(SID))
        self.assertEqual(self.uncursored(SEAT), [])


# A REAL SUBAGENT PostToolUse PAYLOAD, captured from Claude Code 2.1.280
# (an isolated `claude -p` whose only hook dumped its stdin; one lead
# Bash call, then one Agent-tool subagent Bash call). The subagent's payload
# carried agent_id and agent_type beside the LEAD's session_id and cwd; the
# lead's own payloads carried neither key. Values other than the agent pair are
# this fixture's.
SUBAGENT_POSTTOOL = {"session_id": SID, "hook_event_name": "PostToolUse",
                     "agent_id": "a1f0afed187ccb124",
                     "agent_type": "general-purpose", "tool_name": "Bash",
                     "tool_use_id": "toolu_01XPuT4HAwsHVkH2u9xyVKxi"}


class ASubagentsHookLeavesTheLeadsRowsTest(Base):
    """task/2929. The PostToolUse delivery hook fires for an Agent-tool
    subagent under the lead's session, and it whispered the lead's addressed
    rows into the SUBAGENT and advanced the seat's cursor past them, so the
    lead never saw them."""

    ROW = "@%s the lead must read this" % SEAT

    def setUp(self):
        super().setUp()
        seat, _line = self.join()
        self.assertEqual(seat, SEAT)

    def hook(self, agent=False):
        out = []
        payload = dict(SUBAGENT_POSTTOOL, cwd=self.tmp) if agent else {
            "session_id": SID, "cwd": self.tmp,
            "hook_event_name": "PostToolUse", "tool_name": "Bash"}
        with mock.patch.object(seats_cli, "_hook_stdin",
                               return_value=payload), \
                mock.patch.object(seats_cli, "_hook_emit",
                                  lambda event: out.append), \
                contextlib.redirect_stderr(io.StringIO()):
            seats_cli.cmd("deliver", ["--hook-json"])
        return out

    def carries(self, out, text):
        return [line for line in out if text in (line or "")]

    def test_the_leads_payload_delivers_the_row_and_moves_its_cursor(self):  # noqa: VACUOUS_ASSERTION — the row must appear exactly once and the cursor must differ: both unconditional positives
        # POSITIVE CONTROL for both observables the subagent arm reads.
        chat.post(self.ROW, room="main", who="bob")
        before = seats_delivery._cursor("main", SEAT, SID)
        self.assertEqual(len(self.carries(self.hook(), self.ROW)), 1)
        self.assertNotEqual(seats_delivery._cursor("main", SEAT, SID), before)

    def test_a_subagents_payload_delivers_nothing_and_leaves_the_cursor(self):  # noqa: VACUOUS_ASSERTION — the lead's later hook must carry the row exactly once, an unconditional positive on the same reader
        chat.post(self.ROW, room="main", who="bob")
        before = seats_delivery._cursor("main", SEAT, SID)
        self.assertEqual(self.carries(self.hook(agent=True), self.ROW), [],
                         "a subagent read the lead's addressed row")
        self.assertEqual(seats_delivery._cursor("main", SEAT, SID), before,
                         "a subagent turn moved the seat's cursor")
        self.assertEqual(len(self.carries(self.hook(), self.ROW)), 1,
                         "the row must stay pending for the lead")

    def test_a_subagent_never_pays_the_leads_owed_banner(self):  # noqa: VACUOUS_ASSERTION — owed_join is asserted NOT None first, and the lead's hook must carry exactly one banner
        self.world("w-owed")
        self.interrupted_join(("roster", 0), owe_banner=True)
        self.assertIsNotNone(seats_join.owed_join(SID))
        self.assertEqual(self.carries(self.hook(agent=True),
                                      BEACON_DIRECTIVE), [],
                         "the banner is the lead's; a subagent was told to "
                         "arm the seat's beacon")
        self.assertIsNotNone(seats_join.owed_join(SID),
                             "a subagent's hook paid the lead's banner")
        self.assertEqual(len(self.carries(self.hook(), BEACON_DIRECTIVE)), 1)
        self.assertIsNone(seats_join.owed_join(SID))


if __name__ == "__main__":
    unittest.main()
