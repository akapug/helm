#!/usr/bin/env python3
"""task/4019 slice C: fewer model requests per wake, with no lost obligation.

(a) THE STOP-GUARD CHECK, SAID EARLY. A stop-guard block is one more
    full-context request after the agent chose to stop. The owed-row check
    now speaks at the tool boundary where a row first becomes owed, once per
    new row (helm/stop_early.py), and the stop still refuses that row.
(b) A SEAT RECOVERY NOTICE IS FYI. seat-events' "<family> recovered" row
    needs no act from its steward; it is held for the steward's next tool
    boundary, never a wake. The dark/wall row stays a wake.
(c) AN ALARM LEADS WITH WHO IT WAKES AND WHY. A wake line is clipped to 200
    bytes, so mentions at the end of a long alarm were cut off its reader's
    line. scratch-gc alarms now lead with their mentions, and any delivery
    line whose only mention of its seat is past the cut leads with it.

THE FIDELITY CONTRACT, held by an arm each: an ACT row still wakes in the
same beacon pass; owner, deadline and failure rows are never held; an
unclassifiable row wakes; and nothing is dropped (every held row is returned
by the next boundary delivery).
"""
import contextlib
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_seats import SeatsBase  # noqa: E402

from helm import (chat, dispatches, needs_act, posttoolrun, scratch,  # noqa: E402
                  seatceiling, seatevents, seats, stop_early, toolwhisper)

SEAT_A, SEAT_B, SEAT_C = "seat-a", "seat-b", "seat-c"
SID_A = "s-seat-a"
RECOVERED = "%s gemini recovered HEALTHY — since t0" % seatevents.FYI_LEAD


def _ts(age_s):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_s))


@contextlib.contextmanager
def _fd1():
    """Capture what is written to fd 1 (the hook's one-write emit law)."""
    r, w = os.pipe()
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(w, 1)
    os.close(w)
    got = []
    try:
        yield got
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
        chunks = []
        while True:
            b = os.read(r, 65536)
            if not b:
                break
            chunks.append(b)
        os.close(r)
        got.append(b"".join(chunks).decode("utf-8"))


class _World(SeatsBase):
    def setUp(self):
        super().setUp()
        # These arms drive the real Stop hook: the scratch reaper must be off
        # (SeatsBase sets it), or a run would delete real dead-session scratch.
        self.assertEqual(os.environ.get("HELM_SCRATCH_GC"), "0")
        seats.join(session="s-seat-b", seat=SEAT_B, cwd="/tmp/p")
        seats.join(session=SID_A, seat=SEAT_A, cwd="/tmp/p")

    def plant(self, rid, lane, age_s=3600):
        """One OPEN review row for seat-a, sent by seat-b, delivered."""
        ts = _ts(age_s)
        rows = ({"v": 3, "event": "dispatch", "seq": 0, "id": rid, "ts": ts,
                 "recipient": SEAT_A, "lane": lane, "tip": "a" * 40,
                 "ref": "a" * 40, "deadline_s": 2700, "status": "open",
                 "kind": "review", "sender": SEAT_B},
                {"v": 3, "event": "delivered", "seq": 1, "id": rid, "ts": ts,
                 "delivery_ref": "dm-x"})
        path = dispatches.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return rid[:12]

    def beacon(self, seat=SEAT_A, session=SID_A):
        """One `wait --follow` pass: every line is one wake."""
        captured = []
        self.assertIsNone(seats.wait(seat=seat, session=session, follow=True,
                                     timeout=0.15, poll=0.01,
                                     emit=captured.append))
        return captured

    def stop(self):
        return self.cmd("stop-guard", ["--hook-json", "--seat", SEAT_A],
                        stdin=json.dumps({"session_id": SID_A}).encode())

    def pair_prepare(self, payload=None):
        """The installed pair's preparatory pass, through seats_cli."""
        event = posttoolrun._Event()
        event.phase = "prepare"
        token = posttoolrun._CURRENT.set(event)
        try:
            self.cmd_fd("deliver", ["--hook-json", "--room", "main"],
                        stdin=json.dumps(payload or {"session_id": SID_A})
                        .encode())
        finally:
            posttoolrun._CURRENT.reset(token)
        return event


