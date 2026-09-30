#!/usr/bin/env python3
"""A beacon ring costs the woken seat ONE model call when there is one thing
to do, instead of a hunt.

THE INSTANCE THESE ARMS PIN (measured on the live fleet). A seat's ring led
with an addressed row two days old, "you have a review row from demo: lane
... at <tip>", whose dispatch row had already been CANCELLED; it carried only
that row's first line and named one `helm chat read --room X --since N` per
room, beside 111 @all rows. The seat then ran eight tool calls (two dispatch
listings, two room reads, a 92 KB catchup, a grep) and found nothing to do.
Over three days of the fleet a wake cost a median of 5 and a mean of 12 model
calls, each re-sending the seat's whole context.

So a ring (helm/beacon_doorbell.py):
  * leads with the row to act on: the owner's, then a DM, then the NEWEST row
    addressed to the seat; an @all never leads while one is unread;
  * releases a row whose dispatch row or task has closed, as an ack would,
    counts it as `N already closed`, and keeps a trace of it in the state; a
    referent it cannot read keeps the row live;
  * carries the lead row's whole body, up to LEAD_CHARS, and says where a
    longer one was cut;
  * names ONE pull, `helm chat read --id ID,ID,...`, for exactly the rows to
    act on, and counts the rest as `read when idle`.

Every arm drives the real waiter (`helm chat wait --follow`) and the real
`helm chat read` door on the doorbell fixture (PullBase: a scratch chat dir
and helm home). Dispatch rows are planted raw into the scratch ledger, as
tests/test_owed_row_resurfaces.py plants them; the "two-day-old" row is the
oldest by order, which is all the ring reads of its age.
"""
import contextlib
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import beacon_doorbell as bd  # noqa: E402
from helm import chat, dispatches, eventledger, pk, seats, tasks  # noqa: E402
from tests._tmphome import declaring as _tmp_declaring  # noqa: E402
from tests.test_beacon_doorbell import SEAT, _unread  # noqa: E402
from tests.test_pull_delivery import PullBase  # noqa: E402

#: The cancelled review row of the instance, as the ledger stores it.
CANCELLED = "72e9a1b7c0ffee5566778899aabbccddeeff0011"
LIVE = "5d1c0e4b9a8f77aa6655443322110099ffeeddcc"


class _World(PullBase):

    def plant(self, rid, cancelled=False, recipient=SEAT):
        """One review row handed to `recipient` by another seat, delivered,
        and cancelled when `cancelled`: the ledger's own events."""
        ts = "2026-09-26T05:46:23Z"
        events = [{"v": 3, "event": "dispatch", "seq": 0, "id": rid, "ts": ts,
                   "recipient": recipient, "lane": "emdash-homepage-cures",
                   "tip": "a" * 40, "ref": "a" * 40, "deadline_s": 2700,
                   "status": "open", "kind": "review",
                   "sender": "demo-lead"},
                  {"v": 3, "event": "delivered", "seq": 1, "id": rid,
                   "ts": ts, "delivery_ref": "dm-x"}]
        if cancelled:
            events.append({"v": 3, "event": "cancel", "seq": 2, "id": rid,
                           "ts": ts, "reason": "the lane was re-sent"})
        path = dispatches.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        row = dispatches.snapshot()[0][rid]
        self.assertEqual(row["status"], "cancelled" if cancelled else "open",
                         "the planted row is not what the arm says it is")

    def stamped(self, offset):
        """Rows posted inside stamp one second apart from `offset` seconds
        off a fixed hour, so rows in different rooms have an order the ring
        can read (a row's stamp has one-second resolution)."""
        t = [1_790_000_000 + offset]

        def now_ts():
            t[0] += 1
            return pk.epoch_ts(t[0])
        return mock.patch.object(pk, "now_ts", now_ts)

    def ring_once(self):
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(bd._TAIL.search(lines[0]),
                        "the tail no longer strips as fixed text: " + lines[0])
        return lines[0]

    def pull_ids(self, ring):
        m = re.search(r"\(\+\d+ waiting — helm chat read --id ([0-9a-f,]+)"
                      r"[ ;]", ring)
        self.assertIsNotNone(m, "the ring names no one pull by id:\n" + ring)
        return m.group(1).split(",")


