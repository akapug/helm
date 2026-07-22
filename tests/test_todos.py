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

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import chat, pk, record, seats, todos, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "HELM_CHAT_ROOM",
            "HELM_CHAT_OWNER_NAMES", "HELM_TODO_POST",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL")

SID = "sess-todo-1"


def todo_list(*pairs):
    return [{"content": t, "status": s} for t, s in pairs]


class TodosBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-todos-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""   # no signer, no node probe
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "david"

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
        self.seat("polyana-codex")
        record.record(self.ev(tool="TaskCreate", tin={"subject": "Scala gate"},
                              resp="Task #41 created successfully"))
        self.assertEqual(self.room(), [], "a new PENDING task is not news")
        record.record(self.ev(tool="TaskUpdate",
                              tin={"taskId": "41", "status": "in_progress"}))
        rows = self.room()
        self.assertEqual(len(rows), 1)
        self.assertIn("Scala gate", rows[0]["text"])
        self.assertEqual(rows[0]["from"], "polyana-codex")

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
        html = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "helm", "web_ui.html"),
            encoding="utf-8").read()
        self.assertIn('id="ledgertodos"', html)
        self.assertIn("fleetTodos", html)
        self.assertIn("/api/todos", html)


if __name__ == "__main__":
    unittest.main()