# --------------------------------------------------------------- (a) early

class EarlyOwedWhisperTest(_World):

    def test_an_owed_row_is_said_at_the_tool_boundary_once_per_new_row(self):
        first = self.plant("aaaa1111bbbb2222", "lane-one")
        got = stop_early.prepare(SID_A, SEAT_A)
        self.assertIsNotNone(got, "a seat owing a row heard nothing early")
        self.assertIn("you OWE 1 dispatch row", got["text"])
        self.assertIn("helm dispatch triage %s" % first, got["text"])
        toolwhisper.commit_for_pair(got)
        self.assertIsNone(stop_early.prepare(SID_A, SEAT_A),
                          "the same owed set was said twice")
        second = self.plant("cccc3333dddd4444", "lane-two", age_s=60)
        again = stop_early.prepare(SID_A, SEAT_A)
        self.assertIsNotNone(again, "a NEW owed row was not said")
        self.assertIn("you OWE 2 dispatch rows", again["text"])
        self.assertIn(second, again["text"])

    def test_the_installed_pair_publishes_it_and_latches_it(self):  # noqa: VACUOUS_ASSERTION — the first prepare's candidate and the published line are the unconditional positive controls on the same pair
        row = self.plant("eeee5555ffff6666", "lane-pair")
        event = self.pair_prepare()
        self.assertIsNotNone(event.candidate, "the pair prepared nothing")
        event.delivery_allowed = True
        with _fd1() as out:
            event.emit()
        doc = json.loads(out[0])
        said = doc["hookSpecificOutput"]["additionalContext"]
        self.assertIn("helm dispatch triage %s" % row, said)
        event.commit_published()
        self.assertIsNone(self.pair_prepare().candidate,
                          "a published, latched line was prepared again")

    def test_the_standalone_hook_says_it_too(self):
        row = self.plant("abab1212cdcd3434", "lane-solo")
        rc, out = self.cmd_fd("deliver", ["--hook-json"],
                              stdin=json.dumps({"session_id": SID_A}).encode())
        self.assertEqual(rc, 0)
        self.assertIn("helm dispatch triage %s" % row, out)

    def test_a_subagent_boundary_and_the_switch_say_nothing(self):
        self.plant("1234abcd5678ef90", "lane-sub")
        self.assertIsNotNone(stop_early.prepare(SID_A, SEAT_A),
                             "control: the row is owed and unsaid")
        self.assertIsNone(stop_early.prepare(SID_A, SEAT_A, "agent-1"))
        self.assertIsNone(self.pair_prepare(
            {"session_id": SID_A, "agent_id": "agent-1",
             "agent_type": "general-purpose"}).candidate)
        with mock.patch.dict(os.environ, {"HELM_OWED_EARLY": "0"}):
            self.assertIsNone(stop_early.prepare(SID_A, SEAT_A))

    def test_nothing_owed_says_nothing(self):  # noqa: VACUOUS_ASSERTION — the planted row's prepare at the end is the unconditional positive control on the same call
        self.assertIsNone(stop_early.prepare(SID_A, SEAT_A))
        self.assertIsNone(self.pair_prepare().candidate)
        self.plant("0f0f0f0f1e1e1e1e", "lane-control")
        self.assertIsNotNone(stop_early.prepare(SID_A, SEAT_A),
                             "control: the same seat owing a row hears it")

    def test_the_stop_still_refuses_a_row_said_early(self):
        """Fidelity: the early line removes no block. A row said at a tool
        boundary and still owed at the stop is refused there."""
        row = self.plant("9999aaaa8888bbbb", "lane-stop")
        toolwhisper.commit_for_pair(stop_early.prepare(SID_A, SEAT_A))
        rc, out, err = self.stop()
        self.assertEqual(rc, 2, out + err)
        self.assertIn("helm dispatch triage %s" % row, out + err)