class TheGeminiRingTest(_World):

    def test_the_ring_leads_with_the_newest_live_ask_whole_and_names_one_pull(self):
        """RED before the cure: the ring led with the cancelled row's first
        line and named three per-room pulls."""
        self.plant(CANCELLED, cancelled=True)
        with self.stamped(-2 * 86400):
            stale = chat.post(
                "@gemini you have a review row from demo: lane "
                "emdash-homepage-cures at 69b4ae043 on repo demo\n"
                "YOUR ROW: %s (lane emdash-homepage-cures, kind review)"
                % CANCELLED, who="demo-lead", room="demo")
        with self.stamped(0):
            live = [chat.post("@gemini ask %d: please read the plan" % i,
                              who="bob", room=room)
                    for i, room in enumerate(("helm", "demo", "main"))]
            body = ("@gemini the land gate is red on the composed tip\n"
                    "the failing arm is test_the_lead_order\n\n"
                    "can you cure it off 3f2a91c and name the tip here? " +
                    "the context is long. " * 20).strip()
            live.append(chat.post(body, who="kimi", room="demo"))
            for i in range(111):
                chat.post("@all standup item %d" % i, who="seat-c")
        ring = self.ring_once()

        lead = ("] kimi: @gemini the land gate is red on the composed tip ⏎ "
                "the failing arm is test_the_lead_order ⏎ can you cure it "
                "off 3f2a91c and name the tip here? ")
        self.assertIn(lead, ring, "the lead is not the newest ask, whole")
        self.assertIn(("the context is long. " * 20).strip() + " (+114 waiting",
                      ring)
        self.assertNotIn("you have a review row", ring)
        self.assertIn("doorbell: 115 unread = 4 addressed, 0 DM, 111 @all",
                      ring)
        self.assertIn(" · 1 already closed", ring)
        # ONE pull, for exactly the four live asks, the lead first
        self.assertEqual(ring.count("helm chat read"), 1, ring)
        self.assertNotIn("--since", ring)
        named = self.pull_ids(ring)
        self.assertEqual(named, [live[3]["id"], live[2]["id"], live[1]["id"],
                                 live[0]["id"]])
        self.assertIn(" · 111 not addressed: read when idle · ", ring)
        # the release is auditable: its trace names the row and what closed it
        trace = bd._load(bd.state_path(SEAT))["closed"]
        self.assertEqual([(c["id"], c["room"]) for c in trace],
                         [(stale["id"], "demo")])
        self.assertEqual(trace[0]["closed"], "%s cancelled" % CANCELLED[:12])
        # the one pull reads exactly those four rows, and they leave the count
        out = self.pull("--id", ",".join(named))
        for row in live:
            self.assertIn(row["text"].split("\n")[0], out)
        self.assertNotIn("you have a review row", out)
        self.assertNotIn("standup item", out)
        self.assertEqual(self.ring_count(), 111)


class TheLeadIsTheRowToActOnTest(_World):

    def test_a_dm_leads_over_a_newer_addressed_row(self):
        """RED before the cure: a DM and a mention shared a class, so the
        newer mention led."""
        seats.dm(SEAT, "a direct ask", who="carol")
        chat.post("@gemini a newer peer ask", who="bob")
        self.assertIn("] carol: a direct ask (+1 waiting — ", self.ring_once())

    def test_the_owner_leads_over_a_dm_and_a_newer_addressed_row(self):
        """The control, green before the cure: the owner's row leads."""
        chat.post("@all hold every land", who="daria", origin="web")
        seats.dm(SEAT, "a direct ask", who="carol")
        chat.post("@gemini a newer peer ask", who="bob")
        ring = self.ring_once()
        self.assertIn("] daria: @all hold every land (+2 waiting — ", ring)
        self.assertIn("1 from the owner", ring)

    def test_an_all_never_leads_while_an_addressed_row_is_unread(self):
        """The first ring announced the ask; an @all lands and rings. RED
        before the cure: the second ring led with the @all, because only the
        rows new since the last ring could lead."""
        chat.post("@gemini the ask", who="bob")

        def script(n, _now):
            if n == 2:
                chat.post("@all standup", who="carol")
        lines, _seen = self.follow(passes=40, script=script, clock=True)
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("] bob: @gemini the ask (+1 waiting — ", lines[1])
        self.assertIn("1 not addressed: read when idle", lines[1])

    def test_the_newest_addressed_row_leads(self):
        chat.post("@gemini the older ask", who="bob")
        chat.post("@gemini the newer ask", who="carol")
        self.assertIn("] carol: @gemini the newer ask (+1 waiting — ",
                      self.ring_once())


