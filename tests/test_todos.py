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