# ------------------------------------------------------------ (b) recovery

class RecoveryIsFyiTest(_World):

    def post(self, text, who=seatevents.WHO, origin=None):
        return chat.post(text, who=who, room=seatevents.ROOM, sign=False,
                         origin=origin)

    def test_a_recovery_row_is_marked_and_a_wall_is_not(self):  # noqa: VACUOUS_ASSERTION — both loops iterate literal tuples and cannot run zero times
        with mock.patch.object(seatevents, "steward",
                               return_value=(SEAT_A, None)):
            for kind in ("recovered", "unblock"):
                text = seatevents.row("credentials", "gemini recovered HEALTHY",
                                      kind=kind)
                self.assertTrue(text.startswith("@seat-a " + seatevents.FYI_LEAD),
                                text)
                self.assertTrue(seatevents.is_recovery(text))
            for kind in ("wall", "outage", None):
                text = seatevents.row("credentials", "gemini dark AUTH-401",
                                      kind=kind)
                self.assertEqual(text, "@seat-a gemini dark AUTH-401")
                self.assertFalse(seatevents.is_recovery(text))

    def test_the_classifier_calls_only_a_clean_recovery_fyi(self):  # noqa: VACUOUS_ASSERTION — the loop iterates a literal tuple of six rows and cannot run zero times
        names = [SEAT_A]
        ok = {"from": seatevents.WHO, "text": "@seat-a " + RECOVERED}
        self.assertFalse(needs_act.needs_act(ok, names))
        for row in (dict(ok, **{"from": "bob"}),
                    dict(ok, text="@seat-a gemini dark AUTH-UNAVAILABLE"),
                    dict(ok, text="@seat-a " + RECOVERED + "; probe failed"),
                    dict(ok, text="@seat-a gemini dark; " + seatevents.FYI_LEAD),
                    dict(ok, text=None), "not a row"):
            with self.subTest(row=row):
                self.assertTrue(needs_act.needs_act(row, names))

    def test_a_recovery_never_wakes_and_is_not_dropped(self):  # noqa: VACUOUS_ASSERTION — the held row deliver_any returns is the positive control on the same row
        self.post("@seat-a " + RECOVERED)
        self.assertEqual(self.beacon(), [])
        held = seats.deliver_any(session=SID_A, seat=SEAT_A)
        self.assertIsNotNone(held, "the held recovery row was dropped")
        self.assertIn("recovered HEALTHY", held)

    def test_act_rows_on_the_same_room_still_wake_in_the_same_pass(self):  # noqa: VACUOUS_ASSERTION — the loop iterates a literal tuple of four rows and cannot run zero times
        """Falsifiers: the dark row, an owner row, a failure row and an
        unclassifiable sender each wake within one beacon pass."""
        for i, (text, who, origin) in enumerate((
                ("@seat-a gemini dark AUTH-UNAVAILABLE", seatevents.WHO, None),
                ("@seat-a " + RECOVERED, "daria", "web"),
                ("@seat-a " + RECOVERED + "; restart failed", seatevents.WHO,
                 None),
                ("@seat-a " + RECOVERED, "bob", None))):
            with self.subTest(text=text, who=who):
                self.post(text, who=who, origin=origin)
                got = self.beacon()
                self.assertEqual(len(got), 1, got)

    def test_announce_end_to_end(self):
        """The real producer: a wall wakes the steward, its recovery does
        not, and the recovery reaches the next boundary."""
        ledger = os.path.join(self.tmp, "seat-events.json")

        def post(text, rid):
            self.post(text)

        def announce(kind, ident, body):
            return seatevents.announce([seatevents.event(
                "credentials", "family:gemini", kind, ident, body)],
                post=post, push=lambda body, title: True, path=ledger)

        with mock.patch.object(seatevents, "steward",
                               return_value=(SEAT_A, None)):
            announce("wall", "dark|t0", "gemini dark AUTH-UNAVAILABLE")
            self.assertEqual(len(self.beacon()), 1, "the wall did not wake")
            announce("unblock", "HEALTHY|t1", "gemini recovered HEALTHY")
        self.assertEqual(self.beacon(), [], "the recovery woke the steward")
        held = seats.deliver_any(session=SID_A, seat=SEAT_A)
        self.assertIn("recovered HEALTHY", held or "")

    def test_a_recovery_never_blocks_the_stop_but_a_wall_does(self):  # noqa: VACUOUS_ASSERTION — the wall's block on the same stop output is the positive control
        self.post("@seat-a " + RECOVERED)
        rc, out, err = self.stop()
        self.assertNotIn("undelivered message(s)", out + err)
        self.post("@seat-a gemini dark AUTH-UNAVAILABLE")
        rc, out, err = self.stop()
        self.assertEqual(rc, 2, out + err)
        self.assertIn("undelivered message(s)", out + err)