class TheLeadRidesWholeTest(_World):

    def test_a_body_past_the_bound_is_cut_and_names_the_read_by_id(self):
        """RED before the cure: 152 characters of the body and no way back
        to the rest but a room read."""
        self.assertEqual(bd.LEAD_CHARS, 1200)
        row = chat.post("@gemini " + "y" * 3000, who="bob")
        ring = self.ring_once()
        self.assertIn("] bob: @gemini " + "y" * 1192 + " (cut, 1808 more "
                      "chars: helm chat read --id %s) (+0 waiting — "
                      % row["id"], ring)
        self.assertNotIn("y" * 1193, ring)

    def test_a_body_at_the_bound_rides_whole(self):
        """RED before the cure: clipped at 160 characters."""
        text = "@gemini " + "z" * (bd.LEAD_CHARS - len("@gemini "))
        chat.post(text, who="bob")
        ring = self.ring_once()
        self.assertIn("] bob: %s (+0 waiting — " % text, ring)
        self.assertNotIn("(cut,", ring)


class AClosedReferentCostsNoCallTest(_World):

    def test_a_ring_of_only_closed_rows_is_not_rung(self):
        """RED before the cure: the waiter rang for a row whose dispatch row
        was cancelled, and the seat hunted for it."""
        self.plant(CANCELLED, cancelled=True)
        row = chat.post("@gemini your review row %s" % CANCELLED[:12],
                        who="demo-lead")
        lines, _seen = self.follow(passes=3, clock=True)
        self.assertEqual(lines, [], "a closed row rang")
        trace = bd._load(bd.state_path(SEAT))["closed"]
        self.assertEqual([c["id"] for c in trace], [row["id"]])
        self.assertEqual(self.ring_count(), 0)

    def test_an_unreadable_referent_stays_live(self):
        """The ledger will not open, so the cancel cannot be seen: the row
        rings and is not counted as closed."""
        self.plant(CANCELLED, cancelled=True)
        chat.post("@gemini your review row %s" % CANCELLED[:12],
                  who="demo-lead")
        with mock.patch.object(dispatches, "snapshot",
                               side_effect=OSError("ledger unreadable")):
            ring = self.ring_once()
        self.assertIn("] demo-lead: @gemini your review row %s"
                      % CANCELLED[:12], ring)
        self.assertNotIn("already closed", ring)
        self.assertEqual(_unread(ring), 1)
        self.assertEqual(bd._load(bd.state_path(SEAT))["closed"], [])

    def test_a_live_referent_and_a_closed_one_keep_the_row(self):
        """A row naming one open row and one cancelled row is still work."""
        self.plant(CANCELLED, cancelled=True)
        self.plant(LIVE)
        chat.post("@gemini rows %s and %s" % (CANCELLED[:12], LIVE[:12]),
                  who="demo-lead")
        ring = self.ring_once()
        self.assertNotIn("already closed", ring)
        self.assertEqual(_unread(ring), 1)

    def test_a_closed_row_the_seat_sent_stays_live(self):
        """A row the seat sent and another seat closed may carry its next
        work (a FIX to cure), so it is not released."""
        self.plant(CANCELLED, cancelled=True, recipient="kimi")
        chat.post("@gemini kimi closed %s" % CANCELLED[:12], who="bob")
        ring = self.ring_once()
        self.assertNotIn("already closed", ring)
        self.assertEqual(_unread(ring), 1)

    def test_a_hex_token_the_ledger_does_not_resolve_is_no_referent(self):
        """A commit sha is not a dispatch row: the row stays and rings."""
        self.plant(CANCELLED, cancelled=True)
        chat.post("@gemini look at commit 0123456789abcdef0123", who="bob")
        ring = self.ring_once()
        self.assertNotIn("already closed", ring)
        self.assertEqual(_unread(ring), 1)

    def test_a_closed_task_is_released_and_an_unknown_task_stays(self):
        self.assertTrue(eventledger.append(tasks.ledger_path(), {
            "id": "task/77", "title": "a finished task", "status": "closed"}))
        chat.post("@gemini task/77 needs your read", who="bob")
        chat.post("@gemini task/78 needs your read", who="bob")
        ring = self.ring_once()
        self.assertIn("] bob: @gemini task/78 needs your read (+0 waiting",
                      ring)
        self.assertIn(" · 1 already closed", ring)
        trace = bd._load(bd.state_path(SEAT))["closed"]
        self.assertEqual([c["closed"] for c in trace], ["task/77 closed"])

    def test_a_closed_row_with_an_unknown_event_kind_stays_live(self):
        """RED before the cure: the waiter read the folded `cancelled` and
        released the row, though the fold met an event kind this helm cannot
        read after the cancel, which may be the one that reopened it
        (`dispatches._row_ended` reads such a row as live)."""
        self.plant(CANCELLED, cancelled=True)
        with open(dispatches.ledger_path(), "a") as f:
            f.write(json.dumps({"v": 3, "event": "a-future-kind", "seq": 3,
                                "id": CANCELLED,
                                "ts": "2026-09-26T05:47:00Z"}) + "\n")
        current = dispatches.snapshot()[0]
        row = current[CANCELLED]
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(dispatches.unknown_event_kinds(row),
                         ("a-future-kind",))
        self.assertEqual(dispatches._row_ended(row, current, None, None),
                         (None, None), "the ledger's own reader says ended")
        chat.post("@gemini your review row %s" % CANCELLED[:12],
                  who="demo-lead")
        ring = self.ring_once()
        self.assertIn("] demo-lead: @gemini your review row %s"
                      % CANCELLED[:12], ring)
        self.assertNotIn("already closed", ring)
        self.assertEqual(_unread(ring), 1)
        self.assertEqual(bd._load(bd.state_path(SEAT))["closed"], [])

    def test_a_task_ledger_with_a_corrupt_row_releases_nothing(self):
        """RED before the cure: the lenient task read skipped the corrupt
        line and released the row as `task/77 closed`. A ledger that does
        not read whole cannot say the task stayed closed."""
        self.assertTrue(eventledger.append(tasks.ledger_path(), {
            "id": "task/77", "title": "a finished task", "status": "closed"}))
        with open(tasks.ledger_path(), "a") as f:
            f.write("{not a row\n")
        self.assertIsNotNone(tasks.snapshot(strict=True)[1])
        chat.post("@gemini task/77 needs your read", who="bob")
        ring = self.ring_once()
        self.assertIn("] bob: @gemini task/77 needs your read (+0 waiting",
                      ring)
        self.assertNotIn("already closed", ring)
        self.assertEqual(bd._load(bd.state_path(SEAT))["closed"], [])

    def test_a_slug_task_is_its_own_referent(self):
        """RED before the cure: `task/12-fix` read as `task/12`, so a row
        about the slug task was released when task/12 closed. The control in
        the same world: the row naming task/12 itself is released."""
        self.assertTrue(eventledger.append(tasks.ledger_path(), {
            "id": "task/12", "title": "a finished task", "status": "closed"}))
        chat.post("@gemini task/12 is done", who="bob")
        chat.post("@gemini task/12-fix needs your read", who="bob")
        ring = self.ring_once()
        self.assertIn("] bob: @gemini task/12-fix needs your read (+0 waiting",
                      ring)
        self.assertIn(" · 1 already closed", ring)
        trace = bd._load(bd.state_path(SEAT))["closed"]
        self.assertEqual([c["closed"] for c in trace], ["task/12 closed"])

    def test_a_task_range_names_no_closed_task(self):
        """A range the docs write, `task/1342-1344`, is not task/1342: the
        row stays though task/1342 closed."""
        self.assertTrue(eventledger.append(tasks.ledger_path(), {
            "id": "task/1342", "title": "a finished task",
            "status": "closed"}))
        chat.post("@gemini task/1342-1344 need your read", who="bob")
        ring = self.ring_once()
        self.assertNotIn("already closed", ring)
        self.assertEqual(_unread(ring), 1)

    def test_a_closed_slug_task_is_released(self):
        """The slug arm's other half: a slug id the task ledger holds closed
        is read by the task module's own id reader and released."""
        self.assertTrue(eventledger.append(tasks.ledger_path(), {
            "id": "task/12-fix", "title": "a finished slug task",
            "status": "closed"}))
        chat.post("@gemini task/12-fix is done", who="bob")
        lines, _seen = self.follow(passes=3, clock=True)
        self.assertEqual(lines, [], "a closed row rang")
        trace = bd._load(bd.state_path(SEAT))["closed"]
        self.assertEqual([c["closed"] for c in trace], ["task/12-fix closed"])


class OnePullReadsSeveralIdsTest(_World):

    def test_read_id_takes_a_list_and_refuses_only_the_unknown_one(self):
        """RED before the cure: `--id a,b` looked for one row named `a,b`."""
        a = chat.post("@gemini first", who="bob")
        b = chat.post("@gemini second", who="bob", room="helm")
        out = self.pull("--id", "%s,%s" % (b["id"], a["id"]))
        self.assertLess(out.index("@gemini second"), out.index("@gemini first"))
        self.assertIn("helm chat [helm] ", out)
        out, err = io.StringIO(), io.StringIO()
        with _tmp_declaring(["--seat", SEAT]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(["read", "--id", "%s,ffffffffffff" % a["id"]])
        self.assertEqual(rc, 1)
        self.assertIn("@gemini first", out.getvalue())
        self.assertIn("ffffffffffff", err.getvalue())


if __name__ == "__main__":
    unittest.main()
