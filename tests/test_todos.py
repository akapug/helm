#!/usr/bin/env python3
"""helm todos — the seat todo mirror (the TodoWrite/Task* -> helm bridge).

Pins the design law (decision-spirit #23, the attention budget): PULL-FIRST
state, PUSH only on a meaningful transition, rate-capped, collapsed, and
NEVER an @mention or a DM. Plus the mechanical laws: capture off both tool
families, fail-closed on garbage, bounded state, no regression to the
recorder's existing writers, and a real pull surface (`helm todos`).

Hermetic: HELM_HOME + HELM_CHAT_DIR are tmp dirs, the signed transport is
killed (HELM_CHAT_NODE_URL set-but-empty), ambient harness session ids are
scrubbed — the live ~/.claude and /dev/shm rooms are never touched.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import (chat, pk, record, seats, tasks, todos, web,  # noqa: E402
                  web_ui_loader)

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "HELM_CHAT_ROOM",
            "HELM_CHAT_OWNER_NAMES", "HELM_TODO_POST",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL",
            # LedgerBridgeTest points the harness home at a tmpdir. Left set,
            # every later module in the same process reads a deleted directory
            # as ~/.claude — which is exactly the cross-module contamination
            # test_env_hygiene exists to catch.
            "CLAUDE_CONFIG_DIR")

SID = "sess-todo-1"


def todo_list(*pairs):
    return [{"content": t, "status": s} for t, s in pairs]


class TodosBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-todos-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        # The fixture homes its posts EXPLICITLY through the same env seam
        # launched seats use — the old accidental "main" default, now stated.
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""   # no signer, no node probe
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "owner"

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ev(self, tool="TodoWrite", sid=SID, tin=None, resp=None, **extra):
        e = {"session_id": sid, "tool_name": tool, "cwd": self.tmp,
             "hook_event_name": "PostToolUse"}
        if tin is not None:
            e["tool_input"] = tin
        if resp is not None:
            e["tool_response"] = resp
        e.update(extra)
        return e

    def write(self, *pairs, **kw):
        """One TodoWrite event through the REAL recorder entry point."""
        record.record(self.ev(tin={"todos": todo_list(*pairs)}, **kw))

    def room(self, name="main"):
        try:
            with open(chat.room_path(name), encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except OSError:
            return []

    def seat(self, name="seat-a", sid=SID):
        seats.write_roster(name, session=sid, cwd=self.tmp)
        return name

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = todos.cmd_todos(list(args))
        return rc, out.getvalue(), err.getvalue()


class CaptureTest(TodosBase):
    def test_todowrite_captures_the_current_list(self):
        self.write(("plan the slice", "completed"),
                   ("write the bridge", "in_progress"),
                   ("ship it", "pending"))
        st = todos.state(SID)
        self.assertEqual(st["src"], "TodoWrite")
        self.assertEqual([i["text"] for i in st["items"]],
                         ["plan the slice", "write the bridge", "ship it"])
        d = todos.digest(st["items"])
        self.assertEqual((d["active"], d["done"], d["total"]),
                         ("write the bridge", 1, 3))

    def test_capture_replaces_rather_than_appends(self):
        self.write(("a", "in_progress"), ("b", "pending"))
        self.write(("a", "completed"), ("b", "in_progress"))
        st = todos.state(SID)
        self.assertEqual(len(st["items"]), 2)
        self.assertEqual(todos.digest(st["items"])["done"], 1)

    def test_legacy_json_encoded_todos_string_parses(self):
        record.record(self.ev(tool="TaskUpdateTODO", tin={
            "todos": json.dumps([{"content": "legacy", "status": "in_progress"}])}))
        self.assertEqual(todos.digest(todos.state(SID)["items"])["active"],
                         "legacy")

    def test_task_family_create_then_update_by_id(self):
        record.record(self.ev(tool="TaskCreate",
                              tin={"subject": "Slice 2: cell tombstone live test"},
                              resp="Task #22 created successfully: Slice 2"))
        st = todos.state(SID)
        self.assertEqual([(i["id"], i["status"]) for i in st["items"]],
                         [("22", "pending")])
        record.record(self.ev(tool="TaskUpdate",
                              tin={"taskId": "22", "status": "completed"}))
        st = todos.state(SID)
        self.assertEqual(st["items"][0]["status"], "completed")
        self.assertEqual(st["items"][0]["text"],
                         "Slice 2: cell tombstone live test")  # subject survives

    def test_task_delete_tombstones_the_row(self):
        record.record(self.ev(tool="TaskCreate", tin={"subject": "doomed"},
                              resp="Task #3 created successfully"))
        record.record(self.ev(tool="TaskUpdate",
                              tin={"taskId": "3", "status": "deleted"}))
        self.assertEqual(todos.state(SID)["items"], [])

    def test_items_and_text_are_bounded(self):
        self.write(*[("x" * 400 + " #%d" % i, "pending")
                     for i in range(todos.ITEM_CAP + 20)])
        items = todos.state(SID)["items"]
        self.assertEqual(len(items), todos.ITEM_CAP)
        self.assertTrue(all(len(i["text"]) <= todos.TEXT_CAP for i in items))

    def test_state_is_session_keyed(self):
        self.write(("mine", "in_progress"))
        self.write(("theirs", "in_progress"), sid="other-session")
        self.assertEqual(todos.digest(todos.state(SID)["items"])["active"],
                         "mine")
        self.assertEqual(
            todos.digest(todos.state("other-session")["items"])["active"],
            "theirs")

    def test_failed_todo_call_mirrors_nothing(self):
        record.record(self.ev(tin={"todos": todo_list(("nope", "in_progress"))},
                              hook_event_name=record.FAIL_EVENT,
                              error="Exit code 1"))
        self.assertEqual(todos.state(SID), {})


class FailClosedTest(TodosBase):
    def test_garbage_payloads_never_raise_and_never_write(self):
        for tin in ({"todos": "not json at all"}, {"todos": 17},
                    {"todos": [1, 2, 3]}, {"todos": [{"status": "pending"}]},
                    {}, {"todos": None}):
            record.record(self.ev(tin=tin))
        st = todos.state(SID)
        self.assertIn(st.get("items", []), ([], ))  # empty list or nothing at all

    def test_prior_state_survives_an_unusable_payload(self):
        self.write(("real work", "in_progress"))
        record.record(self.ev(tin={"todos": "}{ garbage"}))
        self.assertEqual(todos.digest(todos.state(SID)["items"])["active"],
                         "real work")

    def test_a_broken_mirror_never_costs_the_recorder(self):
        """The mirror is walled off: even an exploding todos.capture leaves
        counters, command-log and edit-targets intact."""
        boom = mock.Mock(side_effect=RuntimeError("mirror down"))
        with mock.patch.object(todos, "capture", boom):
            record.record(self.ev(tin={"todos": todo_list(("x", "pending"))}))
            record.record(self.ev(tool="Edit", tin={"file_path": "/a/b.py"}))
        self.assertTrue(boom.called)
        self.assertEqual(record.counters(SID)["last-tool"], "Edit")

    def test_cmd_record_stays_silent_rc0_on_a_todo_event(self):
        out, err = io.StringIO(), io.StringIO()
        payload = json.dumps(self.ev(tin={"todos": todo_list(("a", "pending"))}))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)):
            rc = record.cmd_record(["--hook-json"])
        self.assertEqual((rc, out.getvalue(), err.getvalue()), (0, "", ""))
        self.assertTrue(todos.state(SID)["items"])


class RecorderRegressionTest(TodosBase):
    def test_existing_writers_are_untouched_by_the_new_leg(self):
        with mock.patch.object(record, "_git_dirty", mock.Mock(return_value=True)):
            record.record(self.ev(tool="Bash",
                                  tin={"command": "python3 -m pytest -q"},
                                  resp={"stdout": "ok"}))
            record.record(self.ev(tool="Edit", tin={"file_path": "/x/y.py"}))
            self.write(("something", "in_progress"))
        sd = record.session_dir(SID)
        with open(os.path.join(sd, "command-log.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
        self.assertEqual([r["token"] for r in rows], ["pytest"])
        self.assertEqual(rows[0]["exit"], 0)
        with open(os.path.join(sd, "edit-targets.log"), encoding="utf-8") as f:
            self.assertEqual(f.read().split(), ["y.py"])
        c = record.counters(SID)
        self.assertEqual(c["last-tool"], "TodoWrite")
        self.assertEqual(c["last-dirty"], 1)

    def test_todo_tools_do_not_fork_git(self):
        probe = mock.Mock(return_value=True)
        with mock.patch.object(record, "_git_dirty", probe):
            self.write(("no subprocess on this path", "in_progress"))
        probe.assert_not_called()

    def test_the_recorders_tool_list_matches_the_mirrors(self):
        """record.py spells the tool names itself so the hot path never
        imports todos.py for the 99% of events that are not todo writes —
        which means the two lists can DRIFT, and a name added only to
        todos.TOOLS would be captured never. Pin them equal."""
        self.assertEqual(set(record.TASK_TOOLS), set(todos.TOOLS))

    def test_the_mirror_runs_after_the_counters_are_on_disk(self):
        """The mirror's push leg appends to a chat room under that room's
        flock — and an exception is not the only way to lose a write, a
        BLOCK is. The stop-whisper's ground truth must already be durable
        before the mirror can wait on anybody."""
        seen = {}

        def spy(*a, **kw):
            seen["counters"] = record.counters(SID).get("last-tool")
            return None

        with mock.patch.object(todos, "capture", spy):
            self.write(("ordering", "in_progress"))
        self.assertEqual(seen.get("counters"), "TodoWrite")


class TransitionPostTest(TodosBase):
    def test_a_burst_of_writes_yields_at_most_one_post(self):
        self.seat()
        for i in range(25):     # a churny turn: 25 TodoWrite calls
            self.write(("task one", "in_progress"),
                       ("task two", "pending"),
                       ("note %d" % i, "pending"))
        rows = self.room()
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("task one", rows[0]["text"])

    def test_concurrent_burst_yields_at_most_one_post(self):
        """Atomic replace prevents torn JSON but does not serialize the cap:
        parallel TaskUpdates used to all read the same pre-post ledger and
        append 2-5 rows. The per-session flock makes the decision atomic."""
        for n in range(8):
            sid, seat = "race-%d" % n, "seat-%d" % n
            self.seat(seat, sid)
            record.record(self.ev(
                tool="TaskCreate", sid=sid, tin={"subject": "race task"},
                resp="Task #%d created successfully" % (n + 1)))
            barrier = threading.Barrier(16)

            def update(i):
                barrier.wait()
                record.record(self.ev(
                    tool="TaskUpdate", sid=sid,
                    tin={"taskId": str(n + 1), "status": "in_progress",
                         "subject": "race task %02d" % i}))

            workers = [threading.Thread(target=update, args=(i,))
                       for i in range(16)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(len(self.room()), n + 1)

    def test_no_post_when_nothing_materially_changed(self):
        self.seat()
        self.write(("steady", "in_progress"))
        self.assertEqual(len(self.room()), 1)
        # re-order + re-word a PENDING sibling: churn, not a transition
        for _ in range(5):
            self.write(("steady", "in_progress"), ("later idea", "pending"))
        self.assertEqual(len(self.room()), 1)

    def test_a_completion_posts_once_the_rate_cap_window_passes(self):
        self.seat()
        self.write(("first", "in_progress"))
        self.assertEqual(len(self.room()), 1)
        self.write(("first", "completed"), ("second", "in_progress"))
        self.assertEqual(len(self.room()), 1, "rate cap holds the second post")
        st = todos.state(SID)                 # age the cap ledger past the floor
        st["post"]["ts"] -= todos.MIN_POST_S + 1
        pk.write_json(todos.state_path(SID), st)
        self.write(("first", "completed"), ("second", "completed"))
        rows = self.room()
        self.assertEqual(len(rows), 2)
        self.assertIn("2/2 done", rows[1]["text"])

    def test_task_family_transition_posts_too(self):
        """Regression (dogfood): _apply_task aliased the pre-event rows, so an
        in-place TaskUpdate mutated the snapshot it was diffed against and the
        whole Task* family — what the live fleet actually emits — never posted
        a single transition."""
        self.seat("demo-codex")
        record.record(self.ev(tool="TaskCreate", tin={"subject": "Scala gate"},
                              resp="Task #41 created successfully"))
        self.assertEqual(self.room(), [], "a new PENDING task is not news")
        record.record(self.ev(tool="TaskUpdate",
                              tin={"taskId": "41", "status": "in_progress"}))
        rows = self.room()
        self.assertEqual(len(rows), 1)
        self.assertIn("Scala gate", rows[0]["text"])
        self.assertEqual(rows[0]["from"], "demo-codex")

    def test_apply_task_never_mutates_the_caller_list(self):
        before = todos._items(todo_list(("a", "pending")))
        after = todos._apply_task(before, "TaskUpdate",
                                  {"taskId": "1", "status": "completed"}, None)
        self.assertEqual(before[0]["status"], "pending")
        self.assertEqual(after[0]["status"], "completed")

    def test_a_new_pending_item_is_not_a_meaningful_transition(self):
        old = todos.digest(todos._items(todo_list(("a", "in_progress"))))
        new = todos.digest(todos._items(todo_list(("a", "in_progress"),
                                                  ("b", "pending"))))
        self.assertFalse(todos._meaningful(old, new))

    def test_idle_seat_picking_up_work_is_meaningful(self):
        old = todos.digest(todos._items(todo_list(("a", "pending"))))
        new = todos.digest(todos._items(todo_list(("a", "in_progress"))))
        self.assertTrue(todos._meaningful(old, new))

    def test_post_is_never_a_mention_and_never_a_dm(self):
        self.seat("seat-a")
        seats.write_roster("seat-b", session="other", cwd=self.tmp)
        self.write(("ping @seat-b about the merge", "in_progress"))
        rows = self.room()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("@", rows[0]["text"])          # no mention, ever
        self.assertNotIn("dm", rows[0])                 # no DM stamp
        self.assertFalse(seats.deliverable(rows[0], "seat-b"),
                         "a todo mirror post must never wake another seat")
        self.assertFalse(os.path.isdir(os.path.join(chat.chat_dir(), "dm")))

    def test_post_does_not_wake_a_seat_homed_to_the_posting_room(self):
        """The un-homed case is the EASY one. Stripping '@' is not enough:
        deliverable() hands EVERY plain row in a seat's home room to that
        seat, so on a `helm launch --room team-x` fleet a todo transition
        woke the whole team — the exact beacon-noise class the owner had
        removed. The row rides `ambient`, which is dropped before every wake
        rule (found adversarially; regression pin)."""
        os.environ["HELM_CHAT_ROOM"] = "team-x"
        self.seat("seat-a")
        for peer in ("homed-peer", "muted-peer", "unhomed-peer"):
            seats.write_roster(peer, session="s-" + peer, cwd=self.tmp)
        seats.write_roster("homed-peer", home_room="team-x")
        self.write(("land the slice", "in_progress"))
        rows = self.room("team-x")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].get("ambient"))
        for peer in ("homed-peer", "muted-peer", "unhomed-peer"):
            self.assertFalse(
                seats.deliverable(rows[0], peer, room="team-x"),
                "the todo mirror woke %s — it must wake NOBODY" % peer)

    def test_ambient_is_a_wake_rule_not_a_visibility_rule(self):
        """The line still LANDS: the owner and every reader see it, an @all
        in it cannot smuggle a wake, and a real (non-ambient) row in the same
        room still wakes its home seat — the gate is scoped, not a mute."""
        seats.write_roster("homed-peer", session="s-h", cwd=self.tmp,
                           home_room="team-x")
        chat.post("todo · now: something (0/1 done)", room="team-x",
                  who="seat-a", sign=False, ambient=True)
        chat.post("@all standup in five", room="team-x", who="seat-a",
                  sign=False)
        rows = self.room("team-x")
        self.assertEqual(len(rows), 2)
        self.assertFalse(seats.deliverable(rows[0], "homed-peer", room="team-x"))
        self.assertTrue(seats.deliverable(rows[1], "homed-peer", room="team-x"))
        self.assertEqual(rows[0]["text"], "todo · now: something (0/1 done)")

    def test_ambient_never_silences_a_dm(self):
        """`ambient` is for machine status in a ROOM. A DM is addressed to a
        person and must stay deliverable whatever else a caller passes."""
        seats.write_roster("seat-b", session="s-b", cwd=self.tmp)
        row = chat.post("look at this", who="seat-a", dm="seat-b",
                        sign=False, ambient=True)
        self.assertNotIn("ambient", row)
        self.assertTrue(seats.deliverable(row, "seat-b",
                                          room=seats.dm_lane("seat-b")))

    def test_an_unseated_session_posts_nothing_but_still_mirrors(self):
        self.write(("headless work", "in_progress"))
        self.assertEqual(self.room(), [])
        self.assertTrue(todos.state(SID)["items"])

    def test_post_can_be_switched_off(self):
        os.environ["HELM_TODO_POST"] = "0"
        self.seat()
        self.write(("quiet", "in_progress"))
        self.assertEqual(self.room(), [])
        self.assertTrue(todos.state(SID)["items"])

    def test_a_dead_chat_rail_never_costs_the_mirror(self):
        self.seat()
        with mock.patch.object(chat, "post",
                               mock.Mock(side_effect=OSError("rail down"))):
            self.write(("resilient", "in_progress"))
        self.assertEqual(todos.digest(todos.state(SID)["items"])["active"],
                         "resilient")


class PullSurfaceTest(TodosBase):
    def test_todos_all_table(self):
        self.seat("builder", SID)
        self.write(("wire the bridge", "in_progress"), ("survey", "completed"))
        rc, out, _ = self.run_cli(["--all"])
        self.assertEqual(rc, 0)
        self.assertIn("seat", out)
        self.assertIn("builder", out)
        self.assertIn("wire the bridge", out)
        self.assertIn("1/2", out)

    def test_todos_all_json(self):
        self.seat("builder", SID)
        self.write(("only task", "in_progress"))
        rc, out, _ = self.run_cli(["--all", "--json"])
        rep = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(rep["seats"][0]["seat"], "builder")
        self.assertEqual(rep["seats"][0]["active"], "only task")

    def test_unseated_sessions_surface_as_orphans(self):
        self.write(("nobody claimed me", "in_progress"))
        rep = todos.fleet()
        self.assertEqual(rep["seats"], [])
        self.assertEqual(len(rep["orphans"]), 1)
        self.assertEqual(rep["orphans"][0]["active"], "nobody claimed me")
        rc, out, _ = self.run_cli(["--all"])
        self.assertIn("nobody claimed me", out)

    def test_this_seat_view(self):
        self.write(("do the thing", "in_progress"), ("done thing", "completed"))
        with mock.patch.object(todos, "this_session", lambda: SID):
            rc, out, _ = self.run_cli([])
        self.assertEqual(rc, 0)
        self.assertIn("1/2 done", out)
        self.assertIn("▶ do the thing", out)
        self.assertIn("✓ done thing", out)

    def test_empty_fleet_says_so_and_exits_clean(self):
        rc, out, _ = self.run_cli(["--all"])
        self.assertEqual(rc, 0)
        self.assertIn("no seats yet", out)

    def test_seats_with_no_mirror_collapse_into_one_footer_line(self):
        self.seat("builder", SID)
        for n in range(6):
            seats.write_roster("idle-%d" % n, session="s-%d" % n, cwd=self.tmp)
        self.write(("the only real task", "in_progress"))
        rc, out, _ = self.run_cli(["--all"])
        self.assertEqual(rc, 0)
        self.assertIn("the only real task", out)
        self.assertNotIn("idle-3", out)          # no screen of '—' rows
        self.assertIn("6 seats with no mirrored todos", out)

    def test_a_corrupt_mirror_file_never_crashes_a_read_surface(self):
        """todos.json is a file a hand-edit, a truncated race or a foreign
        writer can reach. Every shape must degrade to 'no mirror', never to
        a traceback out of `helm todos --all` — which reads the WHOLE fleet,
        so one bad file would blind every seat at once."""
        self.seat("builder", SID)
        self.write(("healthy", "in_progress"))
        good = json.dumps({"v": 1, "ts": int(time.time()),
                           "items": [{"id": "1", "text": "healthy",
                                      "status": "in_progress"}]})
        for blob in ('[1, 2, 3]', '"just a string"', '17', 'null',
                     '{"v": 1, "items": "abc"}', '{"v": 1, "items": ["a"]}',
                     '{"v": 1, "ts": "yesterday", "items": [{"text": "x"}]}',
                     '{"v": 1, "ts": 1, "items": [{"id": "1"}]}'):
            with open(todos.state_path(SID), "w", encoding="utf-8") as f:
                f.write(blob)
            for argv in (["--all"], ["--all", "--json"]):
                rc, _out, _err = self.run_cli(argv)
                self.assertEqual(rc, 0, "helm todos %s died on %s"
                                 % (" ".join(argv), blob))
            with mock.patch.object(todos, "this_session", lambda: SID):
                self.assertEqual(self.run_cli([])[0], 0, "this-seat view: " + blob)
            self.assertEqual(web.QUERY_API["/api/todos"]({})[1], 200)
            rep = seats.roster_report()
            self.assertTrue(any(s["seat"] == "builder" for s in rep["seats"]))
            with open(todos.state_path(SID), "w", encoding="utf-8") as f:
                f.write(good)

    def test_the_hot_path_heals_a_corrupt_mirror_file(self):
        """A non-object todos.json used to make capture() raise BEFORE its
        own write, so the mirror could never replace the bad file: one stray
        byte killed that session's mirror permanently. The next write wins."""
        self.write(("first", "in_progress"))
        with open(todos.state_path(SID), "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        self.assertEqual(todos.state(SID), {})
        self.write(("second", "in_progress"))
        self.assertEqual(todos.digest(todos.state(SID)["items"])["active"],
                         "second")

    def test_fleet_ships_items_only_when_asked(self):
        """/api/todos is polled every few seconds and the table reads only
        the digest — every seat's full list on that wire is pure cost."""
        self.seat("builder", SID)
        self.write(*[("item %d" % i, "pending") for i in range(40)])
        self.assertNotIn("items", todos.fleet()["seats"][0])
        self.assertNotIn("items", web.QUERY_API["/api/todos"]({})[0]["seats"][0])
        detail = todos.fleet(items=True)["seats"][0]
        self.assertEqual(len(detail["items"]), 40)
        rc, out, _ = self.run_cli(["--all", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)["seats"][0]["items"]), 40)

    def test_stale_unclaimed_sessions_are_capped_not_a_wall(self):
        """gc keeps reflex-state for 30 days, so a busy estate carries
        hundreds of dead session dirs. The fleet view shows the freshest and
        COUNTS the rest — a thousand orphan rows is the attention tax this
        bridge exists to avoid, not a fleet view."""
        n = todos.ORPHAN_CAP + 12
        for i in range(n):
            record.record(self.ev(sid="orphan-%03d" % i,
                                  tin={"todos": todo_list(("work %d" % i,
                                                           "in_progress"))}))
            st = todos.state("orphan-%03d" % i)
            st["ts"] = 1_000_000 + i          # deterministic recency order
            pk.write_json(todos.state_path("orphan-%03d" % i), st)
        rep = todos.fleet()
        self.assertEqual(len(rep["orphans"]), todos.ORPHAN_CAP)
        self.assertEqual(rep["orphans_hidden"], 12)
        self.assertEqual(rep["orphans"][0]["active"], "work %d" % (n - 1))
        rc, out, _ = self.run_cli(["--all"])
        self.assertEqual(rc, 0)
        self.assertIn("12 older unclaimed sessions hidden", out)
        self.assertEqual(len([l for l in out.splitlines() if "work " in l]),
                         todos.ORPHAN_CAP)

    def test_bad_flag_is_usage_rc2(self):
        rc, _out, err = self.run_cli(["--nope"])
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm todos", err)

    def test_verb_is_wired_into_the_cli(self):
        from helm import cli
        self.assertIn("todos", cli.VERBS)
        self.assertIn("todos", cli._VERB_HELP)