# ---------------------------------------------------------- (c) who and why

class AlarmsLeadWithWhoTest(_World):

    LONG = "[helm scratch] " + "memory is pressed on this slice; " * 12

    def test_a_mention_past_the_cut_leads_the_wake_line(self):
        chat.post(self.LONG + "\n@seat-b @seat-a", who="scratch-gc",
                  room="main", sign=False)
        got = self.beacon()
        self.assertEqual(len(got), 1, got)
        self.assertIn("@seat-a, named past the cut:", got[0])

    def test_the_boundary_line_leads_with_it_too(self):
        chat.post(self.LONG + "\n@seat-a", who="scratch-gc", room="main",
                  sign=False)
        line = seats.deliver_any(session=SID_A, seat=SEAT_A)
        self.assertIn("@seat-a, named past the cut:", line or "")

    def test_a_visible_mention_is_left_alone(self):  # noqa: VACUOUS_ASSERTION — the cut-mention control at the end asserts the same call does change a text
        for text in ("@seat-a " + self.LONG, "@seat-a short row"):
            with self.subTest(text=text[:20]):
                self.assertEqual(needs_act.lead_mention(text, [SEAT_A]), text)
        self.assertEqual(needs_act.lead_mention(self.LONG, [SEAT_A]),
                         self.LONG)
        cut = self.LONG + " @seat-a"
        self.assertNotEqual(needs_act.lead_mention(cut, [SEAT_A]), cut,
                            "control: a mention past the cut is led")

    def _pressure(self):
        return seatceiling.Pressure("/cg/agents-x.slice", 104, 100, 1.04,
                                    None, (1,), (1,), seatceiling.THROTTLED,
                                    None)

    def test_the_scratch_wake_leads_with_the_seats_it_wakes(self):
        p, said = self._pressure(), []
        got = scratch._wake(p.slice, p, {}, None,
                            audience=(SEAT_A, [SEAT_B, SEAT_C], "main", True),
                            post=lambda t, r: said.append(t) or {"id": "x"})
        self.assertIsNotNone(got)
        self.assertTrue(said[0].startswith("@seat-b @seat-c: [helm scratch]"),
                        said[0])

    def test_the_unphoned_escalation_leads_with_them(self):
        p, said = self._pressure(), []
        phone = mock.Mock()
        phone.configured.return_value = False
        spell = {"mentions": [SEAT_B], "since": time.time() - 600,
                 "room": "main", "seat": SEAT_A}
        scratch._escalate([(p.slice, p, spell)], time.time(),
                          lambda t, r: said.append(t) or {"id": "x"},
                          phone=phone)
        self.assertTrue(said[0].startswith("@seat-b: [helm scratch] still open"),
                        said)


if __name__ == "__main__":
    unittest.main()