class RosterAndWebTest(TodosBase):
    def test_roster_report_carries_the_current_task(self):
        self.seat("builder", SID)
        self.write(("wire the bridge", "in_progress"), ("survey", "completed"))
        rep = seats.roster_report()
        row = next(s for s in rep["seats"] if s["seat"] == "builder")
        self.assertEqual(row["todo"]["active"], "wire the bridge")
        self.assertEqual((row["todo"]["done"], row["todo"]["total"]), (1, 2))

    def test_roster_row_todo_is_none_without_a_mirror(self):
        self.seat("idle-seat", "no-such-session")
        rep = seats.roster_report()
        row = next(s for s in rep["seats"] if s["seat"] == "idle-seat")
        self.assertIsNone(row["todo"])

    def test_chat_seats_prints_the_task(self):
        self.seat("builder", SID)
        self.write(("wire the bridge", "in_progress"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            seats.cmd("seats", [])
        self.assertIn("wire the bridge", out.getvalue())

    def test_api_todos_endpoint(self):
        self.seat("builder", SID)
        self.write(("owner-visible work", "in_progress"))
        self.assertIn("/api/todos", web.QUERY_API)
        body, status = web.QUERY_API["/api/todos"]({})
        self.assertEqual(status, 200)
        self.assertEqual(body["seats"][0]["active"], "owner-visible work")

    def test_api_todos_is_fail_open(self):
        with mock.patch.object(todos, "fleet",
                               mock.Mock(side_effect=RuntimeError("boom"))):
            body, status = web.QUERY_API["/api/todos"]({})
        self.assertEqual(status, 200)
        self.assertTrue(body["unavailable"])

    def test_web_ui_renders_the_panel(self):
        html = web_ui_loader.read_text()
        # fleet-todos consolidated into the roster tab's TASK column
        # (rosterTask reads /api/todos via ROSTER_TODOS) — the standalone
        # #ledgertodos/fleetTodos panel was retired in the roster consolidation.
        self.assertIn("function rosterTask", html)
        self.assertIn("ROSTER_TODOS", html)
        self.assertIn("/api/todos", html)


if __name__ == "__main__":
    unittest.main()


class LedgerBridgeTest(TodosBase):
    """The two writers. The read window has existed for a while; these are
    the legs that let a teammate durably interact with what it shows, and
    the destructive one is the reason every arm here proves a FILE state
    rather than the absence of an exception (owner's acceptance bar: "prove
    it with the local file absent, not with 'no error raised'")."""

    def setUp(self):
        super().setUp()
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        self.ledger = os.path.join(self.tmp, "tasks.jsonl")
        self.pdir = todos.personal_dir(SID)
        os.makedirs(self.pdir, exist_ok=True)

    def personal(self, pid, subject, **over):
        row = {"id": str(pid), "subject": subject, "status": "pending"}
        row.update(over)
        full = os.path.join(self.pdir, "%s.json" % pid)
        with open(full, "w", encoding="utf-8") as fh:
            json.dump(row, fh)
        return full

    def file_row(self, title, owner, status="open"):
        row, err = tasks.add(title, owner, path=self.ledger)
        self.assertIsNone(err, err)
        if status != "open":
            _r, err = tasks.update(row["id"], path=self.ledger, status=status,
                                   closed_reason="done")
            self.assertIsNone(err, err)
        return row

    # ── promote ────────────────────────────────────────────────────────
    def test_promote_files_a_row_AND_stamps_the_id_back(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive controls are the row's title/owner and the STAMP read back OUT of the personal file; the assertIsNone(err) lines are guards, not the assertion
        """The stamp is the whole mechanism: without it the next pass has to
        guess the pairing by title, which is what makes the destructive leg
        unsafe."""
        full = self.personal(1, "wire the bridge")
        row, err = todos.promote(SID, "1", "cj", path=self.ledger)
        self.assertIsNone(err, err)
        self.assertEqual(row["title"], "wire the bridge")
        self.assertEqual(row["owner"], "cj")
        got = tasks.get(row["id"], path=self.ledger)   # really in the ledger
        self.assertIsNotNone(got)
        self.assertEqual(got["id"], row["id"])
        with open(full, encoding="utf-8") as fh:
            back = json.load(fh)
        self.assertEqual(back[todos.STAMP], row["id"])

    def test_a_SECOND_promote_returns_THE_SAME_row_never_a_duplicate(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the exact ledger population (len(rows) == 1) plus the two ids being equal — an empty ledger fails both
        """A seat retries after a crash. A bridge that filed again would
        reproduce the 96%-duplicate disease in the DURABLE store."""
        self.personal(1, "wire the bridge")
        first, err = todos.promote(SID, "1", "cj", path=self.ledger)
        self.assertIsNone(err, err)
        again, err = todos.promote(SID, "1", "cj", path=self.ledger)
        self.assertIsNone(err, err)
        self.assertEqual(again["id"], first["id"])
        rows, unavailable = tasks.snapshot(path=self.ledger)
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(len(rows), 1)

    def test_a_LOST_STAMP_does_not_file_a_SECOND_row(self):  # noqa: VACUOUS_ASSERTION — three unconditional positive controls on the observables that matter: the stamp POPPED equals the filed id (proving it was written), the ledger holds EXACTLY 1 row after the retry, and the stamp is READ BACK restored afterwards
        """An independent review found: promote appended then stamped, so a crash or a concurrent
        run between those two writes left a ledger row nobody could find —
        and the retry, seeing no stamp, filed ANOTHER. A bridge whose retry
        duplicates rows in the DURABLE store is the disease it was built to
        cure, moved somewhere worse."""
        full = self.personal(1, "wire the bridge")
        first, err = todos.promote(SID, "1", "cj", path=self.ledger)
        self.assertIsNone(err, err)
        # simulate the crash: the ledger row exists, the stamp never landed
        with open(full, encoding="utf-8") as fh:
            row = json.load(fh)
        self.assertEqual(row.pop(todos.STAMP), first["id"])   # it WAS stamped
        with open(full, "w", encoding="utf-8") as fh:
            json.dump(row, fh)

        again, err = todos.promote(SID, "1", "cj", path=self.ledger)
        self.assertIsNone(err, err)
        self.assertEqual(again["id"], first["id"])            # found by backref
        rows, _u = tasks.snapshot(path=self.ledger)
        self.assertEqual(len(rows), 1, "the retry filed a duplicate")
        # and it re-stamped, so the mapping is durable again
        with open(full, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)[todos.STAMP], first["id"])

    def test_the_ledger_row_CITES_the_personal_item(self):
        """The back-reference is what makes the recovery above possible, so
        it is pinned as its own fact rather than only implied."""
        self.personal(2, "cite me")
        row, err = todos.promote(SID, "2", "cj", path=self.ledger)
        self.assertIsNone(err, err)
        self.assertIn("local:%s/2" % SID, row["refs"])

    def test_an_UNREADABLE_ledger_refuses_to_FILE(self):
        """Filing onto a ledger you cannot read is how a duplicate is created
        with no way to notice it."""
        self.personal(3, "do not file me")
        with mock.patch.object(tasks, "snapshot",
                               return_value=({}, "ledger unreadable")):
            row, err = todos.promote(SID, "3", "cj", path=self.ledger)
        self.assertIsNone(row)
        self.assertIn("unreadable", err)

    def test_owner_comparison_is_CASE_INSENSITIVE(self):
        """An independent review found: a seat named CJ and a row owned by cj are the same seat,
        and treating them as different silently keeps a foreign row."""
        twin = self.file_row("someone else has this", "CODEX")
        full = self.personal(4, "someone else has this",
                             **{todos.STAMP: twin["id"]})
        self.assertTrue(os.path.exists(full))
        rep = todos.demote(SID, "codex", path=self.ledger)
        self.assertEqual(rep["removed"], [])       # same seat, different case
        self.assertEqual(rep["kept"], 1)
        self.assertTrue(os.path.exists(full))

    def test_promote_refuses_an_unowned_row(self):
        self.personal(1, "wire the bridge")
        row, err = todos.promote(SID, "1", "", path=self.ledger)
        self.assertIsNone(row)
        self.assertIn("owner", err)

    # ── demote ─────────────────────────────────────────────────────────
    def test_demote_drops_a_row_whose_twin_is_CLOSED_and_keeps_the_bytes(self):
        """THE damage case: 3 rows read `pending` locally while the ledger had
        them CLOSED, and one re-dispatched work another seat had finished."""
        twin = self.file_row("already done", "cj", status="closed")
        full = self.personal(1, "already done", **{todos.STAMP: twin["id"]})
        rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(len(rep["removed"]), 1)
        self.assertNotIn("failed", rep["removed"][0])
        # THE FILE, not the report: the acceptance bar is the local copy gone
        self.assertFalse(os.path.exists(full))
        # and recoverable — an unattended destructive sweep owes an undo
        kept = os.path.join(rep["trash"], os.path.basename(full))
        self.assertTrue(os.path.exists(kept))
        with open(kept, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["subject"], "already done")

    def test_demote_drops_a_row_the_ledger_says_is_ANOTHER_seats(self):
        twin = self.file_row("someone else has this", "codex")
        full = self.personal(2, "someone else has this",
                             **{todos.STAMP: twin["id"]})
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: a fixture
        # that silently failed to write would make "the file is gone" pass
        # without the sweep doing anything at all.
        self.assertTrue(os.path.exists(full))
        rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(len(rep["removed"]), 1)
        self.assertIn("owned by codex", rep["removed"][0]["why"])
        self.assertFalse(os.path.exists(full))

    def test_a_row_that_is_STILL_MINE_and_OPEN_survives(self):
        """The positive control. A sweep that removed everything would pass
        every arm above."""
        twin = self.file_row("my live work", "cj")
        full = self.personal(3, "my live work", **{todos.STAMP: twin["id"]})
        rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(rep["removed"], [])
        self.assertEqual(rep["kept"], 1)
        self.assertTrue(os.path.exists(full))

    def test_an_UNSTAMPED_row_is_NEVER_touched(self):
        """The safety property, stated as a test because it is the difference
        between a bridge and a shredder: no stamp means no proven twin, and
        title similarity must never authorize a delete."""
        self.file_row("wire the bridge", "codex", status="closed")
        full = self.personal(4, "wire the bridge")     # same TITLE, no stamp
        rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(rep["removed"], [])
        self.assertEqual(rep["unstamped"], 1)
        self.assertTrue(os.path.exists(full))

    def test_a_stamp_the_ledger_cannot_resolve_is_KEPT(self):
        """An unreadable twin means CANNOT TELL, never NOT MINE. Fail-closed
        in the direction that preserves the row."""
        full = self.personal(5, "orphaned stamp",
                             **{todos.STAMP: "task/99999"})
        rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(rep["removed"], [])
        # ORPHANED, not merely "unresolved": this stamp PARSES and names a row
        # the ledger does not hold — the ledger moved on. A stamp that cannot
        # be READ is a different fact with a different owner, and they shared
        # one nameless counter until an independent review split them.
        self.assertEqual(len(rep["orphaned"]), 1)
        self.assertEqual(rep["orphaned"][0]["row"], "task/99999")
        self.assertEqual(rep["unparseable"], [])
        self.assertTrue(os.path.exists(full))

    def test_the_two_CANNOT_TELL_populations_are_reported_APART(self):
        """An independent review found this, and the distinction IS the finding.

        `demote` honoured "UNPARSEABLE is not ABSENT" in the DECISION — both
        KEEP, correctly, because cannot-tell must never delete — and then
        collapsed them into ONE nameless counter in the REPORT, which is the
        half an operator reads. A corrupt stamp means a WRITER BUG in a store
        helm does not own; an orphan means the LEDGER MOVED ON. Different
        owners, different fixes, same number, and neither carried a row id —
        so an unattended sweep could be audited for what it DELETED but not
        for what it SILENTLY DECLINED TO UNDERSTAND.

        The 7-digit stamp is what actually reaches the None branch. The
        review's first repro used "task/NOT-A-NUMBER" and PROVED NOTHING, because
        normalize_id returns 'task/not-a-number' for that, so both rows took
        the ABSENT branch — a probe bound to the wrong claim, caught by its
        own author before it became a finding."""
        self.assertIsNone(tasks.normalize_id("1234567"))          # MUST-HIT
        self.assertEqual(tasks.normalize_id("424242"), "task/424242")
        bad = self.personal(11, "corrupt stamp", **{todos.STAMP: "1234567"})
        gone = self.personal(12, "orphan stamp", **{todos.STAMP: "task/424242"})
        rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(rep["removed"], [])
        self.assertEqual(len(rep["unparseable"]), 1)
        self.assertEqual(rep["unparseable"][0]["stamp"], "1234567")
        self.assertEqual(len(rep["orphaned"]), 1)
        self.assertEqual(rep["orphaned"][0]["row"], "task/424242")
        # BOTH ARE KEPT — the split is about the REPORT, never the decision
        self.assertTrue(os.path.exists(bad))
        self.assertTrue(os.path.exists(gone))

    def test_a_CANCELLED_removal_is_NOT_counted_as_a_removal(self):
        """A second independent finding: the report said a thing the
        filesystem did not.

        `removed` was appended to BEFORE _trash ran, and _trash stamped a
        `failed` key onto that same row on failure — so on the one path built
        never to lose a row, the row STAYED in `removed` while its FILE STILL
        EXISTED. _trash's own docstring says a failure CANCELS the removal:
        true of the disk, false of the report, and any consumer doing
        len(rep["removed"]) over-reported deletions.

        THIS PATH HAD NO TEST AT ALL, which is why it survived a review that
        went looking for exactly this class."""
        twin = self.file_row("already done", "cj", status="closed")
        full = self.personal(13, "already done", **{todos.STAMP: twin["id"]})
        with mock.patch("helm.todos.os.remove",
                        side_effect=OSError("no space left on device")):
            rep = todos.demote(SID, "cj", path=self.ledger)
        # THE FILE IS STILL THERE, so the report must not say it was removed
        self.assertTrue(os.path.exists(full))
        self.assertEqual(rep["removed"], [])
        self.assertEqual(len(rep["failed"]), 1)
        self.assertIn("no space left", rep["failed"][0]["failed"])
        self.assertEqual(rep["failed"][0]["row"], twin["id"])
        # UNCONDITIONAL POSITIVE CONTROL: without the fault the SAME row
        # removes and lands in `removed` with an empty `failed`. Without it,
        # every assertion above would pass on a sweep that removes nothing.
        rep2 = todos.demote(SID, "cj", path=self.ledger)
        self.assertFalse(os.path.exists(full))
        self.assertEqual(len(rep2["removed"]), 1)
        self.assertEqual(rep2["failed"], [])

    def test_a_file_REPLACED_under_the_sweep_is_NOT_deleted(self):
        """P0 DESTRUCTIVE — the worst defect an independent review found in
        this sweep, and it was in the author's own code.

        `personal_rows` reads every file up front and the decision is taken
        against that CACHED row, but the unlink happened BY PATHNAME with no
        identity check. The review's probe atomically replaced the file with a NEW
        LIVE UNSTAMPED row just before os.remove, and helm: DELETED that row,
        reported it removed while citing the OLD closed twin as the reason,
        and wrote the OLD bytes to trash. A live row destroyed, a false reason
        recorded, and an undo holding something that was never there — on the
        one leg built so that no row is ever lost.

        The cure is delete-if-same-identity: re-read, compare (id, stamp)
        against the row actually judged, and REFUSE on a mismatch. helm never
        evaluated the new content, so it must not delete it."""
        twin = self.file_row("already done", "cj", status="closed")
        full = self.personal(21, "already done", **{todos.STAMP: twin["id"]})
        real_read = pk.read_json

        def swap_after_first_read(path, default=None):
            got = real_read(path, default)
            if path == full and not getattr(swap_after_first_read, "fired", False):
                swap_after_first_read.fired = True
                with open(full, "w", encoding="utf-8") as fh:
                    json.dump({"id": "21", "subject": "BRAND NEW LIVE WORK",
                               "status": "in_progress"}, fh)
            return got

        with mock.patch("helm.pk.read_json", swap_after_first_read):
            rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertTrue(swap_after_first_read.fired, "the race never armed")
        # THE FILE, not the report: the acceptance bar is that the row a human
        # wrote between the read and the unlink is still on disk.
        self.assertTrue(os.path.exists(full))
        with open(full, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["subject"], "BRAND NEW LIVE WORK")
        self.assertEqual(rep["removed"], [])
        self.assertEqual(len(rep["failed"]), 1)
        self.assertIn("changed under the sweep", rep["failed"][0]["failed"])
        # UNCONDITIONAL POSITIVE CONTROL, and it needs a FRESH row rather than
        # a re-run: after the race the file holds an UNSTAMPED row, so a
        # second sweep declines it for an unrelated reason and would prove
        # nothing. An identically-built row that is NOT raced must still be
        # removed — otherwise every assertion above passes on a sweep that
        # removes nothing at all.
        control = self.personal(24, "already done", **{todos.STAMP: twin["id"]})
        rep2 = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(len(rep2["removed"]), 1)
        self.assertFalse(os.path.exists(control))
        self.assertTrue(os.path.exists(full))       # the raced row STILL safe

    def test_a_DRY_run_claims_no_removal_it_did_not_make(self):
        """An independent review found: `removed` meant removed only on the APPLY path. A dry
        run appended anyway, so `--dry-run --json` published removed:[row]
        about a file still on disk and the text summary said "1 dropped"
        under a per-row line reading "would drop".

        My previous commit CLAIMED "removed means removed by construction".
        Naming a contract does not establish it — the sentence has to be true
        on every path the function has."""
        twin = self.file_row("already done", "cj", status="closed")
        full = self.personal(22, "already done", **{todos.STAMP: twin["id"]})
        rep = todos.demote(SID, "cj", path=self.ledger, apply=False)
        self.assertTrue(os.path.exists(full))
        self.assertEqual(rep["removed"], [])
        self.assertEqual(len(rep["would_remove"]), 1)
        self.assertEqual(rep["would_remove"][0]["row"], twin["id"])
        # POSITIVE CONTROL: the same row DOES land in `removed` when applied,
        # so the empty list above is about the dry path and not about a sweep
        # that never removes anything.
        rep2 = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(len(rep2["removed"]), 1)
        self.assertFalse(os.path.exists(full))

    def test_a_file_that_does_NOT_PARSE_is_reported_not_swallowed(self):
        """An independent review found the THIRD declined-to-understand cause, which the two
        stamp buckets did not cover. Skipping an unparseable file is right —
        a row helm cannot read is one it cannot prove anything about — but
        skipping it SILENTLY meant a directory holding nothing but broken JSON
        reported "0 row(s) still yours and open", which reads as a clean
        list."""
        broken = os.path.join(self.pdir, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(len(rep["unreadable"]), 1)
        self.assertEqual(rep["unreadable"][0]["file"], broken)
        self.assertTrue(os.path.exists(broken))     # never repaired, never removed
        # POSITIVE CONTROL: a READABLE row in the same directory still parses
        # and is still judged, so `unreadable` is about this file and not
        # about a reader that stopped reading.
        twin = self.file_row("already done", "cj", status="closed")
        keep = self.personal(23, "already done", **{todos.STAMP: twin["id"]})
        rep2 = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(len(rep2["unreadable"]), 1)
        self.assertEqual(len(rep2["removed"]), 1)
        self.assertFalse(os.path.exists(keep))

    def test_an_UNREADABLE_ledger_refuses_the_whole_sweep_and_says_so(self):
        """CANNOT SEE is not CANNOT MATCH. Asking the ledger per row would
        make every row answer "no twin" — a report of N orphaned stamps over
        a ledger that was merely unavailable. Nothing is removed either way;
        what differs is whether the operator is told the truth about why."""
        twin = self.file_row("already done", "cj", status="closed")
        full = self.personal(7, "already done", **{todos.STAMP: twin["id"]})
        with mock.patch.object(tasks, "snapshot",
                               return_value=({}, "ledger unreadable")):
            rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(rep["unavailable"], "ledger unreadable")
        self.assertEqual(rep["removed"], [])
        self.assertEqual(rep["orphaned"], [])    # NOT reported as orphans
        self.assertEqual(rep["unparseable"], [])  # nor as corrupt stamps
        self.assertTrue(os.path.exists(full))

    def test_an_UNNAMEABLE_seat_refuses_BOTH_verbs_rather_than_degrading(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the SECOND half: the same row, same command, WITH --owner, reports "would drop". That proves the row is removable, which is what makes its survival under the refusal mean something
        """FOUND BY DOGFOODING THIS ON THE SEAT THAT WROTE IT. A pane can be
        live and working with HELM_CHAT_NAME absent from the shell it hands a
        subprocess. promote refused (right, but with no way forward). demote was
        WORSE: its owned-by-another-seat comparison needs a seat, so with none
        it silently kept every foreign row and printed a clean sweep. A safety
        rule that evaporates with an unset env var is the failure this leg
        exists to prevent."""
        os.environ.pop("HELM_CHAT_NAME", None)
        # THE DEFAULT ledger, not the fixture's side file: this arm drives the
        # CLI, and the CLI resolves its own path — a twin filed elsewhere
        # would read as an orphan stamp and the arm would pass for the wrong
        # reason. HELM_HOME is already this test's tmpdir, so it stays local.
        twin, err = tasks.add("someone else's", "codex")
        self.assertIsNone(err, err)
        full = self.personal(8, "someone else's", **{todos.STAMP: twin["id"]})
        with mock.patch.object(todos, "this_session", return_value=SID):
            rc, out, err = self.cli(["demote"])
            self.assertEqual(rc, 2)
            self.assertIn("--owner", err)
            self.assertTrue(os.path.exists(full))    # and it did NOT sweep
            # the SAME command with the seat named does the real work
            rc2, out2, _err2 = self.cli(["demote", "--dry-run",
                                         "--owner", "cj"])
        self.assertEqual(rc2, 0)
        self.assertIn("would drop", out2)

    def cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = todos.cmd_todos(args)
        return rc, out.getvalue(), err.getvalue()

    def test_an_UNPARSEABLE_stamp_is_KEPT_without_leaning_on_a_far_module(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the MUST-HIT assertIsNone(normalize_id(...)) proving the stamp really is unparseable, plus the sibling arms in this class that DO remove rows, so 'removed == []' here is a decision rather than a leg that never fires
        """A second independent review attached the mechanism to a finding
        the first could not reproduce. normalize_id returns None for "not an id
        at all" — a different fact from "no such row" — and my `or ""` handed
        a falsy key to known.get(). Safe only because eventledger rejects
        falsy ids TWO MODULES AWAY: an accident, untested, one refactor from
        deleting valid rows in a store helm does not own.

        The arm proves the decision is made HERE: even with a lookup that
        would happily answer a falsy key, the row survives."""
        full = self.personal(9, "garbage stamp", **{todos.STAMP: "!!!not-an-id"})
        self.assertIsNone(tasks.normalize_id("!!!not-an-id"))   # MUST-HIT
        rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(rep["removed"], [])
        # UNPARSEABLE: normalize_id refused it outright (the MUST-HIT above),
        # so the ledger could never be asked — a WRITER wrote it wrong. That
        # is not the same fact as a stamp that parses and finds nothing.
        self.assertEqual(len(rep["unparseable"]), 1)
        self.assertEqual(rep["unparseable"][0]["stamp"], "!!!not-an-id")
        self.assertEqual(rep["orphaned"], [])
        self.assertTrue(os.path.exists(full))

        # THE MUTATION THE REVIEW RAN: a ledger that answers a falsy key must still
        # not cost the row, because the keep decision no longer depends on it.
        twin = self.file_row("a real closed row", "cj", status="closed")
        with mock.patch.object(tasks, "snapshot",
                               return_value=({"": twin}, None)):
            rep2 = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(rep2["removed"], [], "a falsy-key hit deleted a row")
        self.assertTrue(os.path.exists(full))

    def test_dry_run_reports_the_same_rows_and_removes_NOTHING(self):
        """CONTRACT CHANGED DELIBERATELY, and this arm is where it was pinned.

        It used to assert len(rep["removed"]) == 1 on a dry run WHILE the file
        still existed — which is precisely the defect the review measured: it made
        `removed` mean "removed" on one path and "would be removed" on the
        other, so `--dry-run --json` published removals that had not happened
        and the summary said "1 dropped" under a line reading "would drop".

        The arm's NAME was always right and its assertion was not. The rows are
        still reported — that half of the contract is intact and asserted below
        — they are reported in `would_remove`, which is the bucket whose name
        is true on the path it belongs to."""
        twin = self.file_row("already done", "cj", status="closed")
        full = self.personal(6, "already done", **{todos.STAMP: twin["id"]})
        rep = todos.demote(SID, "cj", path=self.ledger, apply=False)
        self.assertEqual(len(rep["would_remove"]), 1)   # REPORTED
        self.assertEqual(rep["removed"], [])            # but NOT removed
        self.assertTrue(os.path.exists(full))
        self.assertIsNone(rep["trash"])
