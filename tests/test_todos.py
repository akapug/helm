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
import ast
import contextlib
import errno
import fcntl
import gc
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import (chat, fsops, pk, record, seats, tasks, todos, web,  # noqa: E402
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
            # test_env_hygiene caught on the first gate of this lane.
            "CLAUDE_CONFIG_DIR")

SID = "sess-todo-1"


def todo_list(*pairs):
    return [{"content": t, "status": s} for t, s in pairs]






@contextlib.contextmanager
def _both(a, b):
    """Two patches as one context, so an arm that must cover both the path
    form and the descriptor form of a call installs them together."""
    with a:
        with b:
            yield


@contextlib.contextmanager
def _caps(qdir):
    """The directory descriptors a real caller of _sweep_claim already holds.

    There is no capless fallback any more: the preflight guarantees
    the primitives exist, so a path-resolving branch beside the anchored one
    would be a second implementation nothing exercises — and every arm would
    silently pass through whichever branch happened to run. A test that wants
    to call the sweep therefore has to hold what its caller holds."""
    pfd = todos._dir_fd(os.path.dirname(qdir) or ".")
    qfd = todos._dir_fd(qdir)
    try:
        yield pfd, qfd
    finally:
        for fd in (pfd, qfd):
            if fd is not None:
                os.close(fd)



def _digest_of(path):
    """A digest of a path, for FIXTURES ONLY.

    helm's own digest is taken from an open descriptor now; the path-based
    version was dead production code retaining old machinery, and
    keeping it alive so tests could call it would have kept a second, unused
    implementation of the module's most safety-critical read. Fixtures build
    state from paths — that is what a fixture IS — so the path version belongs
    here, where nothing in the anchored path can reach it.

    Returns None when the path is absent, matching the helper it replaced —
    fixtures call it on a claim they deliberately built EMPTY, and raising
    there turned a fixture into an error instead of the state it describes."""
    import hashlib
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None



def _mint_claim(d, sid, prefix=None):
    """A claim directory AND its undo directory, the way _trash mints them.

    PRODUCTION MINTS BOTH TOGETHER, so a real crashed claim always has an undo
    directory even when the undo itself was never published. A fixture that
    creates only the claim models a topology production cannot produce — and
    under the strict undo state machine it reads as UNDECIDABLE (the root or
    the claim's undo dir is missing), which is correct about the fixture and
    says nothing about the case the test meant (row 86).

    The realistic missing-undo case is the SOLE MISSING MEMBER: the directory
    exists and the member inside it does not."""
    import tempfile as _tf
    q = _tf.mkdtemp(dir=d, prefix=prefix or todos.CLAIM_PREFIX)
    _derived_undo(sid, q, ".keep")            # creates the undo directory
    return q


def _publishes_to_trash(dst, **kw):
    """Is THIS link/rename the undo publish?

    The destination used to be a whole path, so `TRASH in dst` answered it.
    Member operations are anchored to directory descriptors now, so `dst` is a
    BARE NAME and that test silently matches nothing — a discriminator that
    stops discriminating disarms the injection just as thoroughly as naming
    the wrong syscall did, and just as quietly.

    The destination DIRECTORY is what carries the answer, and it is reachable
    from the descriptor rather than from the string."""
    fd = kw.get("dst_dir_fd")
    if fd is None:
        return todos.TRASH in str(dst)
    try:
        return todos.TRASH in os.readlink("/proc/self/fd/%d" % fd)
    except OSError:
        return False


def _manifest(path):
    """A claim manifest as a fixture sees it — through production's framing.

    Fixtures that WRITE a bare object still work: that is the legacy shape and
    the reader accepts it deliberately, so pre-format claims are not orphaned.
    Fixtures that READ one cannot use json.load any more, because production
    now appends RS-framed snapshots and the latest complete one is the state."""
    with open(path, "rb") as fh:
        frames = todos._frames(fh.read())
    return frames[-1] if frames else None


def _fd_targets():
    """THE EXACT SET OF OPEN DESCRIPTORS, by target, not a count.

    The leak arms asserted `after - before < 5` under names promising zero, so
    one to four real leaks per run passed. A tolerance on a leak
    assertion is a leak allowance. Comparing targets also NAMES what leaked
    rather than only counting it."""
    gc.collect()                 # settle anything a fault left to finalisation
    out = []
    for n in os.listdir("/proc/self/fd"):
        try:
            out.append(os.readlink("/proc/self/fd/%s" % n))
        except OSError:
            pass
    return out


def _assert_no_leak(case, before, after, faults):
    from collections import Counter
    # the listdir handle used to take each sample is itself transient
    keep = lambda t: "/proc/" not in t
    delta = Counter(t for t in after if keep(t)) - \
        Counter(t for t in before if keep(t))
    case.assertEqual(dict(delta), {},
                     "descriptors leaked across %d faults: %r"
                     % (faults, dict(delta)))


def _undo_of(rep, row):
    """Where THIS row's undo actually landed, read from the REPORT rather than
    rebuilt by hand. The undo is per-transaction now (<trash>/<claim-id>/<row>),
    so a test that recomputes <trash>/<row> is asserting against a location the
    code stopped using — and would pass or fail for reasons unrelated to what
    it means to check."""
    for e in rep.get("undos", ()):
        if e["row"] == row:
            return e["undo"]
    raise AssertionError("no undo reported for %r; rep=%r" % (row, rep))



def _live_claims(d):
    """Claim directories still IN FLIGHT — terminal residuals excluded.

    "No claim REMAINS" and "no claim is UNRESOLVED" were the same question
    while cleanup deleted whatever it found. They stopped being the same the
    moment cleanup began preserving bytes in place: a residual is a decision
    already taken and durably recorded, so a test that asserts emptiness is
    now asserting that the preservation did NOT happen. This is the question
    those assertions actually meant."""
    out = []
    for n in sorted(os.listdir(d)):
        if not n.startswith(todos.CLAIM_PREFIX):
            continue
        meta = {}
        try:
            meta = _manifest(os.path.join(d, n, todos.CLAIM_META)) or {}
        except Exception:
            pass
        if meta.get("state") != todos.RESIDUAL:
            out.append(n)
    return out


def _derived_undo(sid, qdir, row):
    """Where the CODE will look for THIS claim's undo, derived the same way
    `_recover_one` derives it, and created.

    A fixture that invents its own undo directory is planting evidence
    somewhere the code stopped looking: recovery would find no undo, take a
    different branch, and the test would report an outcome that says nothing
    about what it meant to check. The claim dir must therefore exist BEFORE
    the undo, because the undo now descends from it."""
    from helm import record
    d = os.path.join(record.session_dir(sid), todos.TRASH,
                     os.path.basename(qdir))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, row)

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
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"

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
    """task/327 — the two writers. The read window has existed for a while;
    these are the legs that let a teammate durably interact with what it
    shows, and the destructive one is the reason every arm here proves a
    FILE state rather than the absence of an exception (owner's acceptance
    bar: "prove it with the local file absent, not with 'no error raised'")."""

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
        """promote appended then stamped, so a crash or a concurrent
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

    def test_promote_reports_a_MALFORMED_target_never_false_absence(self):
        broken = os.path.join(self.pdir, "3.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        row, err = todos.promote(SID, "3", "cj", path=self.ledger)
        self.assertIsNone(row)
        self.assertIn("exists but is unreadable", err)
        rows, unavailable = tasks.snapshot(path=self.ledger)
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(rows, {})
        self.assertTrue(os.path.exists(broken))

        with open(broken, "w", encoding="utf-8") as fh:
            json.dump({"id": "3", "subject": "now readable",
                       "status": "pending"}, fh)
        filed, err = todos.promote(SID, "3", "cj", path=self.ledger)
        self.assertIsNone(err, err)
        self.assertEqual(filed["title"], "now readable")
        self.assertEqual(len(tasks.snapshot(path=self.ledger)[0]), 1)

    def test_an_INACCESSIBLE_personal_directory_refuses_never_zero_rows(self):
        with mock.patch("helm.todos.os.listdir",
                        side_effect=PermissionError("permission denied")):
            rep = todos.demote(SID, "cj", path=self.ledger)
            self.assertIn("permission denied", rep["personal_unavailable"])
            with mock.patch.object(todos, "this_session", return_value=SID):
                rc, out, err = self.cli(["demote", "--owner", "cj"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("personal task directory unreadable", err)
        self.assertIn("sweep REFUSED", err)
        self.assertNotIn("nothing to drop", err)

        shutil.rmtree(self.pdir)
        rows, unavailable = todos.personal_rows(SID)
        self.assertEqual(rows, [])
        self.assertIsNone(unavailable)        # missing means a new empty session

    def test_owner_comparison_is_CASE_INSENSITIVE(self):
        """a seat named CJ and a row owned by cj are the same seat,
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
        kept = _undo_of(rep, os.path.basename(full))
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
        # one nameless counter until a review split them.
        self.assertEqual(len(rep["orphaned"]), 1)
        self.assertEqual(rep["orphaned"][0]["row"], "task/99999")
        self.assertEqual(rep["unparseable"], [])
        self.assertTrue(os.path.exists(full))

    def test_the_two_CANNOT_TELL_populations_are_reported_APART(self):
        """The distinction IS the finding.

        `demote` honoured "UNPARSEABLE is not ABSENT" in the DECISION — both
        KEEP, correctly, because cannot-tell must never delete — and then
        collapsed them into ONE nameless counter in the REPORT, which is the
        half an operator reads. A corrupt stamp means a WRITER BUG in a store
        helm does not own; an orphan means the LEDGER MOVED ON. Different
        owners, different fixes, same number, and neither carried a row id —
        so an unattended sweep could be audited for what it DELETED but not
        for what it SILENTLY DECLINED TO UNDERSTAND.

        The 7-digit stamp is what actually reaches the None branch. Their
        first repro used "task/NOT-A-NUMBER" and PROVED NOTHING, because
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
        """The second finding: the report said a thing the
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
        # THE FAULT IS INJECTED AT os.replace, NOT os.remove, BECAUSE THE
        # REMOVAL MOVED (task/420). Closing the unlink race meant claiming the
        # bytes with rename() FIRST and then placing them in the undo with
        # os.replace() — there is no longer an os.remove on this path for a
        # fault to land on, and patching one would inject into a call that
        # never happens, which is a test that cannot fail.
        #
        # THE CONTRACT THIS ARM PINS IS UNCHANGED and is the whole point: when
        # the filesystem refuses mid-removal, the row must come BACK and the
        # report must not claim it went. Under the new shape that means
        # `_put_back` restores it, and the failure text still names the cause.
        # THE SEAM MOVED AGAIN, and for the third time in this lane the reason
        # is that a fault-injection arm binds a SYSCALL, not a behaviour. The
        # undo now receives the OBJECT by rename rather than a copy by
        # os.replace, so the fault has to land on the rename that files it —
        # and only that one, since the CLAIM is also a rename and failing it
        # would test a different refusal entirely.
        # FOURTH RE-POINT, AND THE COMMENT ABOVE PREDICTED IT. The publish is
        # os.link now (link-and-preserve), not a rename at all, so an
        # injection on rename fires never and the removal SUCCEEDS — the arm
        # then fails by asserting a cancellation that did not happen, which is
        # the honest direction at least. The behaviour under test has not
        # moved once in four re-points: when the filesystem refuses mid
        # removal, the row comes BACK and the report does not claim it went.
        # Only the syscall carrying it keeps changing.
        real_link = os.link

        def fail_filing_only(src, dst, *a, **k):
            if _publishes_to_trash(dst, **k):
                raise OSError("no space left on device")
            return real_link(src, dst, *a, **k)

        with mock.patch("helm.todos.os.link", fail_filing_only):
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
        """The T1, P0 DESTRUCTIVE (task/394) — the worst defect in this
        lane and it was mine.

        `personal_rows` reads every file up front and the decision is taken
        against that CACHED row, but the unlink happened BY PATHNAME with no
        identity check. Their probe atomically replaced the file with a NEW
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
        # THE SEAM MOVED WITH THE CURE, and this is the honest reason: the
        # enumeration no longer reads rows with pk.read_json by pathname — it
        # opens each member THROUGH the pinned directory and parses the
        # descriptor, which is the capability change that closed a different
        # hole. An injection left on the old function stops firing silently,
        # and the arm then passes while testing nothing. It moves to where the
        # read actually happens.
        real_json, fired = todos._json_fd, []

        def swap_after_the_first_read(fd):
            got = real_json(fd)
            if not fired and isinstance(got, dict) and got.get("id") == "21":
                fired.append(1)
                with open(full, "w", encoding="utf-8") as fh:
                    json.dump({"id": "21", "subject": "BRAND NEW LIVE WORK",
                               "status": "in_progress"}, fh)
            return got

        with mock.patch.object(todos, "_json_fd", swap_after_the_first_read):
            rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertEqual(len(fired), 1, "the race never armed")
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
        """The T1: `removed` meant removed only on the APPLY path. A dry
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
        """The T1: the THIRD declined-to-understand cause, which the two
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
        # The aggregate must retain the population named by the per-file line.
        # Before this arm it printed a malformed file as "kept" and then ended
        # with "1 dropped, 0 kept", erasing the declined row from the summary.
        with mock.patch.object(todos, "this_session", return_value=SID), \
                mock.patch.object(todos, "demote", return_value=rep2):
            rc, out, _err = self.cli(["demote", "--owner", "cj"])
        self.assertEqual(rc, 0)
        self.assertIn("1 dropped, 0 row(s) still yours and open, "
                      "1 file(s) that do not parse", out)
        self.assertNotIn("1 dropped, 0 kept", out)
        self.assertTrue(os.path.exists(broken))

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

    def test_a_CORRUPT_ledger_classifies_UNREADABLE_never_ORPHANED(self):
        """With REAL corrupt bytes rather than a mock.

        The arm above mocks `tasks.snapshot` into confessing, which proved
        demote's refusal branch and NOT the seam the finding names: the
        default eventledger read SKIPS a malformed complete row, so a corrupt
        ledger came back ({}, None) — could-not-read narrated as
        ledger-moved-on. Safety held (nothing deleted) but every stamped row
        misfiled as ORPHANED, and an operator reading "the ledger moved on"
        goes and cleans up rows instead of restoring a ledger."""
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write("{this is not json at all\n")     # COMPLETE corrupt row
        # MUST-HIT: the tolerant read really does launder these bytes into
        # "known-empty" — the accident this cure exists to stop depending on.
        self.assertEqual(tasks.snapshot(path=self.ledger), ({}, None))
        full = self.personal(31, "live work", **{todos.STAMP: "task/424242"})
        rep = todos.demote(SID, "cj", path=self.ledger)
        self.assertIn("corrupt ledger line", str(rep["unavailable"]))
        self.assertEqual(rep["orphaned"], [])    # could-not-read, not moved-on
        self.assertEqual(rep["removed"], [])
        self.assertTrue(os.path.exists(full))
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: a VALID ledger that simply
        # does not hold task/424242 files the SAME row as ORPHANED — so the
        # empty list above is a classification, not a sweep that stopped
        # filing. Two fixture traps live here: file_row APPENDS (one corrupt
        # line rightly poisons the whole strict read no matter what follows,
        # so it must GO first), and a fresh ledger numbers from task/1 (a
        # stamp of task/1 would RESOLVE to the control row and read as kept).
        os.remove(self.ledger)
        self.file_row("some other row", "cj")            # ledger now valid
        rep2 = todos.demote(SID, "cj", path=self.ledger)
        self.assertIsNone(rep2["unavailable"])
        self.assertEqual(len(rep2["orphaned"]), 1)
        self.assertEqual(rep2["orphaned"][0]["row"], "task/424242")

    def test_promote_refuses_a_CORRUPT_ledger_with_real_bytes(self):
        """The filing leg shares the seam: promote on a laundered ({}, None)
        sees "no twin", files a duplicate, and nobody can notice. The mocked
        arm above pins the refusal; this one pins that REAL corrupt bytes
        reach it."""
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write("{this is not json at all\n")
        self.personal(32, "do not file me")
        row, err = todos.promote(SID, "32", "cj", path=self.ledger)
        self.assertIsNone(row)
        self.assertIn("unreadable", err)
        self.assertIn("corrupt ledger line", err)
        # POSITIVE CONTROL: the same personal row files once the bytes parse.
        os.remove(self.ledger)
        row2, err2 = todos.promote(SID, "32", "cj", path=self.ledger)
        self.assertIsNone(err2, err2)
        self.assertEqual(row2["title"], "do not file me")

    def test_the_CLI_narrates_a_REFUSED_sweep_never_a_clean_list(self):
        """A VALUE COMPUTED AND NEVER RENDERED IS ITS OWN BUG CLASS — demote
        set rep["unavailable"] and the text path never printed it, so a
        refused sweep ended "nothing to drop — 0 row(s) still yours and open"
        with rc 0: a confident clean answer about a ledger nobody could read.
        This arm drives the DEFAULT ledger path, corrupt for real."""
        lp = tasks.ledger_path()
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        with open(lp, "w", encoding="utf-8") as fh:
            fh.write("{this is not json at all\n")
        full = self.personal(33, "live work", **{todos.STAMP: "task/1"})
        with mock.patch.object(todos, "this_session", return_value=SID):
            rc, out, err = self.cli(["demote", "--owner", "cj"])
        self.assertEqual(rc, 1)
        self.assertIn("unreadable", out + err)
        self.assertNotIn("nothing to drop", out)
        self.assertNotIn("ledger moved on", out)
        self.assertTrue(os.path.exists(full))
        # THE JSON PATH CARRIES THE SAME TRUTH IN BOTH CHANNELS: the report
        # names the refusal AND rc is 1 — a wrapper checking either one must
        # not read a judged-nothing sweep as a found-nothing sweep.
        with mock.patch.object(todos, "this_session", return_value=SID):
            rcj, outj, _errj = self.cli(["demote", "--owner", "cj", "--json"])
        self.assertEqual(rcj, 1)
        repj = json.loads(outj)
        self.assertIn("corrupt ledger line", str(repj["unavailable"]))
        self.assertEqual(repj["orphaned"], [])
        # POSITIVE CONTROL ON THE SAME OBSERVABLES: with the bytes healed the
        # same command IS a clean sweep — rc 0 and the clean-list sentence.
        os.remove(lp)
        with mock.patch.object(todos, "this_session", return_value=SID):
            rc2, out2, _err2 = self.cli(["demote", "--owner", "cj"])
        self.assertEqual(rc2, 0)
        self.assertIn("nothing to drop", out2)

    def test_an_UNNAMEABLE_seat_refuses_BOTH_verbs_rather_than_degrading(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the SECOND half: the same row, same command, WITH --owner, reports "would drop". That proves the row is removable, which is what makes its survival under the refusal mean something
        """FOUND BY DOGFOODING THIS ON THE SEAT THAT WROTE IT. A pane can be
        live and working with HELM_CHAT_NAME absent from the shell it hands a
        subprocess — this session is one, and its own resume-turn notice says
        so. promote refused (right, but with no way forward). demote was
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

    def test_a_MISSING_owner_value_cannot_consume_DRY_RUN_and_delete(self):  # noqa: VACUOUS_ASSERTION — the same removable file and corrected argv positively produce "would drop" while leaving the file present
        """A valueless --owner used to eat --dry-run as its seat name. The
        dry flag then vanished from the remaining argv and demote applied the
        removal: malformed input turned a preview into a destructive command."""
        twin, err = tasks.add("already done", "codex")
        self.assertIsNone(err, err)
        _row, err = tasks.update(twin["id"], status="closed",
                                 closed_reason="done")
        self.assertIsNone(err, err)
        full = self.personal(25, "already done", **{todos.STAMP: twin["id"]})
        with mock.patch.object(todos, "this_session", return_value=SID):
            rc, out, err = self.cli(["demote", "--owner", "--dry-run"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)
        self.assertNotIn("dropped", out)
        self.assertTrue(os.path.exists(full))

        with mock.patch.object(todos, "this_session", return_value=SID):
            rc2, out2, err2 = self.cli(
                ["demote", "--owner", "codex", "--dry-run"])
        self.assertEqual(rc2, 0, err2)
        self.assertIn("would drop", out2)
        self.assertTrue(os.path.exists(full))

    def cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = todos.cmd_todos(args)
        return rc, out.getvalue(), err.getvalue()

    def test_an_UNPARSEABLE_stamp_is_KEPT_without_leaning_on_a_far_module(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the MUST-HIT assertIsNone(normalize_id(...)) proving the stamp really is unparseable, plus the sibling arms in this class that DO remove rows, so 'removed == []' here is a decision rather than a leg that never fires
        """The unreproducible finding, with the mechanism attached.
        normalize_id returns None for "not an id
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

        # THE MUTATION THEY RAN: a ledger that answers a falsy key must still
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
        still existed — which is precisely the defect measured: it made
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


class TrashCannotTellMeansKeepTest(TodosBase):
    """The identity guard's own CANNOT-TELL case, which it used to fail OPEN.

    `_identity` returns None for a row with no id (or an empty one), and the
    guard compared its two answers with a bare `!=`. In Python `None != None`
    is FALSE, so two UNIDENTIFIABLE rows compared EQUAL and the unlink went
    ahead — destroying a live row on the one leg built to prevent that. The
    docstring on `_identity` asserted the opposite, which is why the hole read
    as handled by anyone auditing the guard.

    Measured 2026-08-07, task/414. Latent rather than live: helm's own writer
    always stamps a non-empty id, so this needs a hand-edited or foreign-
    written personal file — but a guard that fails open in its own
    cannot-tell case is a re-opening of task/394 waiting for one."""

    def _trash_it(self, cached, on_disk):
        """Run the real `_trash` over a file holding `on_disk` while the sweep
        believes it holds `cached`. -> (error string or None, still_exists)."""
        d = os.path.join(self.tmp, "personal")
        os.makedirs(d, exist_ok=True)
        full = os.path.join(d, "1.json")
        with open(full, "w", encoding="utf-8") as fh:
            json.dump(on_disk, fh)
        err = todos._trash(SID, full, cached, {})
        return err, os.path.exists(full)

    def test_two_unidentifiable_rows_are_NOT_the_same_row(self):  # noqa: VACUOUS_ASSERTION — the absent observable is Control B's assertIsNone(err), and the unconditional positive control on that SAME observable is Control A three lines above it, which requires err to be present and to name the mismatch; Control B additionally asserts the file is GONE, so it cannot pass on a _trash that has stopped acting
        # CONTROL A — the guard still REFUSES a proven mismatch.
        err, alive = self._trash_it({"id": "OLD"}, {"id": "NEW"})
        self.assertTrue(err and "changed under the sweep" in err)
        self.assertTrue(alive, "control: a differing id must keep the file")

        # CONTROL B — an identical identity still PROCEEDS. Without this the
        # arm below passes on a `_trash` that has stopped deleting anything.
        err, alive = self._trash_it({"id": "SAME"}, {"id": "SAME"})
        self.assertIsNone(err, "control: matching identity must remove")
        self.assertFalse(alive, "control: matching identity must remove")

        # THE ARM. Neither row can be identified, and they are DIFFERENT rows.
        # Before the fix this deleted the file and returned success.
        err, alive = self._trash_it({"subject": "what the sweep judged"},
                                    {"subject": "A DIFFERENT LIVE ROW"})
        self.assertTrue(alive,
                        "an unprovable identity must KEEP the file — this is "
                        "the cannot-tell case the guard exists for")
        self.assertTrue(err and "cannot prove which row" in err,
                        "and it must say WHY, not report a silent success")

    def test_either_side_unprovable_is_enough_to_refuse(self):
        """Both directions, because they fail for opposite reasons: the FILE
        may be unreadable-as-a-row, or the DECISION may have been taken on
        one. Neither licenses a delete, and the message names which."""
        err, alive = self._trash_it({"id": "known"}, {"subject": "no id here"})
        self.assertTrue(alive)
        self.assertIn("the file on disk", err)

        err, alive = self._trash_it({"subject": "no id here"}, {"id": "known"})
        self.assertTrue(alive)
        self.assertIn("the row this decision was made about", err)


class TrashClaimsBeforeJudgingTest(TodosBase):
    """THE WINDOW THE FIRST CURE LEFT OPEN (T1, task/420).

    Round one re-read the file and compared identity BEFORE unlinking. That
    narrowed the race and did not close it: the re-read and the os.remove were
    still two operations, so a writer landing between them was destroyed
    exactly as before — the repro patched os.remove, replaced the pathname
    after the check, and got the original outcome verbatim (live row gone,
    success reported, OLD bytes in the undo). Reproduced independently.

    A second re-read cannot close it and neither can an inode stat, because
    both are still CHECK-THEN-ACT. The cure claims the bytes first: the
    pathname is moved into a quarantine by rename(), which is atomic, and only
    then judged. These arms hook the moment AFTER the claim — the only moment
    a writer can still reach the original path — and require that whatever
    lands there survives untouched."""

    def _claim_then_write(self, full, newcomer):
        """Patch the JUDGED READ so a writer installs `newcomer` at `full` in
        the instant after helm claims the bytes. Returns the fired-flag list so
        a test can prove the race actually armed rather than passing vacuously.

        (this prose was caught naming os.rename while the code patches
        `helm.pk.read_json`. The hook is on the read ON PURPOSE — see below —
        and the stale sentence is exactly the kind of comment that makes a
        reader trust the wrong seam.)"""
        # EIGHTH RE-POINT IN THIS LANE. The judged read has been
        # pk.read_json(path) and is now _json_fd(payload_fd) — the sweep reads
        # the claimed object through its own descriptor. The MOMENT is
        # unchanged (helm has claimed the bytes and is about to judge them);
        # only the call occupying it moved, for the eighth time. The docstring
        # above already records a stale sentence being caught naming the
        # wrong seam here once before.
        real = todos._json_fd
        fired = []

        def read_then_writer(fd):
            got = real(fd)
            if not fired:
                fired.append(1)
                with open(full, "w", encoding="utf-8") as fh:
                    json.dump(newcomer, fh)
            return got
        # HOOKED ON THE JUDGED READ, NOT ON rename(), SO THIS ARM
        # DISCRIMINATES BOTH IMPLEMENTATIONS. The previous cure read `full` and
        # then unlinked it, so a writer landing here was destroyed; this one has
        # already claimed the bytes and reads the QUARANTINE, so the same writer
        # lands on a path helm no longer owns. An arm hooked on rename() could
        # only ever fire against the new code, which would make it untestable
        # against the very defect it exists for.
        return mock.patch("helm.todos._json_fd", read_then_writer), fired

    def _row_file(self, row):
        d = os.path.join(self.tmp, "personal")
        os.makedirs(d, exist_ok=True)
        full = os.path.join(d, "1.json")
        with open(full, "w", encoding="utf-8") as fh:
            json.dump(row, fh)
        return full

    def test_a_writer_landing_after_the_claim_is_NOT_destroyed(self):
        """The judged row is removed and the newcomer is left alone. Under the
        previous cure this exact sequence deleted the newcomer."""
        judged = {"id": "7", todos.STAMP: "t1"}
        full = self._row_file(judged)
        newcomer = {"id": "7", "subject": "BRAND NEW LIVE WORK"}
        patch, fired = self._claim_then_write(full, newcomer)
        rep = {}
        with patch:
            err = todos._trash(SID, full, judged, rep)
        self.assertEqual(fired, [1], "the race never armed — arm is vacuous")
        self.assertIsNone(err, "the row helm actually judged must still go")
        # THE ACCEPTANCE BAR IS THE FILE, not the report: the row a writer put
        # there after helm took ownership must still be on disk, unread.
        self.assertTrue(os.path.exists(full), "the newcomer was destroyed")
        with open(full, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["subject"], "BRAND NEW LIVE WORK")
        # UNCONDITIONAL POSITIVE CONTROL ON THE REMOVAL ITSELF. Everything
        # above is also true of a `_trash` that read the file and did NOTHING:
        # the newcomer would sit there untouched and err would be None. So the
        # judged row must be PROVABLY in the undo, by its own bytes.
        self.assertTrue(rep.get("trash"), "no undo was recorded — this arm "
                                          "would pass on a no-op _trash")
        kept = _undo_of(rep, os.path.basename(full))
        self.assertTrue(os.path.exists(kept), "the judged row is not in the undo")
        with open(kept, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh).get(todos.STAMP), "t1",
                             "the undo holds the JUDGED bytes, not the "
                             "newcomer's and not a re-serialised stale read")

    def test_a_refusal_that_cannot_put_back_preserves_BOTH_and_says_so(self):  # noqa: VACUOUS_ASSERTION — the absent observable is rep['trash'], and this arm cannot pass on a no-op: it also requires err to NAME a path and that path to hold the claimed row's own bytes, which only a _trash that actually claimed them can produce. The unconditional positive control on rep['trash'] itself is the sibling arm above, which requires it set and the judged bytes inside it.
        """When the judgement REFUSES and a writer already occupies the path,
        restoring would destroy the newcomer to save the original. Both are
        kept and both are named — losing a row is what this leg exists to
        prevent, and that covers the newcomer too."""
        full = self._row_file({"id": "OLD", todos.STAMP: "t1"})
        patch, fired = self._claim_then_write(full, {"id": "NEWCOMER"})
        rep = {}
        with patch:
            # the sweep believes it judged a DIFFERENT row, so it must refuse
            err = todos._trash(SID, full, {"id": "MISMATCH"}, rep)
        self.assertEqual(fired, [1], "the race never armed — arm is vacuous")
        self.assertTrue(os.path.exists(full), "the newcomer was destroyed")
        with open(full, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["id"], "NEWCOMER")
        self.assertIn("preserved at", err or "")
        self.assertIn("rather than overwriting", err or "")
        self.assertIsNone(rep.get("trash"), "a refusal must not report an undo")
        # UNCONDITIONAL POSITIVE CONTROL: "preserved" must mean the bytes are
        # somewhere a human can reach, not a word in a sentence. The message
        # names the path; that path must hold the row helm claimed.
        where = (err or "").split("preserved at ")[-1].split(" rather")[0].strip()
        self.assertTrue(os.path.exists(where), "the claimed bytes are gone — "
                                               "the message names nothing real")
        with open(where, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["id"], "OLD")

    def test_an_uncontested_refusal_puts_the_row_straight_back(self):
        """CONTROL for the arm above: with NOBODY racing, the same refusal
        restores the file to its own path instead of stranding it in the undo
        directory. Without this, 'preserved at' could be the only outcome the
        refusal path has and the arm above would prove nothing."""
        full = self._row_file({"id": "OLD", todos.STAMP: "t1"})
        rep = {}
        err = todos._trash(SID, full, {"id": "MISMATCH"}, rep)
        self.assertTrue(os.path.exists(full))
        with open(full, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["id"], "OLD")
        self.assertIn("changed under the sweep", err or "")
        self.assertNotIn("preserved at", err or "")


class TrashClaimAndUndoMayBeDifferentFilesystemsTest(unittest.TestCase):
    """THE CLAIM AND THE UNDO ARE NOT ONE TREE (T1).

    Closing the unlink race meant claiming the bytes with rename(), and the
    first version took that claim by renaming INTO the undo directory — on my
    assumption that both live under one home. They do not: `personal_dir` is
    rooted at `claude_home()` (CLAUDE_CONFIG_DIR) and the undo at
    `record.state_root()` (HELM_HOME). Point them at different filesystems and
    rename() raises EXDEV, the row is kept, and EVERY eligible demotion is
    permanently stranded — a working sweep silently becomes a no-op.

    Same-filesystem was called "true today" WITHOUT MEASURING IT, which is
    the same error that closed the parent row on a read. Measured.
    The cure separates the two localities: the claim is taken into a
    private directory created BESIDE the row, so it is same-filesystem by
    construction, and the undo is reached afterwards by a COPY that may cross.
    """

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        # /dev/shm and /tmp are separate mounts on Linux; this is the review's own
        # probe shape. If the box ever has them on one device the arm would
        # silently stop discriminating, so the device numbers are ASSERTED.
        self.rows_root = tempfile.mkdtemp(prefix="helm-rows-", dir="/dev/shm")
        self.home_root = tempfile.mkdtemp(prefix="helm-home-", dir="/tmp")
        os.environ["CLAUDE_CONFIG_DIR"] = self.rows_root
        os.environ["HELM_HOME"] = self.home_root

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.rows_root, ignore_errors=True)
        shutil.rmtree(self.home_root, ignore_errors=True)

    def test_a_row_and_its_undo_on_DIFFERENT_filesystems_still_removes(self):  # noqa: VACUOUS_ASSERTION — the absent observable is assertIsNone(err), and this arm cannot pass on a no-op _trash: it also requires the row to be GONE from its own path, rep['trash'] to be set, the undo to hold the row's own id, and no claim directory to survive beside it. A _trash that returned None without acting fails the very next assertion. The fixture also asserts its OWN cross-filesystem premise first, so it fails loudly rather than silently ceasing to discriminate.
        # MUST-HIT CONTROL ON THE FIXTURE ITSELF: if these are the same device
        # the arm proves nothing, so it fails loudly rather than passing.
        self.assertNotEqual(os.stat(self.rows_root).st_dev,
                            os.stat(self.home_root).st_dev,
                            "fixture is not cross-filesystem — this arm cannot "
                            "discriminate and its result would be meaningless")
        d = os.path.join(self.rows_root, "personal")
        os.makedirs(d, exist_ok=True)
        full = os.path.join(d, "1.json")
        judged = {"id": "9", todos.STAMP: "t9"}
        with open(full, "w", encoding="utf-8") as fh:
            json.dump(judged, fh)
        rep = {}
        err = todos._trash("sess-xfs", full, judged, rep)
        self.assertIsNone(err, "a cross-filesystem undo must still remove — "
                               "EXDEV here strands every eligible row")
        self.assertFalse(os.path.exists(full), "the judged row was not removed")
        self.assertTrue(rep.get("trash"), "no undo was recorded")
        kept = _undo_of(rep, "1.json")
        self.assertTrue(os.path.exists(kept), "the undo does not hold the row")
        with open(kept, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["id"], "9")
        # THE CLAIMED OBJECT IS DELIBERATELY PRESERVED HERE, and this reverses
        # what this arm asserted under the previous design. Across filesystems
        # the undo can only be a COPY, so deleting the source would destroy any
        # bytes a writer with a pre-existing fd wrote after the snapshot —
        # exactly the loss measured. The row is removed from the live
        # list, the undo holds it, and the object is kept and REPORTED.
        claims = [n for n in os.listdir(d) if n.startswith(".claiming-")]
        self.assertEqual(len(claims), 1, "the object was deleted while a "
                                         "writer could still reach it")
        self.assertEqual(len(rep.get("preserved") or []), 1,
                         "the preservation was not reported: %r" % rep)
        self.assertTrue(os.path.exists(rep["preserved"][0]["object"]))


class ClaimSurvivesACrashTest(unittest.TestCase):
    """A CRASH MID-REMOVAL MUST NOT MAKE A ROW INVISIBLE (T1).

    A claim is a removal in flight: the row is no longer at its own pathname
    and not yet in the undo. Kill the process there and the only copy sits in
    a hidden `.claiming-` directory that `personal_rows` never descends into —
    bytes intact, row gone from every surface. Measured by raising
    SystemExit straight after the rename, and a git grep for the prefix found
    only the producer and a cleanup assertion: no consumer existed.

    The two crash windows want OPPOSITE acts, which is why best-effort cleanup
    was not the cure: BEFORE the undo copy the row must come BACK, and AFTER it
    the row is durably removed and restoring would RESURRECT it. Recovery reads
    what the claim said it was doing and checks the undo, instead of guessing
    from what is on disk."""

    SID = "sess-crash"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-claim-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, rid="5"):
        full = os.path.join(self.d, "%s.json" % rid)
        row = {"id": rid, todos.STAMP: "t5", "subject": "REAL WORK"}
        with open(full, "w", encoding="utf-8") as fh:
            json.dump(row, fh)
        return full, row

    def _claims(self):
        return [n for n in os.listdir(self.d)
                if n.startswith(todos.CLAIM_PREFIX)]

    def test_a_death_right_after_the_claim_leaves_the_row_RECOVERABLE(self):  # noqa: VACUOUS_ASSERTION — the absent observable is assertFalse(exists(full)) proving the crash ARMED; the unconditional positive control on that same path follows in the same arm — after recovery the file must EXIST and carry its original subject, and the claim directory must be gone
        """codex's exact probe, then the cure. Death after rename, before the
        row is even read."""
        full, row = self._row()
        # SEVENTH RE-POINT. The moment is "the claim is taken and the sweep is
        # about to read the claimed file to judge it". That read was
        # pk.read_json on a path; it is _json_fd on the payload's own
        # descriptor now, so the old hook fires never. The window has not
        # moved once — only the call that occupies it.
        with mock.patch.object(todos, "_json_fd",
                               side_effect=SystemExit("killed mid-claim")):
            with self.assertRaises(SystemExit):
                todos._trash(self.SID, full, row, {})
        # THE MEASURED DEFECT: gone from its path, alive only inside a claim.
        self.assertFalse(os.path.exists(full), "the crash did not arm the case")
        self.assertEqual(len(self._claims()), 1, "no claim was left behind")
        # THE CURE.
        out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["restored-residual"], out)
        self.assertTrue(os.path.exists(full), "the row was not restored")
        with open(full, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["subject"], "REAL WORK")
        self.assertEqual(_live_claims(self.d), [],
                         "the claim was left UNRESOLVED")

    def test_a_death_before_the_undo_lands_also_restores(self):  # noqa: VACUOUS_ASSERTION — same observable asserted in both directions in one arm: absent right after the crash, then PRESENT after recovery. A recover_claims that never acted would fail the second assertion
        """The second window: judged and about to be filed, but the undo never
        became durable, so the row must come back."""
        full, row = self._row()
        # THE SEAM IS THE RENAME THAT FILES THE OBJECT, not os.replace. Under
        # inode preservation the undo receives the object itself, so
        # os.replace runs only on the cross-filesystem branch — patching it
        # here would inject into a call that never happens, which is a test
        # that cannot fail. Only the FILING rename is failed; the CLAIM is
        # also a rename and killing that would test a different window.
        # FIFTH RE-POINT IN THIS LANE, and the count is the finding. The
        # publish has been os.remove, then a copy via os.replace, then a
        # rename, and is now os.link — and every arm bound to the syscall
        # rather than the moment stopped firing at each change, silently
        # except where an assert caught it. The WINDOW under test has never
        # moved: judged and about to be filed, undo not yet durable, so the
        # row must come back.
        real_link = os.link

        def die_filing_only(src, dst, *a, **k):
            if _publishes_to_trash(dst, **k):
                raise SystemExit("killed mid-file")
            return real_link(src, dst, *a, **k)

        with mock.patch.object(todos.os, "link", die_filing_only):
            with self.assertRaises(SystemExit):
                todos._trash(self.SID, full, row, {})
        self.assertFalse(os.path.exists(full))
        out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["restored-residual"], out)
        self.assertTrue(os.path.exists(full))

    def test_a_death_AFTER_the_undo_landed_does_NOT_resurrect_the_row(self):  # noqa: VACUOUS_ASSERTION — the absence (full must NOT come back) is paired with two unconditional positives in the same arm — the undo path must still hold the row, and the claim must be swept — and the fixture itself requires a real _trash to have returned None with rep['trash'] set
        """THE OPPOSITE ACT, and the reason cleanup alone could not be the
        cure. The removal completed durably and only the sweep of the claim was
        lost; putting the row back would undo a removal the operator already
        has."""
        full, row = self._row()
        rep = {}
        self.assertIsNone(todos._trash(self.SID, full, row, rep))
        kept = _undo_of(rep, os.path.basename(full))
        self.assertTrue(os.path.exists(kept), "fixture: the undo must hold it")
        # Rebuild the state a crash between the undo publish and the cleanup
        # leaves — which is THE CLAIM THAT PUBLISHED THIS UNDO, still standing.
        # This used to mint a FRESH claim dir, which modelled that state
        # faithfully only while the undo sat at a shared <trash>/<row> path.
        # Now the undo descends from its claim, so a new id names an undo that
        # was never written: recovery reads not-completed and RESTORES, the
        # exact opposite of what this arm exists to check. The claim id is
        # recovered from the reported undo rather than guessed.
        # THE CLAIM ALREADY SURVIVES, because the publish links rather than
        # moves and the payload stays as the retained backup. So this no
        # longer REBUILDS anything — it rewinds the one step a crash would
        # have lost: the terminal transition. Strip `state` from the manifest
        # and the claim reads exactly as it did between a durable undo and its
        # own bookkeeping.
        #
        # It has been rebuilt twice before, each time faithfully modelling a
        # shape the code had already left: first a FRESH claim dir (correct
        # while the undo sat at a shared path), then the publishing claim
        # recreated by hand (correct while the publish still moved the
        # payload out). The fixture keeps chasing the cure because it encodes
        # a MECHANISM; what it means to check has never moved.
        claim_id = os.path.basename(os.path.dirname(kept))
        self.assertTrue(claim_id.startswith(todos.CLAIM_PREFIX),
                        "fixture: the undo must sit under its own claim dir, "
                        "got %r" % (kept,))
        qdir = os.path.join(self.d, claim_id)
        payload = os.path.join(qdir, os.path.basename(full))
        self.assertTrue(os.path.exists(payload),
                        "fixture: the publish must have kept the payload")
        meta_path = os.path.join(qdir, todos.CLAIM_META)
        meta = _manifest(meta_path)
        self.assertEqual(meta.pop("state", None), todos.RESIDUAL,
                         "fixture: the real _trash must have marked it "
                         "terminal, or there is nothing to rewind")
        meta.pop("residual_why", None)
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        out = todos.recover_claims(self.SID)
        # COMPLETED, AND NOW RECORDED. Recovery finishes the bookkeeping the
        # crash lost; it does not put the row back.
        self.assertEqual([o["outcome"] for o in out], ["completed-residual"],
                         out)
        self.assertFalse(os.path.exists(full),
                         "a completed removal was RESURRECTED by recovery")
        self.assertTrue(os.path.exists(kept), "the undo lost the row")
        self.assertTrue(os.path.exists(payload), "the backup was disposed of")

    def test_a_writer_holding_the_path_leaves_BOTH_and_reports_it(self):  # noqa: VACUOUS_ASSERTION — this arm's assertions are almost all positive: the outcome must be exactly ['contested'], the writer's file must survive with ITS content, and the preserved path must exist and hold the ORIGINAL row. Nothing here passes on a no-op
        """Contested recovery: overwriting would destroy the newcomer to save
        the orphan, so both are kept and both are named."""
        full, row = self._row()
        # SEVENTH RE-POINT. The moment is "the claim is taken and the sweep is
        # about to read the claimed file to judge it". That read was
        # pk.read_json on a path; it is _json_fd on the payload's own
        # descriptor now, so the old hook fires never. The window has not
        # moved once — only the call that occupies it.
        with mock.patch.object(todos, "_json_fd",
                               side_effect=SystemExit("killed mid-claim")):
            with self.assertRaises(SystemExit):
                todos._trash(self.SID, full, row, {})
        with open(full, "w", encoding="utf-8") as fh:      # a writer returns
            json.dump({"id": "5", "subject": "SOMEONE ELSE'S NEW ROW"}, fh)
        out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["contested"], out)
        with open(full, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["subject"], "SOMEONE ELSE'S NEW ROW")
        self.assertTrue(os.path.exists(out[0]["preserved"]),
                        "the claimed bytes were dropped on the floor")
        with open(out[0]["preserved"], encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["subject"], "REAL WORK")

    def test_a_LIVE_claim_is_never_recovered_out_from_under_its_sweep(self):  # noqa: VACUOUS_ASSERTION — the empty-list assertion IS the claim, and its unconditional positive control is in the same arm three lines down: the moment the holder releases the lock, the SAME claim must recover and restore the row. A recover_claims that never acts fails that
        """CONTROL ON THE RECOVERY PREDICATE ITSELF. Every arm above proves
        recovery ACTS; this one proves it declines, or 'recover everything
        always' would pass all of them while stealing rows from a running
        sweep."""
        full, row = self._row()
        qdir = _mint_claim(self.d, self.SID)
        meta = os.path.join(qdir, todos.CLAIM_META)
        with open(meta, "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir), "sid": self.SID,
                       "row": os.path.basename(full), "payload": os.path.basename(full),
                       "identity": [row["id"], row[todos.STAMP]]}, fh)
        with open(os.path.join(qdir, os.path.basename(full)), "w") as fh:
            json.dump(row, fh)
        # HOLD THE CLAIM THE WAY A LIVE SWEEP DOES — an advisory lock the
        # kernel owns. Nothing about pids or ages is asserted, because neither
        # is the mechanism any more.
        holder = open(meta, "a")
        try:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(todos.recover_claims(self.SID), [],
                             "a live claim was recovered out from under the "
                             "sweep that still holds it")
            self.assertEqual(len(self._claims()), 1, "the live claim was swept")
        finally:
            holder.close()
        # AND THE MOMENT THE HOLDER LETS GO — which is what a crash does — the
        # SAME claim is acted on. Without this the arm above would also pass on
        # a recover_claims that never acts at all, which is the failure mode a
        # liveness control is most likely to hide.
        #
        # The row is cleared first so the RESTORE path is the one exercised:
        # left in place, recovery would correctly report `contested` and the
        # control would be measuring the wrong branch. (It did, on the first
        # run of this arm — the code was right and my expectation was wrong.)
        os.remove(full)
        self.assertEqual([o["outcome"] for o in todos.recover_claims(self.SID)],
                         ["restored-residual"])
        self.assertTrue(os.path.exists(full))
        self.assertEqual(_live_claims(self.d), [])


class ClaimRecoveryBindsToCONTENTTest(unittest.TestCase):
    """RECOVERY MUST NOT CALL A CLAIM COMPLETE ON A STALE UNDO (D1/D3).

    The undo path is REUSABLE across transactions, so "the undo holds a row
    with this identity" can never mean "the undo holds THIS row". Their
    adversarial state: a stale undo carrying identity A, a claimed payload B,
    and metadata naming A. The identity-only version called that COMPLETED and
    swept B — data loss through the recovery written to prevent data loss.

    Completion is now bound to the payload's own DIGEST, recorded while the
    claim was owned and unable to change."""

    SID = "sess-content"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-content-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_stale_undo_with_the_same_identity_does_NOT_sweep_the_payload(self):
        full = os.path.join(self.d, "5.json")
        stale = {"id": "5", todos.STAMP: "t5", "subject": "AN OLD TRANSACTION"}
        live = {"id": "5", todos.STAMP: "t5", "subject": "THE ROW HELM CLAIMED"}
        # THE CLAIM COMES FIRST: the undo descends from it now.
        qdir = _mint_claim(self.d, self.SID)
        undo = _derived_undo(self.SID, qdir, "5.json")
        with open(undo, "w") as fh:                 # the STALE undo, identity A
            json.dump(stale, fh)
        payload = os.path.join(qdir, "5.json")
        with open(payload, "w") as fh:              # the CLAIMED payload, B
            json.dump(live, fh)
        with open(os.path.join(qdir, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir), "sid": self.SID,
                       "row": os.path.basename(full), "payload": os.path.basename(full),
                       "identity": ["5", "t5"],     # names A — the trap
                       "payload_sha256": _digest_of(payload)}, fh)
        out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["restored-residual"], out)
        # THE ACCEPTANCE BAR IS THE BYTES: the claimed row must be back, and it
        # must be the LIVE one, not the stale undo's twin.
        self.assertTrue(os.path.exists(full), "the claimed row was swept")
        with open(full) as fh:
            self.assertEqual(json.load(fh)["subject"], "THE ROW HELM CLAIMED")
        # CONTROL: with the undo actually holding THESE bytes, the same shape
        # DOES read completed — so the arm above is a discrimination, not a
        # recovery that never sweeps anything.
        os.remove(full)
        qdir2 = _mint_claim(self.d, self.SID)
        p2 = os.path.join(qdir2, "5.json")
        with open(p2, "w") as fh:
            json.dump(live, fh)
        # THIS claim's undo, not the previous one's. They were the same file
        # while the undo was a shared <trash>/<row> path; they are different
        # directories now, and writing the control's bytes into the OTHER
        # claim's undo made the control read `restored` — it was measuring the
        # first claim again.
        with open(_derived_undo(self.SID, qdir2, "5.json"), "w") as fh:
            json.dump(live, fh)
        with open(os.path.join(qdir2, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir2), "sid": self.SID,
                       "row": os.path.basename(full), "payload": os.path.basename(full),
                       "identity": ["5", "t5"],
                       "payload_sha256": _digest_of(p2)}, fh)
        # TWO claims are on disk by now: the residual this test's first phase
        # left behind when it restored, and the control claim just built. Both
        # are reported, which is the point — a residual that stopped being
        # reported would be exactly the hidden state seam 3 exists to prevent.
        self.assertEqual(sorted(o["outcome"] for o in
                                todos.recover_claims(self.SID)),
                         ["completed-residual", "residual"])
        self.assertFalse(os.path.exists(full))

    def test_asking_whether_a_claim_is_live_does_NOT_create_its_metadata(self):
        """The D5. The probe opened claim.json in APPEND mode, so merely
        ASKING minted an empty metadata file and made a metadata-less claim
        permanently 'unreadable' — the instrument manufactured the condition it
        reported."""
        qdir = _mint_claim(self.d, self.SID)
        with open(os.path.join(qdir, "9.json"), "w") as fh:
            json.dump({"id": "9"}, fh)
        meta = os.path.join(qdir, todos.CLAIM_META)
        self.assertFalse(os.path.exists(meta), "fixture: metadata must be absent")
        out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["unreadable"], out)
        self.assertFalse(os.path.exists(meta),
                         "the liveness probe CREATED the metadata it was "
                         "testing for")
        self.assertTrue(os.path.exists(os.path.join(qdir, "9.json")),
                        "an undecidable claim's payload was destroyed")

    def test_a_DRY_RUN_recovery_decides_without_touching_the_disk(self):
        """The D6. Recovery ran unconditionally inside demote, so
        `--dry-run` MUTATED — the worst defect in that commit, shipped inside a
        feature about not losing rows."""
        full = os.path.join(self.d, "7.json")
        row = {"id": "7", todos.STAMP: "t7"}
        qdir = _mint_claim(self.d, self.SID)
        payload = os.path.join(qdir, "7.json")
        with open(payload, "w") as fh:
            json.dump(row, fh)
        with open(os.path.join(qdir, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir), "sid": self.SID,
                       "row": os.path.basename(full), "payload": os.path.basename(full),
                       "identity": ["7", "t7"],
                       "payload_sha256": _digest_of(payload)}, fh)
        out = todos.recover_claims(self.SID, apply=False)
        self.assertEqual([o["outcome"] for o in out], ["restorable"], out)
        self.assertFalse(os.path.exists(full), "a DRY RUN restored the row")
        self.assertTrue(os.path.exists(payload), "a DRY RUN moved the payload")
        self.assertTrue(os.path.isdir(qdir), "a DRY RUN swept the claim")
        # UNCONDITIONAL POSITIVE CONTROL: the SAME claim, applied, DOES act —
        # so the assertions above are measuring restraint, not a no-op.
        applied = todos.recover_claims(self.SID, apply=True)
        # `restored-residual`, not `restored`: the restore hard-links the
        # payload to the row and the claim's copy is then a SECOND NAME FOR THE
        # SAME INODE, which cleanup preserves rather than unlinking (seam 3).
        # The claim therefore survives BY DESIGN and the outcome must say so —
        # asserting plain `restored` here would be asserting that the
        # preservation did not happen.
        self.assertEqual([o["outcome"] for o in applied], ["restored-residual"])
        self.assertTrue(os.path.exists(full))
        self.assertTrue(os.path.isdir(qdir), "the residual was disposed of")
        self.assertEqual(applied[0]["preserved"], payload)
        self.assertEqual(os.stat(full).st_ino, os.stat(payload).st_ino,
                         "the residual is a COPY, not a second name — that "
                         "would be duplicated bytes rather than a dirent")


class RecoveredOutcomesREACHTheOperatorTest(unittest.TestCase):
    """A RECOVERY THE OPERATOR CANNOT SEE IS THE DEFECT, ONE LAYER UP.

    The normal CLI omitted recovered contested/preserved
    paths. A report that says bytes were "preserved" without saying WHERE
    leaves the operator with exactly the silently-absent row this whole leg
    exists to prevent — the sweep would have recovered honestly and told
    nobody where to look."""

    SID = "sess-cli"

    def setUp(self):
        self.keys = ("HELM_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_ID",
                     "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "HELM_CHAT_NAME")
        self.prior = {k: os.environ.get(k) for k in self.keys}
        for k in self.keys:
            os.environ.pop(k, None)
        self.tmp = tempfile.mkdtemp(prefix="helm-cli-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SID
        os.environ["HELM_CHAT_NAME"] = "test-seat"
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_CONTESTED_recovery_prints_the_path_its_bytes_are_at(self):
        full = os.path.join(self.d, "3.json")
        with open(full, "w") as fh:                       # the writer's row
            json.dump({"id": "3", "subject": "SOMEONE ELSE'S NEW ROW"}, fh)
        qdir = _mint_claim(self.d, self.SID)
        payload = os.path.join(qdir, "3.json")
        with open(payload, "w") as fh:                    # the claimed row
            json.dump({"id": "3", todos.STAMP: "t3"}, fh)
        with open(os.path.join(qdir, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir), "sid": self.SID,
                       "row": os.path.basename(full), "payload": os.path.basename(full),
                       "identity": ["3", "t3"],
                       "payload_sha256": _digest_of(payload)}, fh)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            todos._cmd_bridge(["demote"])
        text = out.getvalue()
        self.assertIn("KEPT BOTH", text)
        self.assertIn(payload, text,
                      "the operator is told bytes were preserved and NOT told "
                      "where they are")
        self.assertIn(full, text)
        # UNCONDITIONAL POSITIVE CONTROL ON THE RENDERER ITSELF: a RESTORED
        # recovery must also reach the operator, so the assertions above are
        # measuring what is printed rather than a renderer that prints nothing.
        os.remove(full)
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            todos._cmd_bridge(["demote"])
        self.assertIn("RECOVERED", out2.getvalue())
        self.assertTrue(os.path.exists(full), "the row did not come back")


class RestoreNamesWhatCameBackTest(unittest.TestCase):
    """The successor blocker. The metadata's identity is the row the sweep
    DECIDED about, read before the claim. The payload is what was actually at
    that pathname when the claim took it — and an already-open writer can
    mutate that inode in between. Restoring the bytes is right either way,
    because they belong at that path; reporting them as the recorded row when
    they are NOT is the report lying about the disk."""

    SID = "sess-named"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-named-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _claim(self, payload_row, recorded_identity):
        full = os.path.join(self.d, "4.json")
        qdir = _mint_claim(self.d, self.SID)
        payload = os.path.join(qdir, "4.json")
        with open(payload, "w") as fh:
            json.dump(payload_row, fh)
        with open(os.path.join(qdir, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir), "sid": self.SID,
                       "row": os.path.basename(full), "payload": os.path.basename(full),
                       "identity": list(recorded_identity),
                       "payload_sha256": _digest_of(payload)}, fh)
        return full

    def test_bytes_that_are_NOT_the_recorded_row_come_back_and_are_NAMED(self):
        # the writer mutated the inode after the claim: payload is row 4/tX,
        # the sweep had decided about 4/t4.
        full = self._claim({"id": "4", todos.STAMP: "tX"}, ("4", "t4"))
        out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["restored-unexpected-residual"],
                         out)
        self.assertEqual(out[0]["recorded"], ["4", "t4"])
        self.assertEqual(out[0]["found"], ["4", "tX"])
        # THE BYTES STILL COME BACK — they belong at that path regardless.
        self.assertTrue(os.path.exists(full))
        with open(full) as fh:
            self.assertEqual(json.load(fh)[todos.STAMP], "tX")

    def test_a_MATCHING_payload_is_NOT_reported_as_unexpected(self):
        """Renamed: it said "plainly restored" and now asserts
        `restored-residual`, because cleanup preserves the claim by
        design. What it actually discriminates is expected vs
        UNEXPECTED identity, which is orthogonal to the residual — and a
        test name that describes the wrong axis is a surface that lies
        exactly the way a commit message does."""
        """UNCONDITIONAL POSITIVE CONTROL: without this, `restored-unexpected`
        could be what every recovery reports and the arm above would prove
        nothing about discrimination."""
        full = self._claim({"id": "4", todos.STAMP: "t4"}, ("4", "t4"))
        out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["restored-residual"], out)
        self.assertNotIn("recorded", out[0])
        self.assertTrue(os.path.exists(full))


class RenamePinsAPathNotAnInodeTest(unittest.TestCase):
    """Measured. An earlier comment in `_trash` claimed the
    object was "owned and unable to change" once renamed. FALSE for a writer
    holding a descriptor it opened BEFORE the rename: their probe let _trash
    rename, read, hash and identity-check, then wrote through the old fd at the
    mkstemp seam. _trash returned SUCCESS, the live path was gone, the undo
    held the bytes read at claim time, and the writer's newer bytes were
    deleted with the claim directory.

    Nothing here can exclude an open-fd writer, so the claim was never
    immunity. What it can do is re-hash immediately before destroying the
    object and keep BOTH when they disagree."""

    SID = "sess-openfd"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-openfd-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bytes_written_through_an_old_fd_are_KEPT_not_deleted(self):  # noqa: VACUOUS_ASSERTION — the absent observable is assertIsNone(err) and the arm cannot pass on a no-op: it requires the hook to have FIRED, the undo file to EXIST, and its content to be the writer's NEWER bytes. A _trash that removed nothing leaves no undo and fails the next line; the unconditional positive control on err itself is the unraced arm below, which requires a completed removal
        full = os.path.join(self.d, "2.json")
        judged = {"id": "2", todos.STAMP: "t2", "subject": "AS HELM READ IT"}
        with open(full, "w") as fh:
            json.dump(judged, fh)
        writer = open(full, "r+")            # opened BEFORE the claim
        # THE SEAM IS AFTER THE OBJECT HAS MOVED INTO THE UNDO. Hooking the
        # old copy seam would test a branch the same-filesystem path no longer
        # takes — the undo receives the INODE now, so a late write follows it.
        real_sweep = todos._sweep_claim
        fired = []

        # *args/**kwargs, not a pinned signature. This wrapper has now been
        # broken twice by _sweep_claim gaining a parameter, each time as a
        # TypeError in a test about something else entirely. A hook that
        # forwards whatever it is given cannot be invalidated by the callee's
        # signature — only by its BEHAVIOUR, which is what the arm is about.
        def mutate_then_sweep(qdir, *a, **k):
            if not fired:
                fired.append(1)
                writer.seek(0)
                writer.write(json.dumps({"id": "2", todos.STAMP: "t2",
                                         "subject": "THE WRITER'S NEWER BYTES"}))
                writer.truncate()
                writer.flush()
            return real_sweep(qdir, *a, **k)

        rep = {}
        try:
            with mock.patch.object(todos, "_sweep_claim", mutate_then_sweep):
                err = todos._trash(self.SID, full, judged, rep)
        finally:
            writer.close()
        self.assertEqual(fired, [1], "the race never armed — arm is vacuous")
        self.assertIsNone(err)
        # THE ACCEPTANCE BAR IS THE WRITER'S BYTES SURVIVING SOMEWHERE, and
        # under inode preservation they survive IN THE UNDO: the late write
        # went to the object helm moved, not to a file helm then deleted.
        # Snapshot-then-delete lost them here; that is the whole shape change.
        kept = _undo_of(rep, "2.json")
        self.assertTrue(os.path.exists(kept), "the undo does not hold the row")
        with open(kept) as fh:
            self.assertEqual(json.load(fh)["subject"],
                             "THE WRITER'S NEWER BYTES",
                             "the writer's bytes were lost to a snapshot")

    def test_an_UNRACED_removal_still_completes(self):  # noqa: VACUOUS_ASSERTION — THIS ARM IS ITSELF THE POSITIVE CONTROL for the open-fd arm above it — its whole job is to show the same call completes when nobody races, so requiring it to carry its own control inverts what it exists for. Its assertions are also not all absences: the claim directory list must be EMPTY and the row must be GONE, which a _trash that stopped acting cannot produce
        """UNCONDITIONAL POSITIVE CONTROL: without a writer, the same call
        removes and sweeps. Otherwise the arm above would pass on a `_trash`
        that had stopped completing anything at all."""
        full = os.path.join(self.d, "8.json")
        row = {"id": "8", todos.STAMP: "t8"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        rep = {}
        self.assertIsNone(todos._trash(self.SID, full, row, rep))
        self.assertFalse(os.path.exists(full))
        # No claim is left UNRESOLVED. A terminal residual DOES remain, by
        # design: the publish links rather than moves, so the payload stays as
        # the retained backup and the claim records that it did. Asserting an
        # empty directory here would be asserting the preservation did not
        # happen.
        self.assertEqual(_live_claims(self.d), [])
        self.assertEqual(len(rep["residuals"]), 1, rep)
        self.assertTrue(os.path.exists(rep["residuals"][0]["payload"]))

    def test_a_claim_that_cannot_be_made_durable_puts_the_row_BACK(self):  # noqa: VACUOUS_ASSERTION — the absent observable is rep['trash'], and the arm cannot pass on a no-op: it requires err to be PRESENT and to name 'could not be made durable', and the row to be PRESENT at its own path. The unconditional positive control on rep['trash'] itself is the unraced arm above, which requires a completed removal
        """The post-rename directory fsync failure was
        SWALLOWED while the comments above it promised the claim survives a
        crash. Prose that outruns the code is the prose lying."""
        full = os.path.join(self.d, "6.json")
        row = {"id": "6", todos.STAMP: "t6"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        real_fsync = todos._fsync_dir

        # ELEVENTH RE-POINT, same window. The row directory's sync is
        # os.fsync(pdir_fd) now, so hooking _fsync_dir alone reaches only the
        # pre-anchor mkdir. A descriptor has no name, so the target is its
        # INODE — which is the identity the code itself switched to.
        _st = os.stat(self.d)
        want_id = (_st.st_dev, _st.st_ino)   # device AND inode, as above
        real_os_fsync = os.fsync

        def fail_on_personal(d):
            if os.path.realpath(d) == os.path.realpath(self.d):
                raise OSError("no fsync on this mount")
            return real_fsync(d)

        def fail_on_personal_fd(fd, *a, **k):
            try:
                _f = os.fstat(fd)
                same = (_f.st_dev, _f.st_ino) == want_id
            except OSError:
                same = False
            if same:
                raise OSError("no fsync on this mount")
            return real_os_fsync(fd, *a, **k)

        rep = {}
        with mock.patch.object(todos, "_fsync_dir", fail_on_personal):
            with mock.patch.object(todos.os, "fsync", fail_on_personal_fd):
                err = todos._trash(self.SID, full, row, rep)
        self.assertTrue(err and "could not be made durable" in err, err)
        self.assertTrue(os.path.exists(full), "the row was removed anyway")
        self.assertIsNone(rep.get("trash"))


class TheREADPathNamesHeldRowsTest(unittest.TestCase):
    """The D6. Recovery lived only in `demote`, so a session that never
    swept again left the row held and reported NOWHERE A READER LOOKS. A
    crashed claim is invisible to the list by construction — the row is not at
    its pathname — which is precisely why the read surface has to say so."""

    SID = "sess-read"

    def setUp(self):
        self.keys = ("HELM_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_ID",
                     "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")
        self.prior = {k: os.environ.get(k) for k in self.keys}
        for k in self.keys:
            os.environ.pop(k, None)
        self.tmp = tempfile.mkdtemp(prefix="helm-read-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SID
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_held_row_is_NAMED_by_the_read_verb_and_nothing_is_touched(self):
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1"}
        qdir = _mint_claim(self.d, self.SID)
        payload = os.path.join(qdir, "1.json")
        with open(payload, "w") as fh:
            json.dump(row, fh)
        with open(os.path.join(qdir, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir), "sid": self.SID,
                       "row": os.path.basename(full), "payload": os.path.basename(full),
                       "identity": ["1", "t1"],
                       "payload_sha256": _digest_of(payload)}, fh)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            todos.cmd_todos([])
        text = out.getvalue()
        self.assertIn("HELD BY A CRASHED SWEEP", text)
        self.assertIn(payload, text, "the reader is told a row is held and NOT "
                                     "told where its bytes are")
        self.assertIn("demote", text, "no way out is offered")
        # THE READ VERB MUTATES NOTHING — a read that repaired state would be
        # the mutating dry run again, one surface over.
        self.assertTrue(os.path.isdir(qdir), "the READ path swept a claim")
        self.assertTrue(os.path.exists(payload), "the READ path moved bytes")
        self.assertFalse(os.path.exists(full), "the READ path restored a row")
        # UNCONDITIONAL POSITIVE CONTROL: with no claim held, the same verb
        # says nothing of the sort — so the assertions above read a line that
        # fires selectively rather than one always printed.
        shutil.rmtree(qdir)
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            todos.cmd_todos([])
        self.assertNotIn("HELD BY A CRASHED SWEEP", out2.getvalue())


class LockUntestableREFUSESAndReportsTest(unittest.TestCase):
    """CANNOT-TEST-THE-LOCK IS NOT THE SAME FACT AS SOMEONE-HOLDS-IT.

    Liveness is answered by an advisory lock, which is right — a pid can be
    reused and an age is a guess. But a filesystem WITHOUT flock cannot answer
    the question at all, and treating that as "held" strands every claim on
    that mount forever. Cannot-tell must not silently become never.

    This backstop was documented in prose and then LOST when the pid check was
    replaced by the lock: the constant survived, its only reader did not, and
    NOTHING FAILED — a guarantee whose form outlived its substance. Found by
    asking which names the refactor had orphaned, not by a test."""

    SID = "sess-nolock"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-nolock-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _orphan(self, age_s):
        full = os.path.join(self.d, "1.json")
        qdir = _mint_claim(self.d, self.SID)
        payload = os.path.join(qdir, "1.json")
        with open(payload, "w") as fh:
            json.dump({"id": "1", todos.STAMP: "t1"}, fh)
        with open(os.path.join(qdir, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir), "sid": self.SID,
                       "row": os.path.basename(full), "payload": os.path.basename(full),
                       "identity": ["1", "t1"],
                       "payload_sha256": _digest_of(payload)}, fh)
        old = time.time() - age_s
        os.utime(qdir, (old, old))
        return full

    def test_an_OLD_claim_is_REPORTED_when_the_lock_cannot_be_tested(self):
        """THIS ARM'S CONTRACT CHANGED, deliberately.

        It used to require that an old claim RECOVERS when flock is
        unavailable, on the reasoning that a live claim exists only for the
        microseconds of one _trash call, so an hour of quiet means abandoned.
        The reasoning is sound about the PAST and grants nothing about the
        next instruction: two recoveries meeting the same stale claim both
        pass the age test, both take a handle that holds NO LOCK, and both
        act. Age is evidence; it is not exclusion.

        The fear behind the old behaviour was real — a row stranded forever on
        a lock-less mount — so the cure is not silence but a REPORT. The claim
        is named, its state is unresolved, and an operator can act. Restoring
        automatic recovery there needs an exclusion primitive that works
        without flock; that is a separate piece of work, not something age can
        stand in for."""
        full = self._orphan(3600 + 60)
        with mock.patch.object(fcntl, "flock",
                               side_effect=OSError("no locks on this mount")):
            out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["unreadable"], out)
        self.assertTrue(all("could not be locked" in o.get("why", "")
                            for o in out), out)
        # THE ROW IS NOT LOST — it is held by the claim, and the claim is
        # named. Stranded-and-reported is recoverable; stranded-and-silent is
        # the state this leg exists to prevent.
        self.assertFalse(os.path.exists(full),
                         "the row is back at its path, which means recovery "
                         "ACTED without holding a lock")
        claims = [n for n in os.listdir(self.d)
                  if n.startswith(todos.CLAIM_PREFIX)]
        self.assertEqual(len(claims), 1,
                         "the claim holding the bytes is gone: %r" % (claims,))

    def test_a_FRESH_claim_is_still_left_alone_when_the_lock_is_untestable(self):  # noqa: VACUOUS_ASSERTION — the empty-list assertion IS the claim (a fresh claim must NOT be recovered), and it is paired with an unconditional assertFalse that the row is still absent, so a recover_claims that silently restored would fail the next line. The unconditional positive control on the same observable is the sibling arm above, which requires an OLD claim under the SAME untestable-lock condition to return ['restored']
        """UNCONDITIONAL POSITIVE CONTROL ON THE BACKSTOP'S BOUND. Without it,
        'recover whenever the lock fails' would pass the arm above while
        stealing rows from a live sweep on the very mounts that cannot lock."""
        full = self._orphan(5)
        with mock.patch.object(fcntl, "flock",
                               side_effect=OSError("no locks on this mount")):
            out = todos.recover_claims(self.SID)
        # THE PROPERTY IS UNCHANGED — no row is taken from a possibly-live
        # sweep on a mount that cannot lock — but the REPORTING changed with
        # the age-is-not-exclusion ruling: unlockable is now said out loud
        # rather than skipped in silence, because a claim nobody can decide
        # and nobody mentions is the silently-absent row this leg exists to
        # prevent. Silence used to be indistinguishable from "nothing was
        # there".
        self.assertEqual([o["outcome"] for o in out], ["unreadable"], out)
        self.assertTrue(all("could not be locked" in o.get("why", "")
                            for o in out), out)
        self.assertFalse(os.path.exists(full),
                         "the row was recovered on the strength of an "
                         "untestable lock")

class TheCompletedClaimKEEPSItsPayloadTest(unittest.TestCase):
    """The ruling, meld e:1786171017 — REPLACES the four arms that tested
    `_retire_payload`, which is DELETED.

    That helper existed only because the undo used to be a MOVE, leaving the
    payload as a leftover duplicate wanting tidy-up; review made the tidy-up
    non-destructive rather than removing the need for it. Under
    link-and-preserve the payload is not a leftover: same-filesystem it is one
    of two owned names for the very inode the undo IS, and cross-filesystem it
    is the ONLY inode a descriptor opened before the claim can still write to.
    Neither is disposable, so the completed branch records the terminal state
    and stops.

    Deleting the helper without these three would have left the property
    untested rather than unnecessary."""

    SID = "sess-keep"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-keep-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)
        self.full = os.path.join(self.d, "1.json")
        self.row = {"id": "1", todos.STAMP: "t1", "subject": "THE ROW"}

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _trash_once(self, link=None):
        with open(self.full, "w") as fh:
            json.dump(self.row, fh)
        rep = {"removed": [], "failed": []}
        if link is None:
            err = todos._trash(self.SID, self.full, self.row, rep)
        else:
            with mock.patch.object(todos.os, "link", link):
                err = todos._trash(self.SID, self.full, self.row, rep)
        self.assertIsNone(err, "fixture: the removal must succeed, got %r" % err)
        return rep

    def test_same_FS_completed_keeps_the_payload_as_one_inode_with_the_undo(self):
        rep = self._trash_once()
        r = rep["residuals"][0]
        self.assertTrue(r["same_inode"], r)
        payload, undo = r["payload"], r["undo"]
        self.assertTrue(os.path.exists(payload), "the payload was not kept")
        self.assertTrue(os.path.exists(undo), "the undo is missing")
        a, b = os.stat(payload), os.stat(undo)
        self.assertEqual(a.st_ino, b.st_ino,
                         "same-FS publish did not produce ONE inode with two "
                         "names — it copied or moved instead of linking")
        self.assertEqual(a.st_nlink, 2, "expected exactly two names")

    def test_cross_FS_completed_keeps_a_DISTINCT_payload_a_late_write_reaches(self):
        """The cross-FS undo is a fresh O_EXCL snapshot, so payload and undo
        are DIFFERENT inodes — and the payload is the only one a descriptor
        opened before the claim can still write to. That is the whole reason
        it is not disposable, so the arm writes through such a descriptor."""
        with open(self.full, "w") as fh:
            json.dump(self.row, fh)
        writer = open(self.full, "r+")          # opened BEFORE the claim
        real = todos.os.link

        def exdev_to_the_trash(src, dst, *a, **k):
            if _publishes_to_trash(dst, **k):
                raise OSError(errno.EXDEV, "simulated cross-filesystem undo")
            return real(src, dst, *a, **k)

        try:
            rep = {"removed": [], "failed": []}
            with mock.patch.object(todos.os, "link", exdev_to_the_trash):
                self.assertIsNone(todos._trash(self.SID, self.full,
                                               self.row, rep))
            r = rep["residuals"][0]
            self.assertFalse(r["same_inode"], r)
            payload, undo = r["payload"], r["undo"]
            self.assertNotEqual(os.stat(payload).st_ino, os.stat(undo).st_ino)
            writer.seek(0)
            writer.write(json.dumps({"id": "1", todos.STAMP: "t1",
                                     "subject": "LATE BYTES"}))
            writer.truncate()
            writer.flush()
        finally:
            writer.close()
        with open(payload) as fh:
            self.assertEqual(json.load(fh)["subject"], "LATE BYTES",
                             "a write through a pre-claim descriptor did not "
                             "reach the preserved payload")

    def test_a_second_recovery_is_idempotent_and_attempts_no_publish_or_delete(self):
        rep = self._trash_once()
        payload = rep["residuals"][0]["payload"]
        before = os.stat(payload)
        touched = []
        real_link, real_unlink = todos.os.link, todos.os.unlink

        def note_link(*a, **k):
            touched.append(("link",) + a)
            return real_link(*a, **k)

        def note_unlink(*a, **k):
            touched.append(("unlink",) + a)
            return real_unlink(*a, **k)

        with mock.patch.object(todos.os, "link", note_link):
            with mock.patch.object(todos.os, "unlink", note_unlink):
                # UNCONDITIONAL POSITIVE CONTROL, ON THE SAME OBSERVABLE AND
                # THROUGH THE SAME HOOKS: a real removal DOES link, so an
                # empty `touched` below means "recovery attempted nothing"
                # rather than "the hooks were never installed" — which is
                # exactly what this arm would otherwise be unable to tell
                # apart, since its whole claim is that nothing happened.
                self._trash_once()
                self.assertTrue(touched, "the hooks recorded nothing even for "
                                         "a real removal — they are not "
                                         "installed and this arm is vacuous")
                del touched[:]
                out = todos.recover_claims(self.SID)
        outcomes = [o["outcome"] for o in out]
        self.assertEqual(sorted(set(outcomes)), [todos.RESIDUAL], out)
        self.assertEqual(touched, [],
                         "a terminal residual was re-published or deleted: %r"
                         % (touched,))
        after = os.stat(payload)
        self.assertEqual((before.st_ino, before.st_size),
                         (after.st_ino, after.st_size))


class CleanupNeverDropsTheLastNameOfAnInodeTest(unittest.TestCase):
    """The census, seam 3 — THE CLEANUP COULD DELETE THE THING IT WAS
    CLEANING UP AROUND.

    Three callers publish a hard link and then call `_sweep_claim` to drop the
    source. `_sweep_claim` unlinked every name it found, so if the destination
    was replaced in that interval the unlink took the inode's LAST name: the
    publish silently did not happen and the bytes were gone. No check-then-act
    closes it — POSIX has no "unlink this name only if it still refers to this
    inode" — so the fix is structural. The bytes stay where they are and the
    CLAIM changes state.

    My own proposed cure was to MOVE the payload instead, which a review
    refused: a move is a link to a new name plus a source deletion, so it is
    the same handoff one step later."""

    SID = "sess-residual"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-residual-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _claim(self, with_payload=True):
        qdir = _mint_claim(self.d, self.SID)
        row = {"id": "1", todos.STAMP: "t1", "subject": "THE ONLY COPY"}
        if with_payload:
            with open(os.path.join(qdir, "1.json"), "w") as fh:
                json.dump(row, fh)
        with open(os.path.join(qdir, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir), "sid": self.SID,
                       "row": "1.json", "payload": "1.json",
                       "identity": ["1", "t1"],
                       "payload_sha256": _digest_of(
                           os.path.join(qdir, "1.json")) or ""}, fh)
        return qdir

    def test_a_claim_that_still_holds_bytes_is_NOT_deleted(self):  # noqa: VACUOUS_ASSERTION — nothing here is an absence assertion: the sweep must return exactly RESIDUAL (a no-op returning None fails), the payload must EXIST, and the manifest must READ state=residual with its recorded why. The unconditional positive control on the same observable is the sibling arm, which requires the SAME call to return GONE and the directory to be gone
        qdir = self._claim()
        payload = os.path.join(qdir, "1.json")
        # THE LOCK IS PART OF THE CALL NOW. The terminal transition writes
        # through the LOCKED descriptor, so a caller without one is refused
        # rather than allowed to fall back to the pathname — the fallback
        # was the defect. Acquiring the lock here is what a real caller
        # does, and calling without it was how the earlier version of this
        # arm bypassed the integration.
        with _caps(qdir) as caps:
            lock, _why = todos._claim_lock(caps[1])
            self.assertIsNotNone(lock, "fixture: the claim must be lockable")
            try:
                self.assertEqual(
                    todos._sweep_claim(qdir, "the destination was taken", lock,
                                       *caps),
                    todos.RESIDUAL, "sweep did not report a durable residual")
            finally:
                lock.close()
        self.assertTrue(os.path.exists(payload),
                        "the cleanup deleted the last name of the inode it "
                        "was cleaning up around")
        meta = _manifest(os.path.join(qdir, todos.CLAIM_META))
        self.assertEqual(meta.get("state"), todos.RESIDUAL, meta)
        self.assertEqual(meta.get("residual_why"), "the destination was taken")

    def test_a_claim_that_owns_nothing_IS_removed(self):  # noqa: VACUOUS_ASSERTION — the absence assertions here are the point (the dir must be GONE) and their unconditional positive control is the sibling arm above, which requires the SAME call to leave a payload and a manifest in place
        """UNCONDITIONAL POSITIVE CONTROL, RE-BASED ON THE CURRENT CONTRACT.

        This asserted that the sweep REMOVED a claim owning nothing. It does
        not remove anything any more and says so in its own docstring: once a
        claim is anchored there is no safe delete-by-name, so it terminalizes
        and records instead. The arm was asserting a cleanup that a ruling on
        this very lane deliberately took out, and it only kept passing while
        _mark_residual crashed past its own guard on a None handle.

        Its JOB is unchanged — require the sweep to actually ACT, so the
        preservation arm above measures preservation rather than a sweep that
        stopped doing anything — so it now requires the terminal state to be
        RECORDED, which a sweep that no-ops cannot produce."""
        qdir = self._claim(with_payload=False)
        with _caps(qdir) as caps:
            pdir_fd, qdir_fd = caps
            handle, why = todos._claim_lock(qdir_fd)
            self.assertIsNotNone(handle, "fixture could not lock: %r" % (why,))
            try:
                self.assertEqual(
                    todos._sweep_claim(qdir, "empty", handle, *caps),
                    todos.RESIDUAL,
                    "the sweep did not record a terminal state")
            finally:
                handle.close()
        # THE CLAIM IS RETAINED, and its manifest says why — bytes over
        # cleanup, which is the whole point of the ruling.
        self.assertTrue(os.path.isdir(qdir),
                        "the claim was deleted by name after anchoring")
        meta = _manifest(os.path.join(qdir, todos.CLAIM_META))
        self.assertEqual(meta.get("state"), todos.RESIDUAL, meta)
        self.assertEqual(meta.get("residual_why"), "empty")

    def test_a_sweep_with_NO_locked_handle_records_nothing_and_says_so(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone on the manifest state is HALF the pair; the other half is unconditional and positive: the sweep must return exactly UNRECORDED and the claim directory must still exist. Its unconditional positive control is the sibling directly above, which requires the SAME call WITH a handle to record RESIDUAL on the same observable
        """The other half, which used to CRASH rather than answer: the
        terminal state is written through the locked manifest, so without a
        handle there is nothing to write it on. UNRECORDED is the retryable
        answer; an exception is not an answer at all."""
        qdir = self._claim(with_payload=False)
        with _caps(qdir) as caps:
            self.assertEqual(todos._sweep_claim(qdir, "empty", None, *caps),
                             todos.UNRECORDED,
                             "a sweep with no handle claimed to have recorded "
                             "a decision")
        self.assertTrue(os.path.isdir(qdir))
        meta = _manifest(os.path.join(qdir, todos.CLAIM_META))
        self.assertIsNone((meta or {}).get("state"),
                          "a terminal state was written without a lock")

    def test_recovery_is_idempotent_on_a_residual_and_never_disposes_of_it(self):  # noqa: VACUOUS_ASSERTION — the fixture carries the unconditional positive control on the same observable: _sweep_claim must RETURN RESIDUAL before anything is measured, so there is provably a terminal state to be idempotent about. The arm then requires two recoveries to report exactly [RESIDUAL] and the payload's st_ino/st_size to be unchanged — none of which a no-op recovery produces
        qdir = self._claim()
        payload = os.path.join(qdir, "1.json")
        with _caps(qdir) as caps:
            lock, _why = todos._claim_lock(caps[1])
            self.assertIsNotNone(lock, "fixture: the claim must be lockable")
            try:
                self.assertEqual(
                    todos._sweep_claim(qdir, "the destination was taken", lock,
                                       *caps),
                    todos.RESIDUAL,
                    "fixture: the residual must be recorded, or there is no "
                    "terminal state to be idempotent ABOUT")
            finally:
                lock.close()
        before = os.stat(payload)
        first = todos.recover_claims(self.SID)
        second = todos.recover_claims(self.SID)
        for out in (first, second):
            self.assertEqual([o["outcome"] for o in out], [todos.RESIDUAL], out)
            self.assertEqual(out[0]["preserved"], payload)
        after = os.stat(payload)
        # THE SAME INODE, UNTOUCHED. A residual is a decision already taken;
        # re-deciding it would either churn forever or reach the disposal it
        # exists to prevent.
        self.assertEqual((before.st_ino, before.st_size),
                         (after.st_ino, after.st_size))
        self.assertTrue(os.path.isdir(qdir), "recovery disposed of a residual")


class ResidualIsReachedThroughTheREALCallerTest(unittest.TestCase):
    """The step-3 refutation — MY IDEMPOTENCE ARM BYPASSED THE INTEGRATION.

    It called `_sweep_claim` directly, so it proved that function idempotent
    and said nothing about `_recover_one`, which reported `completed-residual`
    WITHOUT durably transitioning the manifest — leaving the next pass to retry
    the retirement forever. These arms go through `recover_claims`, twice, and
    reach the residual by the road a review actually identified: on the
    cross-filesystem path the retire's hard link NECESSARILY EXDEVs, because
    the payload sits beside the row and the undo is on the other filesystem by
    the same split that forced the snapshot branch."""

    SID = "sess-realcaller"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-realcaller-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _completed_claim(self):
        """A claim recovery will call `completed`: payload digest already
        matches the undo at the location the code derives."""
        row = {"id": "1", todos.STAMP: "t1", "subject": "ALREADY REMOVED"}
        body = json.dumps(row)
        qdir = _mint_claim(self.d, self.SID)
        payload = os.path.join(qdir, "1.json")
        with open(payload, "w") as fh:
            fh.write(body)
        with open(_derived_undo(self.SID, qdir, "1.json"), "w") as fh:
            fh.write(body)
        with open(os.path.join(qdir, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir), "sid": self.SID,
                       "row": "1.json", "payload": "1.json",
                       "identity": ["1", "t1"],
                       "payload_sha256": _digest_of(payload)}, fh)
        return qdir, payload

    def test_a_residual_that_cannot_be_RECORDED_is_not_reported_as_one(self):
        """FAILURE INJECTION: reporting a terminal state the disk does not
        carry is the laundering this whole cure exists to stop."""
        qdir, payload = self._completed_claim()
        # NO EXDEV INJECTION ANY MORE. This used to force a cross-filesystem
        # RETIRE, and the retire is deleted — the injection reached the
        # publish instead and the arm would have read as exercising a path it
        # does not. The property under test never needed it: a completed claim
        # whose terminal transition cannot be WRITTEN must not be reported as
        # a residual, and failing _mark_residual is the whole condition.
        with mock.patch.object(todos, "_mark_residual",
                               lambda *a, **k: False):
            out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["completed-unrecorded"],
                         out)
        self.assertEqual(out[0]["preserved"], payload)
        self.assertTrue(os.path.exists(payload))

    def test_an_unreadable_claim_directory_is_never_reported_GONE(self):  # noqa: VACUOUS_ASSERTION — the assertion is a specific VALUE, not an absence: the sweep must return exactly UNRECORDED. It also proves the injection fired, because an unfired mock leaves a readable directory holding a payload, which returns RESIDUAL — a different value that fails this assertion. A no-op returning None fails it too
        """`_sweep_claim` read a failed listdir as "owns nothing" and returned
        True — cannot-tell silently becoming nothing-here, in the one function
        whose answer decides whether a caller believes bytes were preserved."""
        qdir, _payload = self._completed_claim()
        # TWELFTH RE-POINT. listdir takes the claim's DESCRIPTOR now, so a
        # discriminator comparing a path string matched nothing and the arm
        # silently measured the ordinary residual path instead. Identified by
        # (st_dev, st_ino) — a descriptor has no name, and inode alone is not
        # RE-AIMED AT WHAT THE SWEEP ACTUALLY READS. This blinded os.listdir
        # on the claim directory, because the sweep used to LIST it before
        # deciding. It does not any more — the structural ruling made it
        # terminalize instead of enumerate-then-delete — so the injection
        # stopped reaching the code under test and the arm reported RESIDUAL
        # while claiming to prove something about unreadability.
        #
        # The live property is the same sentence one layer in: a claim whose
        # RECORD cannot be read must not be marked terminal, because the mark
        # is written onto that record and a terminal state nobody can parse is
        # the unresolved-and-unreported claim this leg exists to prevent. And
        # it is reached with a real disk state rather than a mock: a frame
        # that carries its closing byte and still does not parse is COMMITTED
        # corruption, which fails closed.
        with _caps(qdir) as caps:
            lock, _why = todos._claim_lock(caps[1])
            self.assertIsNotNone(lock, "fixture: the claim must be lockable")
            try:
                with open(os.path.join(qdir, todos.CLAIM_META), "ab") as fh:
                    fh.write(todos.RS + b'{"state": "trunc\n')
                self.assertEqual(
                    todos._sweep_claim(qdir, "unreadable", lock, *caps),
                    todos.UNRECORDED,
                    "a claim whose record cannot be read was marked terminal")
            finally:
                lock.close()
        # AND NOTHING WAS WRITTEN ON TOP OF THE CORRUPTION.
        with self.assertRaises(todos.ManifestCorrupt):
            todos._frames(open(os.path.join(qdir, todos.CLAIM_META),
                               "rb").read())


class NoModuleLevelConstantIsBoundTwiceTest(unittest.TestCase):
    """THE SHADOW EVERY OTHER ARM IN THIS FILE MISSED.

    GONE had been a TUPLE of terminal task statuses at module scope for
    months, read as `if st in GONE`. A second module-level GONE = "gone",
    added as a sweep result, rebound it: module assignments run top to bottom,
    so the later one won for every call after import, and the membership test
    silently became a SUBSTRING search. Valid syntax, correct for some inputs,
    TypeError only on None — raised inside `capture`, whose caller `record`
    swallows every exception. The task mirror simply stopped being written.

    NOTHING ELSE HERE COULD SEE IT. Both probes, every seam arm and the whole
    claim suite were green, because the damage was in a subsystem the change
    never touched, reached purely through a NAME. Reading the diff cannot
    catch it either: the diff shows one new constant and looks correct. Only a
    structural check over the module's own bindings can."""

    # An intentional rebinding belongs here WITH ITS REASON, so this stays a
    # decision record rather than a silencer. Empty is the honest state today.
    INTENTIONAL = {}

    def test_no_module_level_constant_is_bound_twice(self):
        src = open(todos.__file__, encoding="utf-8").read()
        seen = {}
        for node in ast.parse(src).body:      # MODULE LEVEL ONLY — a name
            if isinstance(node, ast.Assign):  # rebound inside a function is
                targets = node.targets        # scoped and not this defect
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            for t in targets:
                if not isinstance(t, ast.Name):
                    continue
                if not (t.id[:1].isalpha() and t.id.isupper()):
                    continue
                seen.setdefault(t.id, []).append(node.lineno)
        doubled = {n: ls for n, ls in seen.items()
                   if len(ls) > 1 and n not in self.INTENTIONAL}
        self.assertEqual(doubled, {},
                         "a module-level constant is bound more than once in "
                         "%s — the second binding changes the TYPE every "
                         "earlier expression operates on, silently. Rename it, "
                         "or record it in INTENTIONAL with the reason."
                         % todos.__file__)
        # UNCONDITIONAL POSITIVE CONTROL: the walk really did find constants,
        # so an empty `doubled` means "none are doubled" rather than "nothing
        # was inspected". A parse that yielded nothing would satisfy the
        # assertion above and prove exactly nothing.
        self.assertIn("GONE", seen, "the module walk found no GONE at all")
        self.assertGreater(len(seen), 10,
                           "only %d module-level constants found — the walk is "
                           "not seeing this module" % len(seen))


class WithoutTheCapabilityEveryOperationREFUSESTest(unittest.TestCase):
    """The seam 4 — THE REFUSAL PATH IS TESTABLE BECAUSE THE CAPABILITY
    CHECK IS DATA.

    I argued this branch was unreachable on Linux, so writing it would ship a
    dead path that reads as tested and the honest alternative was refusing at
    import. Both were wrong, and the framing was the error: `os.supports_dir_fd`
    is a SET, so a test can remove one entry and the preflight must refuse. A
    capability check over patchable data is neither dead nor fatal.

    The bar is not just that it refuses — it is that NOTHING WAS TOUCHED. A
    refusal that has already moved the row is a worse outcome than proceeding,
    because the operator is told nothing happened while the disk disagrees."""

    SID = "sess-nocap"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-nocap-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)
        self.full = os.path.join(self.d, "1.json")
        self.row = {"id": "1", todos.STAMP: "t1", "subject": "UNTOUCHED"}
        with open(self.full, "w") as fh:
            json.dump(self.row, fh)
        self.before = os.stat(self.full)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _without(self, fn):
        return mock.patch.object(
            todos.os, "supports_dir_fd",
            frozenset(x for x in os.supports_dir_fd if x is not fn))

    def _a_required_primitive(self):
        """A primitive the census ACTUALLY requires, chosen from the census.

        These arms named os.unlink and os.rename, which the census stopped
        requiring once it was derived from the call sites — so removing them
        no longer triggers a refusal and the arms went quiet rather than red.
        A test that hard-codes a requirement rots the moment the requirement
        is corrected, and it rots SILENTLY, which is the shape this lane
        exists to remove. Derived, so it cannot."""
        by_name = {getattr(f, "__name__", ""): f for f in os.supports_dir_fd}
        for name in todos._NEED_DIR_FD:
            if name in by_name:
                return name, by_name[name]
        self.fail("no censused primitive is available to remove")

    def test_trash_REFUSES_and_names_the_missing_primitive(self):
        with self._without(os.link):
            err = todos._trash(self.SID, self.full, self.row,
                               {"removed": [], "failed": []})
        self.assertIsInstance(err, str)
        self.assertIn("nothing was removed", err)
        self.assertIn("link", err, "the refusal must NAME the primitive — "
                                   "'cannot operate safely here' is not "
                                   "something an operator can act on")
        after = os.stat(self.full)
        self.assertEqual((self.before.st_ino, self.before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns),
                         "the row was touched by a call that reported doing "
                         "nothing")
        self.assertEqual([n for n in os.listdir(self.d)
                          if n.startswith(todos.CLAIM_PREFIX)], [],
                         "a claim was minted before the preflight refused")

    def test_recovery_REFUSES_without_deciding_anything(self):
        name, fn = self._a_required_primitive()
        with self._without(fn):
            out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["refused"], out)
        self.assertIn(name, out[0]["why"],
                      "the refusal does not NAME the missing primitive")
        self.assertTrue(os.path.exists(self.full))

    def test_a_MISSING_listdir_fd_form_is_caught_too(self):
        """listdir has no dir_fd — it takes an fd directly — so it lives in a
        different support set and a check that only walked supports_dir_fd
        would miss it entirely."""
        with mock.patch.object(todos.os, "supports_fd", frozenset()):
            err = todos._trash(self.SID, self.full, self.row,
                               {"removed": [], "failed": []})
        self.assertIsInstance(err, str)
        self.assertIn("listdir(fd)", err)

    def test_the_preflight_passes_UNPATCHED(self):  # noqa: VACUOUS_ASSERTION — this IS the unconditional positive control for the three refusal arms above: it requires the gap to be EMPTY and a real removal to SUCCEED on this platform, so those arms measure a refusal rather than a module that refuses everything
        """UNCONDITIONAL POSITIVE CONTROL: without patching, this platform has
        every capability and a real removal succeeds — so the arms above
        measure the missing-capability refusal rather than a module that
        refuses unconditionally."""
        self.assertEqual(todos._capability_gap(), "")
        rep = {"removed": [], "failed": []}
        self.assertIsNone(todos._trash(self.SID, self.full, self.row, rep))
        self.assertFalse(os.path.exists(self.full))


class EveryDurabilitySyncFailsLOUDTest(unittest.TestCase):
    """The cumulative refute — A SWALLOWED SYNC IS A DECISION THE DISK
    HAS NOT MADE.

    Each of these fsyncs guards a different crash window, and each was either
    swallowed or ordered wrongly. The bar is the same everywhere: when the
    sync fails, the operation REFUSES and says so, and the claim is left
    standing rather than terminalized on top of an unsynced act — because the
    claim is the only thing that would bring the row back after a crash."""

    SID = "sess-sync"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-sync-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)
        self.full = os.path.join(self.d, "1.json")
        self.row = {"id": "1", todos.STAMP: "t1", "subject": "THE ROW"}
        with open(self.full, "w") as fh:
            json.dump(self.row, fh)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _failing_on(self, want, exact=False):
        """Fail _fsync_dir for ONE directory, so each arm names the window it
        is testing rather than failing every sync and proving only that
        something refused.

        `exact` exists because a substring needle is not a window. The row
        directory's name is the session id, which also appears in the TRASH
        path — so a substring match tripped the undo-directory mint instead,
        and the arm refused at a window it does not name. The message
        assertion is what caught it."""
        # TENTH RE-POINT. The post-anchor syncs are os.fsync(pinned_fd) now,
        # not _fsync_dir(path) — so an injection on _fsync_dir reaches only
        # the pre-anchor mkdir and these arms stopped testing anything. The
        # WINDOW is unchanged: a directory whose durability fails must cancel
        # the removal. Only the call has moved, for the tenth time in this
        # lane.
        #
        # A descriptor has no name, so the target is identified by its INODE —
        # which is the honest identity anyway, and is exactly what the code
        # switched to.
        real_dir, real_fsync = todos._fsync_dir, os.fsync

        def want_id():
            """RESOLVED LAZILY, and that is the fix for the re-point treadmill.

            This resolved `want` to an inode ONCE, before the call. The undo
            root is CREATED DURING the call now (creation is
            descriptor-relative, which is what closed the ancestry TOCTOU), so
            at setup time it does not exist, want_id was None, and the
            injection never fired — the arm reported 'the removal did not
            refuse' about a removal that was never interfered with.

            Every previous re-point chased the MECHANISM: path to descriptor,
            _fsync_dir to os.fsync, name to inode. This one is about WHEN, and
            resolving at each call is stable under all of them: the target is
            whatever that pathname denotes at the moment the sync happens,
            which is the same question the arm has always been asking."""
            try:
                st = os.stat(want)
                return (st.st_dev, st.st_ino)
            except OSError:
                return None

        def fail_dir(d, *a, **k):
            hit = (os.path.realpath(str(d)) == os.path.realpath(want)
                   if exact else want in str(d))
            if hit:
                raise OSError("no fsync on this mount")
            return real_dir(d, *a, **k)

        def fail_fd(fd, *a, **k):
            # (st_dev, st_ino) — AN INODE NUMBER IS ONLY UNIQUE WITHIN A
            # DEVICE, and this lane exists because the claim and the undo can
            # live on different filesystems. Matching on the inode alone
            # could fire on an unrelated directory that happens to share a
            # number across devices. It is also the identity the module's own
            # same-inode check uses, which is the point: the test should name
            # things the way the code does.
            try:
                st = os.fstat(fd)
                target = want_id()
                same = target is not None and (st.st_dev, st.st_ino) == target
            except OSError:
                same = False
            if same:
                raise OSError("no fsync on this mount")
            return real_fsync(fd, *a, **k)

        return _both(mock.patch.object(todos, "_fsync_dir", fail_dir),
                     mock.patch.object(todos.os, "fsync", fail_fd))

    def test_the_UNDO_ROOT_sync_failing_cancels_the_removal(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is IN this arm, on the same observable: after the refusal it runs the SAME _trash with no injection and requires it to return None and the row to be GONE, so the arm cannot pass on a _trash that refuses everything
        # THE REAL PATH, not the bare name. todos.TRASH is "promoted-trash" —
        # a NAME — and the old substring match against path-based syncs made
        # that work by accident. An inode target needs something os.stat can
        # resolve, so the arm names the directory it actually means.
        from helm import record
        troot = os.path.join(record.session_dir(self.SID), todos.TRASH)
        with self._failing_on(troot, exact=True):
            err = todos._trash(self.SID, self.full, self.row,
                               {"removed": [], "failed": []})
        self.assertIsInstance(err, str, "the removal did not refuse")
        self.assertTrue(os.path.exists(self.full),
                        "the row was removed on an unsynced undo")
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: without the
        # injection the SAME call removes the row. Otherwise this arm passes
        # on a _trash that refuses everything, which is exactly the failure it
        # would be least able to notice.
        self.assertIsNone(todos._trash(self.SID, self.full, self.row,
                                       {"removed": [], "failed": []}))
        self.assertFalse(os.path.exists(self.full))

    def test_the_ROW_DIRECTORY_sync_failing_puts_the_row_BACK(self):
        with self._failing_on(self.d, exact=True):
            err = todos._trash(self.SID, self.full, self.row,
                               {"removed": [], "failed": []})
        self.assertIsInstance(err, str, "the removal did not refuse")
        self.assertIn("durable", err)
        self.assertTrue(os.path.exists(self.full), "the row did not come back")

    def test_an_UNRECOVERED_sweep_is_reported_retryable_not_terminal(self):
        """THE THIRD SWEEP STATE. SWEPT is done and RESIDUAL is terminal;
        UNRECORDED is neither, and collapsing it into the residual bucket
        invites an operator to dispose of a claim that recorded no decision.
        The recovery callers did exactly that until this arm."""
        rep = {"removed": [], "failed": []}
        self.assertIsNone(todos._trash(self.SID, self.full, self.row, rep))
        qdir = os.path.dirname(rep["residuals"][0]["payload"])
        # rewind the terminal mark so recovery must decide it again
        meta_path = os.path.join(qdir, todos.CLAIM_META)
        meta = _manifest(meta_path)
        meta.pop("state", None)
        meta.pop("residual_why", None)
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        with mock.patch.object(todos, "_mark_residual",
                               lambda *a, **k: False):
            out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["completed-unrecorded"],
                         out)
        self.assertNotIn(todos.RESIDUAL, out[0]["outcome"].split("-")[-1:],
                         "an unrecorded claim was reported as a residual")

    def test_all_three_sweep_states_are_distinguishable(self):  # noqa: VACUOUS_ASSERTION — every assertion here is a required VALUE, not an absence: the three states must be exactly SWEPT, RESIDUAL and UNRECORDED, and a _sweep_claim that returned one constant for everything fails all three at once
        """The states are only useful if a caller can tell them apart, so this
        pins all three from one place rather than trusting three separate arms
        to have covered the set."""
        seen = {}
        # SWEPT: a claim owning nothing
        q1 = _mint_claim(self.d, self.SID)
        with open(os.path.join(q1, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q1), "sid": self.SID,
                       "row": "x.json", "payload": "x.json"}, fh)
        with _caps(q1) as caps:
            # NOT "SWEPT" — _sweep_claim cannot produce it any more and says
            # so in its own docstring: after the anchor there is no safe
            # delete-by-name, so it terminalizes and never removes. With no
            # locked handle the terminal state cannot be written at all, so
            # UNRECORDED is the honest answer and the one this call has.
            seen["no-handle"] = todos._sweep_claim(q1, "empty", None, *caps)
        # RESIDUAL: holds bytes, and the transition is recorded under the lock
        q2 = _mint_claim(self.d, self.SID)
        with open(os.path.join(q2, "x.json"), "w") as fh:
            fh.write("{}")
        with open(os.path.join(q2, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q2), "sid": self.SID,
                       "row": "x.json", "payload": "x.json"}, fh)
        with _caps(q2) as caps:
            # THE LOCK NEEDS THE ANCHOR TOO. _claim_lock refuses without a
            # pinned claim now — there is no honest path-based version of it —
            # so the caps are opened first and the lock is taken through them,
            # which is the order a real caller uses.
            lock, _why = todos._claim_lock(caps[1])
            self.assertIsNotNone(lock, "fixture: the claim must be lockable")
            try:
                seen["residual"] = todos._sweep_claim(q2, "held bytes", lock,
                                                      *caps)
            finally:
                lock.close()
        # UNRECORDED: holds bytes and the terminal state CANNOT BE WRITTEN.
        # This used to pass held=None, which is no longer a representable
        # call — the signature requires the lock. That is the point of the
        # required-capability shape, and it means the arm has to reach this
        # state the way production does: the record fails to become durable.
        q3 = _mint_claim(self.d, self.SID)
        with open(os.path.join(q3, "x.json"), "w") as fh:
            fh.write("{}")
        with open(os.path.join(q3, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q3), "sid": self.SID,
                       "row": "x.json", "payload": "x.json"}, fh)
        with _caps(q3) as caps:
            lock3, _why3 = todos._claim_lock(caps[1])
            self.assertIsNotNone(lock3, "fixture: the claim must be lockable")
            real_fsync = os.fsync
            # THE MANIFEST FILE'S OWN SYNC. This targeted the claim
            # DIRECTORY's descriptor, and an append only syncs the directory
            # when it CREATED the entry — a terminal transition does not — so
            # the injection stopped firing on this path entirely and the state
            # came back RESIDUAL. The record's durability is what UNRECORDED
            # is about, so the record's fsync is what has to fail.
            st3 = os.fstat(lock3.fileno())
            fired3 = []

            def no_durable(fd, *a, **k):
                try:
                    f = os.fstat(fd)
                except OSError:
                    return real_fsync(fd, *a, **k)
                if (f.st_dev, f.st_ino) == (st3.st_dev, st3.st_ino):
                    fired3.append(1)
                    raise OSError(errno.EIO, "no fsync on this mount")
                return real_fsync(fd, *a, **k)

            try:
                with mock.patch.object(todos.os, "fsync", no_durable):
                    seen["unrecorded"] = todos._sweep_claim(
                        q3, "held bytes", lock3, *caps)
            finally:
                lock3.close()
            self.assertTrue(fired3, "the manifest sync never failed — the "
                                    "UNRECORDED case is vacuous")
        self.assertEqual(seen, {"no-handle": todos.UNRECORDED,
                                "residual": todos.RESIDUAL,
                                "unrecorded": todos.UNRECORDED}, seen)
        # AND THE TWO UNRECORDED ROUTES ARE GENUINELY DIFFERENT CAUSES, which
        # is the distinction this arm exists for: one had nothing to write on,
        # the other could not make the write durable.


class TheFirstRemovalMakesItsWholeUNDOCHAINDurableTest(unittest.TestCase):
    """THE UNDO ROOT'S OWN ENTRY. os.makedirs(dest) creates
    intermediates, so syncing trash_root persists the dest entry and leaves
    the entry NAMING trash_root unsynced. On a first removal a crash can lose
    the undo root after the row is already gone from its own name.

    MEASURED, and the count is why the fix is not two steps: a first removal
    in a fresh home creates SIX levels, so a one-level sync leaves FIVE
    ancestor entries unsynced."""

    SID = "sess-firstrm"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-firstrm-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _remove_one(self, name):
        full = os.path.join(self.d, name)
        row = {"id": name[0], todos.STAMP: "t" + name[0]}
        with open(full, "w") as fh:
            json.dump(row, fh)
        # RECORDED BY INODE, because the durability moved onto DESCRIPTORS.
        # This patched _fsync_dir and recorded PATHNAMES; the creation is now
        # descriptor-relative (that is what closes the TOCTOU), so it syncs
        # fds and this instrument saw nothing while the syncs were happening.
        # A moved seam, self-inflicted, and the arm reported "synced []" for a
        # transaction that synced every level.
        synced, real = [], os.fsync

        def note(fd, *a, **k):
            try:
                st = os.fstat(fd)
                synced.append((st.st_dev, st.st_ino))
            except (OSError, TypeError):
                pass
            return real(fd, *a, **k)

        with mock.patch.object(os, "fsync", note):
            err = todos._trash(self.SID, full, row,
                               {"removed": [], "failed": []})
        self.assertIsNone(err, err)
        return synced

    def test_every_ancestor_the_first_removal_CREATES_is_synced(self):  # noqa: VACUOUS_ASSERTION — the only absence here is the FIXTURE's precondition (the undo root must not exist yet, which makes this the first-removal case rather than the ordinary one). Every acceptance assertion is a required PRESENCE plus an ORDER: the root's parent must appear in the synced list, the root must appear, and the parent must come first. A _trash that synced nothing fails all three
        from helm import record
        troot = os.path.realpath(os.path.join(record.session_dir(self.SID),
                                              todos.TRASH))
        self.assertFalse(os.path.isdir(troot), "fixture: the undo root must "
                                               "NOT exist yet, or this arm is "
                                               "testing the ordinary case")
        # THE EXACT CHAIN, DERIVED BEFORE THE CALL. Production
        # promises that EVERY level it creates is made durable, and this arm
        # checked exactly two of them — the root and its parent — so a
        # mutation omitting any MIDDLE ancestor passed. Measured on this box, a
        # first removal creates six levels; asserting two of six is asserting
        # the ends of a chain and calling the chain proven.
        #
        # The chain is derived from the filesystem as it stands BEFORE the
        # removal (walk up until something exists), so it is what production
        # will actually have to create rather than a number written down here.
        missing, probe = [], troot
        while probe and probe != os.path.dirname(probe) \
                and not os.path.isdir(probe):
            missing.append(probe)
            probe = os.path.dirname(probe)
        self.assertGreater(len(missing), 2,
                           "the fixture creates only %d level(s); this arm is "
                           "about a DEEP chain" % len(missing))
        outermost_existing = probe

        synced = self._remove_one("1.json")

        # every created entry's PARENT must have been synced — that entry is
        # what names the level below it, and an unsynced name is a level that
        # can vanish after the row is already gone from its own path.
        def ident(path):
            st = os.stat(path)
            return (st.st_dev, st.st_ino)

        for level in missing:
            parent = os.path.dirname(level)
            self.assertIn(ident(parent), synced,
                          "the entry NAMING %s was never synced; created %r, "
                          "synced %d descriptor(s)"
                          % (level, missing, len(synced)))
        # OUTERMOST FIRST, all the way down: an entry is durable before
        # anything beneath it is relied on. Checking only root-before-parent
        # would pass on a chain synced in any order below that.
        order = [synced.index(ident(os.path.dirname(l)))
                 for l in reversed(missing)]
        self.assertEqual(order, sorted(order),
                         "the chain was not synced outermost-first: created "
                         "%r, sync order %r" % (list(reversed(missing)), synced))
        self.assertIn(ident(outermost_existing), synced,
                      "the entry naming the first level we created was never "
                      "synced")

    def test_an_EXISTING_root_does_not_pay_for_the_first_one(self):  # noqa: VACUOUS_ASSERTION — the absence assertion (no parent sync) is paired with a required PRESENCE in the same arm: trash_root itself must still be synced, and the first removal above requires the parent sync to happen when the root is new. A _trash that synced nothing fails that sibling
        """The existing-root control: once the tree is there, nothing
        above trash_root is created, so nothing above it may be synced. A fix
        that always walked to the session directory would pass the arm above
        and silently sync on every removal forever."""
        from helm import record
        self._remove_one("1.json")                      # creates the chain
        troot = os.path.realpath(os.path.join(record.session_dir(self.SID),
                                              todos.TRASH))
        synced = self._remove_one("2.json")             # tree already exists
        def ident(path):
            st = os.stat(path)
            return (st.st_dev, st.st_ino)

        self.assertNotIn(ident(os.path.dirname(troot)), synced,
                         "an ordinary removal synced the undo root's PARENT, "
                         "which it did not create: %r" % (synced,))
        self.assertIn(ident(troot), synced,
                      "the undo root itself must still be synced — the new "
                      "claim directory's entry lives in it")


class ASymlinkWhereAROWShouldBeIsNEVERActedOnTest(unittest.TestCase):
    """The seam 4 — THE CASE A DIRECTORY DESCRIPTOR CANNOT COVER.

    dir_fd bounds WHICH directory a name resolves in, so a member cannot
    escape into another tree. It says nothing about what the name resolves TO.
    A symlink at the payload name is inside the claim, reached by the correct
    descriptor, and points anywhere: linking it publishes a POINTER into the
    operator's row list instead of the bytes, and hashing it proves completion
    against a target nobody claimed.

    Recovery reads claim directories it did not create — a crashed sweep's, or
    a hand-edited one — so this is the realistic case, not a contrived one."""

    SID = "sess-symlink"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-symlink-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)
        self.elsewhere = os.path.join(self.tmp, "SOMEONE-ELSES-FILE")
        with open(self.elsewhere, "w") as fh:
            json.dump({"id": "9", todos.STAMP: "t9",
                       "subject": "NOT A ROW HELM CLAIMED"}, fh)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recovery_REFUSES_a_symlinked_payload_and_keeps_it(self):
        qdir = _mint_claim(self.d, self.SID)
        payload = os.path.join(qdir, "1.json")
        os.symlink(self.elsewhere, payload)          # a POINTER, not a row
        with open(os.path.join(qdir, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir), "sid": self.SID,
                       "row": "1.json", "payload": "1.json",
                       "identity": ["1", "t1"],
                       "payload_sha256": _digest_of(payload) or ""}, fh)
        out = todos.recover_claims(self.SID)
        self.assertEqual([o["outcome"] for o in out], ["unreadable"], out)
        self.assertIn("ordinary file", out[0]["why"])
        # NOT RESTORED: the row list must not gain a pointer to a file nobody
        # claimed.
        self.assertFalse(os.path.exists(os.path.join(self.d, "1.json")),
                         "a symlink was published into the row list")
        # AND NOT DESTROYED. The bytes are not ours to judge and not ours to
        # remove — the target belongs to someone else entirely.
        self.assertTrue(os.path.islink(payload), "the link was removed")
        self.assertTrue(os.path.exists(self.elsewhere),
                        "the link's TARGET was touched")

    def test_an_ORDINARY_payload_is_still_acted_on(self):  # noqa: VACUOUS_ASSERTION — this is the unconditional positive control for the arm above: it requires a REAL restore (the row present at its own path, carrying its own subject), which a recovery that refused everything cannot produce
        """UNCONDITIONAL POSITIVE CONTROL: the identical claim with a real
        file is restored, so the arm above measures the symlink refusal rather
        than a recovery that stopped acting."""
        qdir = _mint_claim(self.d, self.SID)
        payload = os.path.join(qdir, "1.json")
        with open(payload, "w") as fh:
            json.dump({"id": "1", todos.STAMP: "t1", "subject": "A REAL ROW"},
                      fh)
        with open(os.path.join(qdir, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(qdir), "sid": self.SID,
                       "row": "1.json", "payload": "1.json",
                       "identity": ["1", "t1"],
                       "payload_sha256": _digest_of(payload)}, fh)
        out = todos.recover_claims(self.SID)
        self.assertTrue(out[0]["outcome"].startswith("restored"), out)
        back = os.path.join(self.d, "1.json")
        self.assertTrue(os.path.exists(back))
        with open(back) as fh:
            self.assertEqual(json.load(fh)["subject"], "A REAL ROW")


class ThePUBLISHBindsTheJudgedINODETest(unittest.TestCase):
    """The root finding — os.link publishes a NAME, so fstat-ing and
    hashing an open payload and then linking its name RE-RESOLVES. Everything
    proven about the descriptor is proven about a file that need not still be
    there.

    The cure publishes through /proc/self/fd/N, which names the descriptor's
    own inode. This arm swaps the payload's NAME for another inode in the
    window between the judgement and the publish, and requires the undo to
    hold what was JUDGED rather than what the name later pointed at."""

    SID = "sess-publish"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-publish-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_payload_swapped_after_the_judgement_does_not_reach_the_undo(self):  # noqa: VACUOUS_ASSERTION — the fired-flag is not the acceptance, only the proof the swap armed. The call result IS consumed: err decides which branch is checked, and each branch makes a positive demand — either the undo holds THE JUDGED BYTES, or the row is back at its own path holding them AND no file anywhere under the undo tree carries the swapped-in subject. The sibling arm is the unconditional control
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "THE JUDGED BYTES"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        real_json, fired = todos._json_fd, []

        def swap_after_the_judgement(fd):
            got = real_json(fd)
            if not fired:
                fired.append(1)
                # The judgement is decided. Now REPLACE the payload's name
                # with a different inode — the exact window a name-based
                # publish would step into.
                claim = [n for n in os.listdir(self.d)
                         if n.startswith(todos.CLAIM_PREFIX)][0]
                victim = os.path.join(self.d, claim, "1.json")
                other = os.path.join(self.tmp, "OTHER")
                with open(other, "w") as fh:
                    json.dump({"id": "1", todos.STAMP: "t1",
                               "subject": "SWAPPED IN AFTERWARDS"}, fh)
                os.rename(other, victim)
            return got

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_json_fd", swap_after_the_judgement):
            err = todos._trash(self.SID, full, row, rep)
        self.assertEqual(fired, [1], "the swap never armed — arm is vacuous")
        # THE INVARIANT IS NOT "THE PUBLISH SUCCEEDS", IT IS "THE UNDO NEVER
        # HOLDS BYTES THE SWEEP DID NOT JUDGE". Two outcomes satisfy it and
        # both are correct: the publish binds the judged inode, or the removal
        # is CANCELLED and the row goes back. What must never happen is the
        # swapped-in bytes reaching the undo.
        #
        # This fixture produces the second, for a reason worth recording: the
        # rename takes the payload's ONLY name, so the judged inode drops to
        # nlink 0 and cannot be re-linked at all — /proc/self/fd/N reads as
        # deleted. My own earlier probe of this technique had created a second
        # link BEFORE the swap, so its inode never reached zero, and I
        # generalised from conditions I had not noticed were special. The
        # fd-publish binds the judged inode only while that inode still has a
        # name.
        if err is None:
            undo = _undo_of(rep, "1.json")
            with open(undo) as fh:
                self.assertEqual(json.load(fh)["subject"], "THE JUDGED BYTES",
                                 "the undo holds bytes the sweep never judged")
        else:
            self.assertTrue(os.path.exists(full),
                            "the removal was cancelled but the row was not "
                            "put back")
            with open(full) as fh:
                self.assertEqual(json.load(fh)["subject"], "THE JUDGED BYTES")
            for root, _dirs, files in os.walk(
                    os.path.join(self.tmp, "helm")):
                for n in files:
                    try:
                        got = json.load(open(os.path.join(root, n)))
                    except Exception:
                        continue
                    self.assertNotEqual(got.get("subject"),
                                        "SWAPPED IN AFTERWARDS",
                                        "swapped-in bytes reached the undo "
                                        "tree at %s" % os.path.join(root, n))

    def test_an_UNSWAPPED_removal_publishes_normally(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control for the swap arm: it requires a real removal (err None, the row gone, the undo holding its bytes) and that the undo and the retained payload are ONE inode, which a publish that copied or failed cannot produce
        """UNCONDITIONAL POSITIVE CONTROL: with nobody swapping, the same call
        publishes and the undo is one inode with the retained payload — so the
        arm above measures the binding rather than a publish that stopped
        working."""
        full = os.path.join(self.d, "2.json")
        row = {"id": "2", todos.STAMP: "t2", "subject": "ORDINARY"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        rep = {"removed": [], "failed": []}
        self.assertIsNone(todos._trash(self.SID, full, row, rep))
        self.assertFalse(os.path.exists(full))
        undo = _undo_of(rep, "2.json")
        with open(undo) as fh:
            self.assertEqual(json.load(fh)["subject"], "ORDINARY")
        r = rep["residuals"][0]
        self.assertEqual(os.stat(undo).st_ino, os.stat(r["payload"]).st_ino)


class TheDOUBLERaceNeverLosesTheJudgedBytesTest(unittest.TestCase):
    """TWO RACES AT ONCE, AND THE REPORT WAS THE ONLY RECORD.

    A writer takes the row's path AND the judged payload's name is swapped
    away. The link fails, the O_EXCL restore fails, and the old text reported
    the claimed bytes "preserved at <quarantine>" — a name that by then held
    the attacker's file. The judged inode had no name left and died when the
    descriptor closed. Data loss, under a report pointing at the wrong bytes.

    Neither single race loses anything; only their product does, which is why
    an arm for each of them individually could not have caught it."""

    SID = "sess-double"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-double-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _find(self, subject):
        hits = []
        for root, _dirs, files in os.walk(self.tmp):
            for n in files:
                fp = os.path.join(root, n)
                try:
                    if json.load(open(fp)).get("subject") == subject:
                        hits.append(fp)
                except Exception:
                    pass
        return hits

    def test_a_swapped_payload_AND_an_occupied_row_still_names_the_judged_bytes(self):
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "THE JUDGED BYTES"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        real_json, fired = todos._json_fd, []

        def both_races(fd):
            got = real_json(fd)
            if not fired:
                fired.append(1)
                claim = [n for n in os.listdir(self.d)
                         if n.startswith(todos.CLAIM_PREFIX)][0]
                # RACE ONE: take the judged payload's only name, so its inode
                # drops to nlink 0 and cannot be re-linked.
                other = os.path.join(self.tmp, "SWAPPED")
                with open(other, "w") as fh:
                    json.dump({"id": "1", todos.STAMP: "t1",
                               "subject": "SWAPPED IN"}, fh)
                os.rename(other, os.path.join(self.d, claim, "1.json"))
                # RACE TWO: a writer installs its own file at the row's path,
                # so the O_EXCL restore cannot land either.
                with open(full, "w") as fh:
                    json.dump({"id": "1", todos.STAMP: "t1",
                               "subject": "A NEWCOMER"}, fh)
            return got

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_json_fd", both_races):
            err = todos._trash(self.SID, full, row, rep)
        self.assertEqual(fired, [1], "the double race never armed")
        self.assertIsInstance(err, str, "the removal did not refuse")
        # THE BYTES EXIST SOMEWHERE...
        where = self._find("THE JUDGED BYTES")
        self.assertTrue(where, "the judged bytes were LOST")
        # ...AND THE REPORT NAMES THE PLACE THEY ARE. A refusal that says
        # "preserved at X" while the bytes are at Y is the same silently-absent
        # row this leg exists to prevent, one layer up: the report is the only
        # record an operator has.
        named = [p for p in where if p in err]
        self.assertTrue(named,
                        "the refusal does not name where the judged bytes "
                        "are. err=%r bytes at %r" % (err, where))
        # and the newcomer is untouched
        self.assertTrue(self._find("A NEWCOMER"), "the writer's file was lost")

    def test_EITHER_race_alone_still_puts_the_row_back(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control for the arm above: with only ONE race the row must be BACK at its own path carrying the judged bytes, which a _trash that refused everything cannot produce
        """UNCONDITIONAL POSITIVE CONTROL, and the reason the double race
        needed its own arm: with only the payload swapped, the restore still
        succeeds by copy, so neither single-race arm exercises the branch."""
        full = os.path.join(self.d, "2.json")
        row = {"id": "2", todos.STAMP: "t2", "subject": "ONE RACE ONLY"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        real_json, fired = todos._json_fd, []

        def swap_only(fd):
            got = real_json(fd)
            if not fired:
                fired.append(1)
                claim = [n for n in os.listdir(self.d)
                         if n.startswith(todos.CLAIM_PREFIX)][0]
                other = os.path.join(self.tmp, "SWAPPED2")
                with open(other, "w") as fh:
                    json.dump({"id": "2", todos.STAMP: "t2",
                               "subject": "SWAPPED IN"}, fh)
                os.rename(other, os.path.join(self.d, claim, "2.json"))
            return got

        with mock.patch.object(todos, "_json_fd", swap_only):
            todos._trash(self.SID, full, row, {"removed": [], "failed": []})
        self.assertEqual(fired, [1], "the single race never armed")
        self.assertTrue(os.path.exists(full), "the row was not put back")
        with open(full) as fh:
            self.assertEqual(json.load(fh)["subject"], "ONE RACE ONLY")


class RecoveryLeaksNoDescriptorsTest(unittest.TestCase):
    """EVERY EARLY RETURN AFTER THE PAYLOAD OPEN LEAKED IT.

    completed, inconsistent and same-inode-restored all returned without
    closing, so a recovery pass over many claims walked the process toward its
    descriptor limit. No single-claim arm can see this: one leak looks exactly
    like no leak. It is only visible as a COUNT across many."""

    SID = "sess-fdcount"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-fdcount-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open_fds(self):
        return _fd_targets()

    def _assert_no_leak(self, before, after, faults):
        _assert_no_leak(self, before, after, faults)

    def _claim(self, i, digest_matches):
        """A claim recovery will decide. `digest_matches=False` makes it
        INCONSISTENT, which is one of the paths that leaked."""
        q = _mint_claim(self.d, self.SID)
        name = "%d.json" % i
        p = os.path.join(q, name)
        with open(p, "w") as fh:
            json.dump({"id": str(i), todos.STAMP: "t%d" % i}, fh)
        with open(os.path.join(q, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                       "row": name, "payload": name,
                       "identity": [str(i), "t%d" % i],
                       "payload_sha256": (_digest_of(p) if digest_matches
                                          else "0" * 64)}, fh)

    def test_many_claims_do_not_walk_the_descriptor_table(self):
        for i in range(25):
            self._claim(i, digest_matches=(i % 2 == 0))
        before = self._open_fds()
        out = todos.recover_claims(self.SID)
        after = self._open_fds()
        self.assertEqual(len(out), 25, "fixture: every claim must be decided")
        # NO SLACK. "A handful for interpreter bookkeeping" is a leak
        # allowance on an assertion named "leaks no descriptor" — one to four
        # real leaks passed under it. The set difference names what
        # leaked instead of tolerating it.
        self._assert_no_leak(before, after, len(out))

    def test_the_COUNT_is_what_makes_it_visible(self):  # noqa: VACUOUS_ASSERTION — this is the unconditional positive control for the arm above: it requires the decisions to actually happen (a mix of inconsistent and restored outcomes), so the fd comparison measures a recovery that ran rather than one that returned early
        """UNCONDITIONAL POSITIVE CONTROL: the claims really are being decided
        down both branches, so the count above measures a recovery that ran."""
        for i in range(4):
            self._claim(i, digest_matches=(i % 2 == 0))
        kinds = {o["outcome"] for o in todos.recover_claims(self.SID)}
        self.assertIn("inconsistent", kinds, kinds)
        self.assertTrue(any(k.startswith("restored") for k in kinds), kinds)


class AnUnreadableUNDONeverResurrectsARemovedRowTest(unittest.TestCase):
    """REPRODUCED — a completed removal came back because a
    DESCRIPTOR COULD NOT BE OPENED.

    Every failed open on the undo path returned None into one `undo_digest`,
    and the completed test read that as "the undo does not match" — so a claim
    whose removal HAD completed fell through to the restore branch and put the
    row back. Cannot-tell silently becoming nothing, for the fourth time in
    this lane, this time arriving through the cure for the first three.

    An absent undo and an unreadable one are different facts and only the
    first may be acted on."""

    SID = "sess-undoread"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-undoread-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)
        self.full = os.path.join(self.d, "1.json")
        self.row = {"id": "1", todos.STAMP: "t1", "subject": "REMOVED FOR REAL"}
        with open(self.full, "w") as fh:
            json.dump(self.row, fh)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _completed_claim_with_its_undo(self):
        """A real removal, then rewind the terminal mark so recovery must
        decide it again — the state a crash between publish and bookkeeping
        leaves."""
        rep = {"removed": [], "failed": []}
        self.assertIsNone(todos._trash(self.SID, self.full, self.row, rep))
        qdir = os.path.dirname(rep["residuals"][0]["payload"])
        meta_path = os.path.join(qdir, todos.CLAIM_META)
        meta = _manifest(meta_path)        # framed now, not a bare object
        meta.pop("state", None)
        meta.pop("residual_why", None)
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        return qdir

    def test_an_unreadable_undo_is_UNDECIDABLE_never_a_restore(self):
        qdir = self._completed_claim_with_its_undo()
        real = todos._open_member
        blocked = []

        def cannot_open_the_undo(dir_fd, name, flags=None):
            # refuse ONLY the undo member, so the payload still opens and the
            # arm reaches the completed test rather than failing earlier
            if name == "1.json" and dir_fd not in (None,):
                try:
                    here = os.listdir(dir_fd)
                except OSError:
                    here = []
                if todos.CLAIM_META not in here:      # the undo dir, not the claim
                    blocked.append(name)
                    return None
            return real(dir_fd, name, flags)

        with mock.patch.object(todos, "_open_member", cannot_open_the_undo):
            out = todos.recover_claims(self.SID)
        self.assertTrue(blocked, "the undo open was never blocked — arm is "
                                 "vacuous")
        self.assertEqual([o["outcome"] for o in out], ["undecidable"], out)
        self.assertFalse(os.path.exists(self.full),
                         "a completed removal was RESURRECTED because a "
                         "descriptor could not be opened")
        self.assertTrue(os.path.isdir(qdir), "the claim was disposed of")

    def test_a_READABLE_undo_still_decides_completed(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control for the arm above: without the block the SAME claim must reach a completed decision and the row must stay removed, which a recovery that refused everything cannot produce
        """UNCONDITIONAL POSITIVE CONTROL: without the block the same claim
        reaches a completed decision, so the arm above measures the refusal
        rather than a recovery that stopped deciding."""
        self._completed_claim_with_its_undo()
        out = todos.recover_claims(self.SID)
        self.assertTrue(out[0]["outcome"].startswith("completed"), out)
        self.assertFalse(os.path.exists(self.full))


class AFaultBetweenOpenAndOwnershipLeaksNothingTest(unittest.TestCase):
    """(5) WAS FIXED IN PRODUCTION AND NEVER ARMED.

    os.fdopen takes ownership of a raw descriptor only on SUCCESS. Between the
    open and the wrapper existing, nobody owns it, so a fault there leaks it —
    and a leak is invisible in any single call. Two sites have that window:
    the claim manifest and the claim lock.

    The arm counts descriptors across many faults, because the aggregate is
    the only observable; and it asserts the fault FIRED, because an injection
    that never runs looks exactly like a clean run."""

    SID = "sess-fdopen"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-fdopen-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fds(self):
        return _fd_targets()

    def _assert_no_leak(self, before, after, faults):
        _assert_no_leak(self, before, after, faults)

    def test_a_manifest_fdopen_fault_leaks_no_descriptor(self):  # noqa: VACUOUS_ASSERTION — the call result IS consumed on every iteration: err must be a string (the removal refused) and the row must still exist. The fired-list is the must-fire observable, not the acceptance
        real, fired = os.fdopen, []

        def boom(fd, *a, **k):
            fired.append(fd)
            raise OSError("simulated fdopen failure")

        before = self._fds()
        for i in range(20):
            full = os.path.join(self.d, "%d.json" % i)
            row = {"id": str(i), todos.STAMP: "t%d" % i}
            with open(full, "w") as fh:
                json.dump(row, fh)
            with mock.patch.object(todos.os, "fdopen", boom):
                err = todos._trash(self.SID, full, row,
                                   {"removed": [], "failed": []})
            self.assertIsInstance(err, str, "the removal did not refuse")
            self.assertTrue(os.path.exists(full), "the row was removed anyway")
        after = self._fds()
        self.assertEqual(len(fired), 20, "the fdopen fault never fired — arm "
                                         "is vacuous")
        self._assert_no_leak(before, after, len(fired))

    def test_a_claim_lock_fdopen_fault_leaks_no_descriptor(self):
        # a claim recovery will try to LOCK, so the lock's fdopen is reached
        for i in range(20):
            q = _mint_claim(self.d, self.SID)
            with open(os.path.join(q, "%d.json" % i), "w") as fh:
                fh.write("{}")
            with open(os.path.join(q, todos.CLAIM_META), "w") as fh:
                json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                           "row": "%d.json" % i, "payload": "%d.json" % i}, fh)
        real, fired = os.fdopen, []

        def boom(fd, *a, **k):
            fired.append(fd)
            raise OSError("simulated fdopen failure")

        before = self._fds()
        with mock.patch.object(todos.os, "fdopen", boom):
            out = todos.recover_claims(self.SID)
        after = self._fds()
        self.assertTrue(fired, "the fdopen fault never fired — arm is vacuous")
        # THE RESULT IS CONSUMED, not discarded: recovery must have RETURNED
        # rather than propagating the fault, which is its stated contract, and
        # every claim must be left untouched.
        self.assertIsInstance(out, list)
        self.assertEqual(len([n for n in os.listdir(self.d)
                              if n.startswith(todos.CLAIM_PREFIX)]), 20,
                         "a claim was acted on despite an unlockable state")
        # AND THE SILENCE IS GONE. A review ruled this in scope: a claim whose
        # lock cannot be OPENED was skipped as quietly as one legitimately
        # HELD by a live sweep, because _claim_lock returned None for both.
        # Held is silence — expected, and none of our business. Unlockable is
        # UNRESOLVED, and an unresolved claim nobody mentions is the
        # silently-absent row this whole leg exists to prevent.
        self.assertEqual(len(out), 20,
                         "claims that could not be locked went unreported")
        self.assertTrue(all(o["outcome"] == "unreadable" for o in out), out)
        self.assertTrue(all("could not be locked" in o.get("why", "")
                            for o in out), out)
        self._assert_no_leak(before, after, len(fired))

    def test_WITHOUT_the_fault_both_paths_still_work(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control for the two leak arms: it requires a real removal to SUCCEED and a real recovery to DECIDE, so those arms measure a fault path rather than a module that refuses everything
        """UNCONDITIONAL POSITIVE CONTROL: unpatched, the same calls succeed —
        so the arms above measure a leak on the fault path rather than a
        module that stopped working."""
        full = os.path.join(self.d, "99.json")
        row = {"id": "99", todos.STAMP: "t99"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        self.assertIsNone(todos._trash(self.SID, full, row,
                                       {"removed": [], "failed": []}))
        self.assertFalse(os.path.exists(full))


class ACorruptLedgerREFUSESBeforeRecoveryMutatesTest(unittest.TestCase):
    """THE SEMANTIC CONFLICT THE TEXTUAL ONE HID.

    The rebase produced exactly one merge conflict, and resolving it correctly
    still left the two sides in the wrong ORDER: this lane's
    `recover_claims(apply=True)` ran ABOVE trunk's strict ledger snapshot and
    its refusal. Trunk's invariant is refuse-before-destructive-work — "no row
    was judged" — and recovery mutates: it restores rows, publishes undos and
    terminalises claims. A sweep that reported touching nothing had already
    changed the disk.

    Nothing about that is visible in a diff. Both hunks were individually
    right and their composition was not."""

    SID = "sess-corrupt"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-corrupt-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)
        self.ledger = os.path.join(self.tmp, "ledger.jsonl")

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recoverable_claim(self):
        """A crashed claim recovery WOULD restore — the row is gone from its
        own name and its bytes live only inside the claim."""
        q = _mint_claim(self.d, self.SID)
        payload = os.path.join(q, "1.json")
        with open(payload, "w") as fh:
            # A PARSEABLE STAMP. With "t1" the enumeration files the row
            # under `unparseable` and never asks the ledger about it, so the
            # twin's status could not matter and the ordering could not be
            # measured.
            json.dump({"id": "1", todos.STAMP: "task/424242",
                       "subject": "IN A CLAIM"}, fh)
        with open(os.path.join(q, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                       "row": "1.json", "payload": "1.json",
                       "identity": ["1", "task/424242"],
                       "payload_sha256": _digest_of(payload)}, fh)
        return q, payload

    def test_a_corrupt_ledger_refuses_before_recovery_can_touch_anything(self):  # noqa: VACUOUS_ASSERTION — the empty fired-list is the acceptance and it is paired with required PRESENCES in the same arm: rep['unavailable'] must be set (the refusal happened) and the payload's st_ino/st_mtime_ns must be unchanged. The unconditional positive control is the healthy-ledger sibling, which requires the SAME claim to be recovered and the row restored — mutation-proven: moving recovery back above the refusal kills this arm with [True] != [] and leaves the control green
        q, payload = self._recoverable_claim()
        with open(self.ledger, "w") as fh:
            fh.write('{"id": "t1", "status": "clo\n')     # truncated mid-row
        before = os.stat(payload)
        fired = []
        real = todos.recover_claims

        def note(*a, **k):
            fired.append(k.get("apply", True))
            return real(*a, **k)

        with mock.patch.object(todos, "recover_claims", note):
            rep = todos.demote(self.SID, "cj", path=self.ledger)
        self.assertTrue(rep.get("unavailable"),
                        "the corrupt ledger did not refuse: %r" % (rep,))
        self.assertEqual(fired, [],
                         "recovery RAN on a sweep that refused — the refusal "
                         "reported no row was judged while the disk changed")
        # nothing moved: the row is still only inside its claim
        self.assertFalse(os.path.exists(os.path.join(self.d, "1.json")),
                         "a refused sweep restored a row")
        after = os.stat(payload)
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))

    def test_a_HEALTHY_ledger_still_recovers(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control for the arm above: with a readable ledger the SAME claim must be recovered and the row restored, which a demote that never recovers cannot produce
        """UNCONDITIONAL POSITIVE CONTROL: with a readable ledger the same
        claim IS recovered — so the arm above measures the refusal's ordering
        rather than a demote that stopped recovering at all.

        THE TWIN MUST BE ONE THIS CALL ACTS ON (tests-only gap 1).
        It used to be `open` and cj-owned, so the restored row was merely
        KEPT — and "kept" is what happens whether recovery ran before
        enumeration or after it, so reversing the two lines still passed. A
        CLOSED twin makes the restored row ELIGIBLE FOR REMOVAL, which the
        enumeration can only do if it can SEE the row, which it can only do if
        recovery already put it back. Same call, or not at all."""
        q, payload = self._recoverable_claim()
        with open(self.ledger, "w") as fh:
            fh.write(json.dumps({"id": "task/424242", "status": "closed",
                                 "owner": "cj", "subject": "x"}) + "\n")
        rep = todos.demote(self.SID, "cj", path=self.ledger)
        self.assertFalse(rep.get("unavailable"), rep)
        self.assertTrue(rep.get("recovered"), "recovery never ran: %r" % (rep,))
        # THE RESTORED ROW WAS JUDGED BY THIS SAME SWEEP. Its twin is closed,
        # so a row the enumeration saw is a row it removed; if it is still
        # sitting at its path, the enumeration ran before recovery put it
        # there and the row waits for a sweep that may never come.
        self.assertTrue(rep.get("removed"),
                        "the recovered row was not judged by the same sweep "
                        "that recovered it: %r" % (rep,))
        self.assertIn("1", [str(x.get("id")) for x in rep["removed"]
                            if isinstance(x, dict)] or
                      [str(x) for x in rep["removed"]],
                      "something other than the recovered row was removed: %r"
                      % (rep.get("removed"),))
        self.assertFalse(os.path.exists(os.path.join(self.d, "1.json")),
                         "the restored row is still at its path, so the "
                         "enumeration never saw it — recovery ran too late")


class TheUNDOTopologyMatrixTest(unittest.TestCase):
    """The row 86 — WHICH ABSENCES PROVE ANYTHING.

    Only ONE does: an ENOENT on the MEMBER, beneath an undo directory we are
    already holding open. Every coarser absence — the trash root missing,
    moved, symlinked or unopenable; this claim's undo directory missing,
    moved, symlinked or unopenable — is equally consistent with the undo
    having been moved out from under us, and restoring on that evidence
    resurrects a completed removal.

    I had `troot_absent` authorising a restore on the coarsest of them. Fixing
    its symlink hole made the check honest about the NAME and left it
    answering the wrong QUESTION, which is the finding this matrix pins."""

    SID = "sess-topology"

    def setUp(self):
        # THE ORIGINAL ENVIRONMENT, CAPTURED ONCE (row 101, via the
        # cross-family refuter). test_the_matrix drives setUp/tearDown
        # re-entrantly to get a clean fixture per cell, and a re-entrant
        # capture stores THIS FIXTURE'S values as the "prior" — so the final
        # tearDown wrote a since-deleted tmpdir's paths into the process
        # environment instead of what the test found. Measured: every later
        # test in the same process then ran against a HELM_HOME that does not
        # exist.
        if not hasattr(self, "prior"):
            self.prior = {k: os.environ.get(k)
                          for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-topology-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)
        self.full = os.path.join(self.d, "1.json")
        self.row = {"id": "1", todos.STAMP: "t1", "subject": "REMOVED FOR REAL"}

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _completed_then_rewound(self):
        """A REAL removal, with its terminal mark rewound — the state a crash
        between publish and bookkeeping leaves, and the only state in which
        the undo question is asked at all."""
        with open(self.full, "w") as fh:
            json.dump(self.row, fh)
        rep = {"removed": [], "failed": []}
        self.assertIsNone(todos._trash(self.SID, self.full, self.row, rep))
        qdir = os.path.dirname(rep["residuals"][0]["payload"])
        meta_path = os.path.join(qdir, todos.CLAIM_META)
        meta = _manifest(meta_path)
        meta.pop("state", None)
        meta.pop("residual_why", None)
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        from helm import record
        troot = os.path.join(record.session_dir(self.SID), todos.TRASH)
        return qdir, troot, os.path.join(troot, os.path.basename(qdir))

    def _outcomes(self):
        return [o["outcome"] for o in todos.recover_claims(self.SID)]

    @contextlib.contextmanager
    def _case(self):
        """ONE SCOPED FIXTURE PER CELL — no re-entrant setUp/tearDown.

        The matrix used to drive self.setUp()/self.tearDown() by hand to get a
        clean tree per cell. That overwrites the instance attributes the
        FRAMEWORK's own lifecycle owns: `prior` ends up holding a fixture's
        values rather than the process's, and the final tearDown writes a
        since-deleted tmpdir into the environment, so every later test in the
        same process runs against a HELM_HOME that does not exist (row
        row 107; independently reproduced env_restored=False). A cell needs a
        scope, not a second lifecycle."""
        tmp = tempfile.mkdtemp(prefix="helm-topology-case-")
        prior = {k: os.environ.get(k)
                 for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(tmp, "helm")
        try:
            d = todos.personal_dir(self.SID)
            os.makedirs(d, exist_ok=True)
            yield d, os.path.join(d, "1.json")
        finally:
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shutil.rmtree(tmp, ignore_errors=True)

    def _build(self, full):
        """A REAL removal with its terminal mark rewound, inside a scope."""
        with open(full, "w") as fh:
            json.dump(self.row, fh)
        rep = {"removed": [], "failed": []}
        self.assertIsNone(todos._trash(self.SID, full, self.row, rep))
        qdir = os.path.dirname(rep["residuals"][0]["payload"])
        meta_path = os.path.join(qdir, todos.CLAIM_META)
        meta = _manifest(meta_path)
        meta.pop("state", None)
        meta.pop("residual_why", None)
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        from helm import record
        troot = os.path.join(record.session_dir(self.SID), todos.TRASH)
        return qdir, troot, os.path.join(troot, os.path.basename(qdir))

    @staticmethod
    def _seen(path):
        """Identity AND bytes, or an explicit absence — never just one."""
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError as e:
            return ("absent", e.errno)
        try:
            with open(path, "rb") as fh:
                return (st.st_dev, st.st_ino, fh.read())
        except OSError:
            return (st.st_dev, st.st_ino, "unreadable")

    def _observe(self, full, qdir, udir):
        return {"row": self._seen(full),
                "payload": self._seen(os.path.join(qdir, "1.json")),
                "undo": self._seen(os.path.join(udir, "1.json")),
                "manifest": self._seen(os.path.join(qdir, todos.CLAIM_META))}

    def test_the_matrix(self):  # noqa: VACUOUS_ASSERTION — the absence assertions are UNDECIDABLE outcomes plus before==after on the disk; their unconditional positive control is the member-enoent cell in the same run, which requires the row PRESENT at its original path with the exact inode the claim held and the recorded bytes. It sits inside a `with` scope, not a conditional — it runs on every execution of this test, and a recovery that answered undecidable to everything cannot produce it.
        """Every cell reports what recovery DID, not merely what it said.

        An outcome string is a claim about the disk; six cells recording only
        that string proved nothing about whether recovery had already acted
        before deciding it could not (row 107). Each cell is compared
        against the state AFTER its own mangle, so the mangle itself is never
        mistaken for something recovery did."""
        cases, moved = {}, {}

        def run(name, mangle):
            with self._case() as (_d, full):
                qdir, troot, udir = self._build(full)
                mangle(troot, udir)
                before = self._observe(full, qdir, udir)
                cases[name] = [o["outcome"]
                               for o in todos.recover_claims(self.SID)]
                moved[name] = (before, self._observe(full, qdir, udir))

        for name, mangle in (
            ("root-missing", lambda t, u: shutil.rmtree(t)),
            ("root-moved", lambda t, u: os.rename(t, t + ".moved")),
            ("root-symlink", lambda t, u: (os.rename(t, t + ".real"),
                                           os.symlink(t + ".real", t))),
            ("undodir-missing", lambda t, u: shutil.rmtree(u)),
            ("undodir-moved", lambda t, u: os.rename(u, u + ".moved")),
            ("undodir-symlink", lambda t, u: (os.rename(u, u + ".real"),
                                              os.symlink(u + ".real", u))),
        ):
            run(name, mangle)

        # --- UNOPENABLE: INJECTED, NEVER CHMOD-ED ---
        #
        # A chmod cell is conditional on not being root and on a filesystem
        # that honours the mode — under root it silently drops, and elsewhere
        # it can pass by testing nothing (row 107). The failure this
        # cell is about is "the open did not succeed", so the OPEN is what
        # gets failed, and a fired flag makes the injection must-fire rather
        # than hopeful.
        fired = {}

        def unopenable(target):
            """Fail EXACTLY the open this cell is about, and prove it fired.

            The scanner opens claim directories in the personal dir with the
            same helper, the same O_DIRECTORY and the same name prefix as the
            undo open beneath the trash root, so neither the name nor the
            flags can tell them apart — keying on either silences the whole
            scan and the cell reports nothing at all rather than
            `undecidable`. The DIRECTORY is the discriminator, so the trash
            root's own descriptor is captured as it is handed out and the
            failure is bound to it."""
            def mangle(_t, _u):
                fired[target] = []
                real_dir_fd, real_open = todos._dir_fd, todos._open_member
                troot_fds = []

                def note_or_fail(path):
                    if todos.TRASH in path:
                        if target == "root":
                            fired[target].append(path)
                            return None
                        fd = real_dir_fd(path)
                        troot_fds.append(fd)
                        return fd
                    return real_dir_fd(path)

                def fail_beneath_the_root(dir_fd, name, flags=None):
                    if dir_fd in troot_fds:
                        fired[target].append(name)
                        return None
                    return real_open(dir_fd, name, flags)

                self._patches = [
                    mock.patch.object(todos, "_dir_fd", note_or_fail),
                    mock.patch.object(todos, "_open_member",
                                      fail_beneath_the_root),
                ]
                for pt in self._patches:
                    pt.start()
            return mangle

        for target, name in (("root", "root-unopenable"),
                             ("undodir", "undodir-unopenable")):
            try:
                run(name, unopenable(target))
            finally:
                for pt in getattr(self, "_patches", []):
                    pt.stop()
            self.assertTrue(fired.get(target),
                            "%s never failed an open — the cell is vacuous"
                            % name)

        # --- THE ONLY ABSENCE THAT PROVES ANYTHING ---
        with self._case() as (_d, full):
            qdir, _t, udir = self._build(full)
            os.unlink(os.path.join(udir, "1.json"))
            held = os.stat(os.path.join(qdir, "1.json"))
            cases["member-enoent"] = [o["outcome"]
                                      for o in todos.recover_claims(self.SID)]
            # AN OUTCOME PREFIX IS NOT A RESTORE (row 101). Deleting
            # the publication while still reporting `restored` left this whole
            # class green, because nothing looked at the disk. The claim the
            # outcome makes is THE ROW IS BACK, so that is what is measured.
            self.assertTrue(os.path.exists(full),
                            "reported %r but the row is not at its path"
                            % (cases["member-enoent"],))
            back = os.stat(full)
            self.assertEqual((back.st_dev, back.st_ino),
                             (held.st_dev, held.st_ino),
                             "the row at the original path is not the inode "
                             "the claim held")
            with open(full) as fh:
                self.assertEqual(json.load(fh), self.row,
                                 "the restored bytes are not the recorded row")
            outcome = cases["member-enoent"][0]
            if outcome.endswith("-" + todos.RESIDUAL):
                self.assertTrue(os.path.exists(os.path.join(qdir, "1.json")),
                                "reported a RESIDUAL but kept no retained "
                                "payload")
            elif outcome == "restored":
                self.assertFalse(os.path.isdir(qdir),
                                 "reported a clean sweep but the claim is "
                                 "still there")

        undecidable = ("root-missing", "root-moved", "root-symlink",
                       "undodir-missing", "undodir-moved", "undodir-symlink",
                       "root-unopenable", "undodir-unopenable")
        for name in undecidable:
            self.assertEqual(cases[name], ["undecidable"],
                             "%s must be UNDECIDABLE, got %r"
                             % (name, cases[name]))
            # AND UNDECIDABLE MEANS NOTHING WAS TOUCHED. A recovery that
            # restored the row and THEN decided it could not proceed would
            # record the same string.
            before, after = moved[name]
            self.assertEqual(before, after,
                             "%s answered undecidable but changed the disk:\n"
                             "  before=%r\n  after =%r"
                             % (name, before, after))
            self.assertEqual(after["row"][0], "absent",
                             "%s left a row at the original path" % name)
        self.assertTrue(cases["member-enoent"][0].startswith("restored"),
                        "a sole missing MEMBER must authorise the restore, "
                        "got %r" % (cases["member-enoent"],))

    def test_an_INTACT_undo_still_decides_completed(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control for the matrix: with nothing mangled the same claim must reach a COMPLETED decision, which a recovery that answered undecidable to everything cannot produce
        """UNCONDITIONAL POSITIVE CONTROL: mangle nothing and the same claim
        decides completed — so the matrix measures each topology rather than a
        recovery that says undecidable to everything."""
        self._completed_then_rewound()
        self.assertTrue(self._outcomes()[0].startswith("completed"))


class ARenamedClaimIsNeverActedOnByItsOldNameTest(unittest.TestCase):
    """The (2)(3) — RENAME AND REUSE.

    Once a claim is anchored its NAME no longer identifies the inode we hold.
    Rename the pinned claim away, put a replacement at the old basename, and
    anything that still works by parent+basename acts on the REPLACEMENT:
    _discard_fresh_claim unlinked its members, _sweep_claim rmdir'd it, and
    the report named a path that held nothing while our bytes sat in the moved
    directory.

    POSIX gives no conditional rmdir-by-inode, so this cannot be fixed by
    checking harder — only by not deleting by name at all after the anchor."""

    SID = "sess-reuse"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-reuse-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_replacement_at_the_reused_basename_is_never_touched(self):
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        fired, real = [], todos._sweep_claim

        def swap_the_name_then_sweep(qdir, why, held, pdir_fd, qdir_fd):
            if not fired:
                fired.append(1)
                # THE PINNED CLAIM MUST BE EMPTY, or the delete-by-name branch
                # is unreachable and this arm proves nothing. My first version
                # left the payload in place: the sweep listed the pinned fd,
                # saw a member, returned RESIDUAL, and never reached the rmdir
                # — so the mutation restoring delete-by-name stayed GREEN and
                # the arm was testing the safe path while claiming the
                # dangerous one.
                for n in os.listdir(qdir_fd):
                    if n != todos.CLAIM_META:
                        os.unlink(n, dir_fd=qdir_fd)
                # now move the PINNED claim away and put a stranger's
                # directory at the basename this call still holds as a string
                os.rename(qdir, qdir + ".moved")
                # AN EMPTY REPLACEMENT, which is the only case that matters.
                # rmdir REFUSES a non-empty directory, so the kernel protects
                # a populated replacement for free — my first version put a
                # file inside and the mutation stayed green because the rmdir
                # simply failed. A review said exactly this: what the kernel
                # cannot refuse is a replacement that is ALSO empty. A fixture
                # that lets the kernel win is not testing our code.
                os.makedirs(qdir)
            return real(qdir, why, held, pdir_fd, qdir_fd)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_sweep_claim", swap_the_name_then_sweep):
            todos._trash(self.SID, full, row, rep)
        self.assertEqual(fired, [1], "the swap never armed — arm is vacuous")
        # THE REPLACEMENT DIRECTORY STILL EXISTS. Nothing we did may reach a
        # directory that merely inherited the name we used to hold.
        survivors = [n for n in os.listdir(self.d)
                     if n.startswith(todos.CLAIM_PREFIX)
                     and not n.endswith(".moved")]
        self.assertEqual(len(survivors), 1,
                         "a replacement claim was removed by name: %r"
                         % (sorted(os.listdir(self.d)),))
        # AND THE CLAIM WE ACTUALLY HELD SURVIVES ITS RENAME. The
        # assertion above only says the STRANGER was spared; it says nothing
        # about the directory our descriptors still point at. Its payload
        # cannot be the observable here — the fixture empties it on purpose to
        # make the rmdir reachable — so the directory itself is what must
        # still be there.
        moved = [n for n in os.listdir(self.d) if n.endswith(".moved")]
        self.assertEqual(len(moved), 1,
                         "the pinned claim did not survive being renamed: %r"
                         % (sorted(os.listdir(self.d)),))
        self.assertTrue(os.path.isdir(os.path.join(self.d, moved[0])))

    def test_a_report_never_names_a_path_the_claim_no_longer_owns(self):
        """The stale-report half: `preserved` was built by joining the claim's
        pathname to a member name, so a rename made it name a location holding
        nothing while the bytes sat in the moved directory."""
        q = _mint_claim(self.d, self.SID)
        payload = os.path.join(q, "1.json")
        with open(payload, "w") as fh:
            json.dump({"id": "1", todos.STAMP: "t1"}, fh)
        with open(os.path.join(q, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                       "row": "1.json", "payload": "1.json",
                       "identity": ["1", "t1"],
                       "payload_sha256": "0" * 64}, fh)   # -> inconsistent
        real_open = todos._open_member
        fired = []

        def move_the_claim_after_it_is_pinned(dir_fd, name, flags=None):
            fd = real_open(dir_fd, name, flags)
            if name == "1.json" and not fired:
                fired.append(1)
                os.rename(q, q + ".moved")     # the pinned fd still works
            return fd

        with mock.patch.object(todos, "_open_member",
                               move_the_claim_after_it_is_pinned):
            out = todos.recover_claims(self.SID)
        self.assertEqual(fired, [1], "the rename never armed — arm is vacuous")
        self.assertEqual([o["outcome"] for o in out], ["inconsistent"], out)
        got = out[0]
        # EITHER a path that really holds the bytes, OR an honest unknown —
        # never a precise path that does not.
        if got.get("preserved"):
            self.assertTrue(os.path.exists(got["preserved"]),
                            "the report named a path that holds nothing: %r"
                            % (got["preserved"],))
        else:
            self.assertTrue(got.get("preserved_unknown"),
                            "no path and no unknown marker: %r" % (got,))


class TheReportNamesTheHELDINODENotTheNameTest(unittest.TestCase):
    """Rows 99/101-103 — FOUR ROUNDS OF ONE FINDING.

    Every round cured a strictly weaker property than the report claims, and
    each weaker property sounded like the real one:

      round 1  nothing checked
      round 2  the PARENT is ours          -> "right directory"
      round 3  the MEMBER exists           -> "something is there"
      round 4  the member IS the held inode -> what the report actually says

    Rounds 2 and 3 both pass a SAME-DIRECTORY replacement: rename the member
    inside the very claim we hold and drop a stranger at its name. The parent
    is unchanged and something is at the name, so a precise, confident, FALSE
    path still reaches the operator.

    The same gap sits on the restore side with a different disguise. Both
    branches proved their identity BEFORE _sweep_claim; a rename between the
    proof and the terminalization installs a stranger carrying the SAME
    logical id and stamp, and the only downstream check is logical."""

    SID = "sess-heldinode"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-heldinode-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _claim_holding(self, subject="OURS", row_id="1", stamp="t1"):
        """A crashed claim whose payload is the row, manifest and all."""
        q = _mint_claim(self.d, self.SID)
        payload = os.path.join(q, "1.json")
        with open(payload, "w") as fh:
            json.dump({"id": row_id, todos.STAMP: stamp,
                       "subject": subject}, fh)
        with open(os.path.join(q, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                       "row": "1.json", "payload": "1.json",
                       "identity": [row_id, stamp],
                       "payload_sha256": _digest_of(payload)}, fh)
        return q, payload

    def test_a_same_directory_replacement_is_not_reported_as_our_bytes(self):
        """Parent ours, member present, WRONG OBJECT — rounds 2 and 3 both
        pass this and emit a precise false `preserved`."""
        q, payload = self._claim_holding()
        held = os.stat(payload)
        real = todos._verified_path
        seen = []

        def replace_inside_the_same_dir(path, pinned_fd, expect=None):
            if not seen and path.endswith("1.json"):
                seen.append(path)
                # rename OUR member away and install a stranger at its name,
                # inside the SAME pinned directory
                os.rename(path, path + ".elsewhere")
                with open(path, "w") as fh:
                    fh.write('{"id": "1", "stranger": true}')
            return real(path, pinned_fd, expect)

        with mock.patch.object(todos, "_verified_path",
                               replace_inside_the_same_dir):
            out = todos.recover_claims(self.SID, apply=False)
        self.assertEqual(len(seen), 1, "the replacement never armed")
        reported = [r.get("preserved") for r in out if r.get("preserved")]
        for path in reported:
            st = os.stat(path)
            self.assertEqual((st.st_dev, st.st_ino),
                             (held.st_dev, held.st_ino),
                             "the report names %s, which is NOT the inode the "
                             "claim holds — a stranger took that name inside "
                             "our own directory" % path)
        # and when it cannot stand behind a path it says so rather than
        # naming one
        if not reported:
            self.assertTrue(any(r.get("preserved_unknown") for r in out),
                            "the path was dropped without saying why: %r"
                            % (out,))

    def test_a_same_id_replacement_after_the_link_is_not_reported_restored(self):
        """The row 102 — the FRESH publish branch. Between _link_fd and
        the terminalization, a stranger with the same id and stamp takes the
        row's name. Logical identity matches, so only the inode can catch it."""
        q, payload = self._claim_holding()
        held = os.stat(payload)
        row_path = os.path.join(self.d, "1.json")
        real, fired = todos._sweep_claim, []

        def replace_between_link_and_sweep(qdir, why, h, pdir_fd, qdir_fd):
            if not fired:
                fired.append(1)
                self.assertTrue(os.path.exists(row_path),
                                "PROBE INVALID: the link never published")
                os.rename(row_path, row_path + ".moved-by-a-racer")
                with open(row_path, "w") as fh:      # SAME id, SAME stamp
                    json.dump({"id": "1", todos.STAMP: "t1",
                               "subject": "A STRANGER"}, fh)
            return real(qdir, why, h, pdir_fd, qdir_fd)

        with mock.patch.object(todos, "_sweep_claim",
                               replace_between_link_and_sweep):
            out = todos.recover_claims(self.SID, apply=True)
        self.assertEqual(fired, [1], "the replacement never armed")
        got = [r.get("outcome") for r in out]
        self.assertTrue(any(str(o).startswith("restored-replaced")
                            for o in got),
                        "a replaced row was reported as %r — the live path "
                        "names a stranger and the restored inode is elsewhere"
                        % (got,))
        # the report is a REPORT: the newcomer is untouched
        with open(row_path) as fh:
            self.assertEqual(json.load(fh).get("subject"), "A STRANGER",
                             "the replacement was destroyed; this leg reports, "
                             "it does not adjudicate")
        # and our bytes are still reachable by their own inode
        moved = os.stat(row_path + ".moved-by-a-racer")
        self.assertEqual((moved.st_dev, moved.st_ino),
                         (held.st_dev, held.st_ino))

    def test_the_already_restored_shortcut_is_bound_the_same_way(self):
        """The row 103 — curing only the fresh branch leaves this one
        live. The shortcut proves (payload_fd == pdir/name) BEFORE the sweep,
        so a replacement after that proof produces the same false report."""
        q, payload = self._claim_holding()
        row_path = os.path.join(self.d, "1.json")
        os.link(payload, row_path)          # the crash-after-restore state
        real, fired = todos._sweep_claim, []

        def replace_after_the_sameness_proof(qdir, why, h, pdir_fd, qdir_fd):
            if not fired:
                fired.append(1)
                os.rename(row_path, row_path + ".moved-by-a-racer")
                with open(row_path, "w") as fh:
                    json.dump({"id": "1", todos.STAMP: "t1",
                               "subject": "A STRANGER"}, fh)
            return real(qdir, why, h, pdir_fd, qdir_fd)

        with mock.patch.object(todos, "_sweep_claim",
                               replace_after_the_sameness_proof):
            out = todos.recover_claims(self.SID, apply=True)
        self.assertEqual(fired, [1], "the replacement never armed")
        got = [r.get("outcome") for r in out]
        self.assertTrue(any(str(o).startswith("restored-replaced")
                            for o in got),
                        "the already-restored shortcut reported %r — the same "
                        "stale-proof defect, one branch over" % (got,))


class AClosedDescriptorNUMBERIsReusedImmediatelyTest(unittest.TestCase):
    """The row 100 — TWO CLOSES OF ONE INTEGER IS NOT A TIDINESS BUG.

    `_recover_one_open` closed the payload descriptor on its nonregular
    branch, and `_recover_one`'s owner `finally` closed the same integer
    again. Between those two closes the reporting path runs, and an fd number
    freed by the first close is the LOWEST FREE NUMBER — so the next open in
    that window is handed it. The owner's second close then shuts a live,
    unrelated capability that merely inherited the number.

    A double close is usually harmless-looking (EBADF, swallowed). What makes
    it a real defect is the window: the number is not dead, it has a new
    owner."""

    SID = "sess-recycle"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-recycle-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_capability_taken_in_the_window_is_not_closed_by_the_owner(self):
        q = _mint_claim(self.d, self.SID)
        # A DIRECTORY AT THE PAYLOAD'S NAME: openable, so a descriptor exists
        # and can be double-closed, but NOT regular, so the nonregular branch
        # is the one taken. A symlink would fail the open outright and never
        # reach the window at all.
        os.mkdir(os.path.join(q, "1.json"))
        with open(os.path.join(q, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                       "row": "1.json", "payload": "1.json",
                       "identity": ["1", "t1"], "payload_sha256": ""}, fh)

        real, taken = todos._preserved_outcome, []
        sentinel = os.path.join(self.tmp, "an-unrelated-capability")
        with open(sentinel, "w") as fh:
            fh.write("x")

        def take_a_descriptor_in_the_window(*a, **kw):
            # runs BETWEEN the (removed) inner close and the owner's finally
            if not taken:
                taken.append(os.open(sentinel, os.O_RDONLY))
            return real(*a, **kw)

        with mock.patch.object(todos, "_preserved_outcome",
                               take_a_descriptor_in_the_window):
            out = todos.recover_claims(self.SID, apply=False)

        self.assertEqual(len(taken), 1,
                         "the nonregular branch never ran — arm is vacuous: "
                         "%r" % (out,))
        self.assertTrue(any(r.get("outcome") == "unreadable" for r in out),
                        "expected the nonregular payload to report unreadable,"
                        " got %r" % ([r.get("outcome") for r in out],))
        try:
            os.fstat(taken[0])
        except OSError as e:
            self.fail("the owner closed fd %d, which by then belonged to an "
                      "unrelated capability taken in the window (%s)"
                      % (taken[0], e))
        os.close(taken[0])


class TheHELDBYTESKeepANameEvenWhenTheirsIsTakenTest(unittest.TestCase):
    """The immutable refuter on 6b2a — REPORT TRUTH IS NOT ENOUGH.

    The same-directory arm moved the held member ASIDE before installing the
    stranger, so the fixture itself kept the judged inode alive at another
    name and the arm could only ever measure whether the report was honest.

    A rename-OVER takes the LAST name. The descriptor keeps the inode alive,
    so an honest "location unknown" is emitted — and then the owner's finally
    closes the descriptor and the bytes cease to exist. A true report about
    destroyed data is still destroyed data.

    The window in which the loss is discovered is the same window in which it
    can be undone, so the discovery has to happen there."""

    SID = "sess-lastname"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-lastname-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_rename_over_the_member_does_not_destroy_the_judged_bytes(self):
        q = _mint_claim(self.d, self.SID)
        payload = os.path.join(q, "1.json")
        ours = b'{"id": "1", "helm_row": "t1", "subject": "THE JUDGED BYTES"}'
        with open(payload, "wb") as fh:
            fh.write(ours)
        with open(os.path.join(q, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                       "row": "1.json", "payload": "1.json",
                       "identity": ["1", "t1"],
                       "payload_sha256": _digest_of(payload)}, fh)
        held = os.stat(payload)

        real, fired = todos._verified_path, []

        def rename_over_the_member(path, pinned_fd, expect=None):
            # NO `.elsewhere`. The stranger REPLACES the member, so after this
            # the only thing keeping our inode alive is the open descriptor.
            if not fired and path.endswith("1.json"):
                fired.append(path)
                stranger = path + ".stranger"
                with open(stranger, "wb") as fh:
                    fh.write(b'{"id": "1", "subject": "NOT OURS"}')
                os.rename(stranger, path)      # over the top: last name gone
                self.assertEqual(os.stat(path).st_ino != held.st_ino, True,
                                 "PROBE INVALID: the member was not replaced")
            return real(path, pinned_fd, expect)

        with mock.patch.object(todos, "_verified_path", rename_over_the_member):
            out = todos.recover_claims(self.SID, apply=False)
        self.assertEqual(len(fired), 1, "the rename-over never armed")

        # THE WHOLE POINT: after recovery returns and every descriptor is
        # closed, THE JUDGED BYTES must still be reachable under some name.
        #
        # Not necessarily the same INODE, and that is a measured platform
        # limit rather than a concession: linkat through /proc/self/fd/N
        # resolves that path, so once the last name is gone it dangles with
        # ENOENT, and re-linking the descriptor itself needs AT_EMPTY_PATH,
        # which this build does not expose. Measured, both branches. So the
        # rescue is a copy, the bytes survive, and the REPORT has to say which
        # it managed — an operator holding a retained path deserves to know
        # whether it is the object helm judged or a faithful reproduction.
        survivors, exact = [], []
        for root, _dirs, files in os.walk(self.tmp):
            for n in files:
                fp = os.path.join(root, n)
                try:
                    st = os.stat(fp, follow_symlinks=False)
                    with open(fp, "rb") as fh:
                        body = fh.read()
                except OSError:
                    continue
                if (st.st_dev, st.st_ino) == (held.st_dev, held.st_ino):
                    exact.append(fp)
                if body == ours:
                    survivors.append(fp)
        self.assertTrue(survivors,
                        "the judged bytes have no name left — recovery "
                        "reported %r and then the close destroyed the very "
                        "thing it was reporting about"
                        % ([r.get("outcome") for r in out],))
        rescued = [r for r in out if r.get("retained_rescued")]
        self.assertTrue(rescued,
                        "the bytes survived but no outcome says where: %r"
                        % (out,))
        r = rescued[0]
        self.assertIn(r["preserved"], survivors,
                      "the report names %r, which does not hold the judged "
                      "bytes" % (r.get("preserved"),))
        if r["retained_rescued"] == "copy":
            self.assertFalse(exact, "reported a COPY while the inode itself "
                                    "survived — the weaker claim was made")
            self.assertEqual(r.get("retained_sha256"), _digest_of(survivors[0]),
                             "a copy is stood behind by its digest or by "
                             "nothing")
        else:
            self.assertIn(r["preserved"], exact,
                          "reported the INODE but the named path is not it")
        # and the stranger who took the name still has it
        with open(os.path.join(q, "1.json"), "rb") as fh:
            self.assertEqual(json.load(fh).get("subject"), "NOT OURS",
                             "the rescue clobbered the newcomer")

    def test_a_retained_copy_does_not_require_os_pread(self):
        source = os.path.join(self.d, "source.json")
        body = b'{"id": "1", "subject": "PORTABLE COPY"}'
        with open(source, "wb") as fh:
            fh.write(body)
        payload_fd = os.open(source, os.O_RDONLY)
        qdir_fd = os.open(self.d, os.O_RDONLY | os.O_DIRECTORY)
        try:
            unavailable = OSError(errno.EXDEV, "different filesystem")
            with mock.patch.object(todos, "_link_fd",
                                   return_value=unavailable), \
                    mock.patch.object(fsops.os, "pread", None, create=True):
                kept, same, why = todos._retain_from_fd(
                    payload_fd, qdir_fd, "1.json")
        finally:
            os.close(qdir_fd)
            os.close(payload_fd)
        self.assertEqual((kept, same, why), ("1.json", False, None))
        with open(os.path.join(self.d, kept), "rb") as fh:
            self.assertEqual(fh.read(), body)

    def _claim_with(self, payload_bytes):
        q = _mint_claim(self.d, self.SID)
        payload = os.path.join(q, "1.json")
        with open(payload, "wb") as fh:
            fh.write(payload_bytes)
        with open(os.path.join(q, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                       "row": "1.json", "payload": "1.json",
                       "identity": ["1", "task/424242"],
                       "payload_sha256": _digest_of(payload)}, fh)
        return q, payload

    def _rename_over(self, fired):
        """Take the member's LAST name from inside the report door."""
        real = todos._verified_path

        def over(path, pinned_fd, expect=None):
            if not fired and path.endswith("1.json"):
                fired.append(path)
                s = path + ".s"
                with open(s, "wb") as fh:
                    fh.write(b'{"id": "1", "subject": "NOT OURS"}')
                os.rename(s, path)
            return real(path, pinned_fd, expect)
        return over

    def test_a_rescue_that_cannot_be_made_durable_says_so(self):
        """a retained location the disk has not committed to is a
        promise this process cannot keep across a crash, and "your bytes are
        safe at X" is the one sentence that must never be provisional."""
        q, payload = self._claim_with(b'{"id": "1", "helm_row": "t1"}')
        fired, synced = [], []
        real_fsync = os.fsync

        want = os.stat(q)
        qid = (want.st_dev, want.st_ino)

        def fail_the_claim_sync(fd):
            # THE CLAIM'S OWN DIRECTORY, PROVEN BY IDENTITY. This
            # failed the first directory sync after the arm fired and never
            # checked WHICH directory — so a mutation syncing the wrong one
            # would still have been caught by this arm, and a mutation
            # syncing the RIGHT one somewhere else would not have been.
            try:
                st = os.fstat(fd)
            except OSError:
                return real_fsync(fd)
            if fired and stat.S_ISDIR(st.st_mode) \
                    and (st.st_dev, st.st_ino) == qid:
                synced.append((st.st_dev, st.st_ino))
                raise OSError(errno.EIO, "simulated sync failure")
            return real_fsync(fd)

        with mock.patch.object(todos, "_verified_path",
                               self._rename_over(fired)):
            with mock.patch.object(os, "fsync", fail_the_claim_sync):
                out = todos.recover_claims(self.SID, apply=False)
        self.assertEqual(len(fired), 1, "the rename-over never armed")
        self.assertEqual(synced, [qid],
                         "the rescue did not sync THIS claim's directory; "
                         "syncs seen: %r, wanted %r" % (synced, qid))
        said = [r for r in out if r.get("retained_why")]
        self.assertTrue(said,
                        "a rescue that could not be synced reported no reason: "
                        "%r" % (out,))
        self.assertIn("durable", said[0]["retained_why"])
        # AND IT DID NOT CLAIM SUCCESS on the same outcome
        self.assertIsNone(said[0].get("preserved"),
                          "named a retained location it could not make durable")
        self.assertTrue(said[0].get("retained_partial"),
                        "left a partial member without naming it")

    def test_a_short_write_never_reports_a_verified_copy(self):  # noqa: VACUOUS_ASSERTION — the only absence assertion is `preserved is None` inside the partial loop, whose unconditional positive control is the assertTrue(named) above it on the same observable: this arm REQUIRES the rescue to name a retained location holding the full bytes, and mutation-proven — removing the write-all loop reddens it (with the weaker `named or partial` acceptance it SURVIVED)
        """os.write may write fewer bytes than it was given, and
        one call read as the whole block produced a TRUNCATED copy while the
        digest was taken from the ORIGINAL descriptor: a report that verified
        the wrong object."""
        body = b'{"id": "1", "helm_row": "t1", "subject": "%s"}' % (b"L" * 4000)
        q, payload = self._claim_with(body)
        fired, shorted = [], []
        real_write = os.write

        def one_byte_at_a_time(fd, data):
            if fired and len(data) > 1:
                shorted.append(len(data))
                return real_write(fd, data[:1])      # a legal short write
            return real_write(fd, data)

        with mock.patch.object(todos, "_verified_path",
                               self._rename_over(fired)):
            with mock.patch.object(os, "write", one_byte_at_a_time):
                out = todos.recover_claims(self.SID, apply=False)
        self.assertEqual(len(fired), 1, "the rename-over never armed")
        self.assertTrue(shorted, "no short write occurred — arm is vacuous")
        # UNCONDITIONAL: a retained location must be NAMED. Iterating a list
        # that can be empty is a guaranteed vacuous pass, and this arm is
        # about what the report SAYS, so an absent report is the failure it
        # would most easily hide.
        named = [r.get("preserved") for r in out if r.get("preserved")]
        partial = [r for r in out if r.get("retained_why")]
        # THE RESCUE MUST SUCCEED, not merely fail honestly. Accepting "it
        # reported a partial" let a mutation removing the write-all loop
        # SURVIVE, because the copy's own digest check — a different cure —
        # noticed the truncation and downgraded the report. Both behaviours
        # are correct; only one of them is what this arm is about.
        self.assertTrue(named,
                        "the rescue did not name a retained location; it "
                        "reported %r. A short write is a normal write, not a "
                        "failure to route around."
                        % ([r.get("retained_why") for r in partial] or out,))
        for path in named:
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), body,
                                 "reported %s as the retained bytes but it is "
                                 "truncated" % path)
        for r in partial:
            self.assertIsNone(r.get("preserved"),
                              "named a location while also reporting the "
                              "rescue failed: %r" % (r,))


class APayloadThatCannotBeOpenedNEVERTRACEBACKSTest(unittest.TestCase):
    """The second immutable refuter — THREE CALLERS, ONE UNBOUND NAME.

    `_put_back` reads `claimed` from its enclosing scope, and three of its
    call sites sit ABOVE the read loop that used to be that name's only
    assignment. A payload that opens but is not regular, or whose digest
    fails, therefore reached `out.write(claimed)` with the name unbound and
    raised UnboundLocalError — AFTER the row had already left its normal path,
    which is the worst moment for this function to stop running.

    And with no descriptor at all, nothing was read: there is no identity to
    restore and no bytes to reproduce, so the honest move is to refuse and
    say the location is unresolved rather than invent a third clever thing."""

    SID = "sess-noopen"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-noopen-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self):
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        return full, row, os.stat(full)

    def test_an_unopenable_payload_reports_instead_of_raising(self):
        full, row, held = self._row()
        real, fired = todos._open_member, []

        def refuse_the_payload(dir_fd, name, flags=None):
            # exactly the live payload open: the row's own basename, no flags
            if name == "1.json" and flags is None and not fired:
                fired.append(name)
                return None                 # EMFILE / unopenable / replaced
            return real(dir_fd, name, flags)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_open_member", refuse_the_payload):
            err = todos._trash(self.SID, full, row, rep)   # must not raise
        self.assertEqual(fired, ["1.json"], "the open never failed — vacuous")
        self.assertIsNotNone(err, "an unopenable payload reported success")
        self.assertIn("unresolved", err,
                      "the failure does not say the location is unresolved: "
                      "%r" % (err,))
        # NOTHING WAS FABRICATED at the row's path...
        if os.path.exists(full):
            st = os.stat(full)
            self.assertEqual((st.st_dev, st.st_ino), (held.st_dev, held.st_ino),
                             "something other than the original row was put "
                             "back at its path")
        # ...and the judged bytes are still reachable somewhere
        found = []
        for root, _dirs, files in os.walk(self.tmp):
            for n in files:
                fp = os.path.join(root, n)
                try:
                    st = os.stat(fp, follow_symlinks=False)
                except OSError:
                    continue
                if (st.st_dev, st.st_ino) == (held.st_dev, held.st_ino):
                    found.append(fp)
        self.assertTrue(found,
                        "the row's inode has no name left after a refusal "
                        "that claimed to leave the claim intact")



class APARTIALCapabilityGrabNeverCleansByNameTest(unittest.TestCase):
    """The second immutable refuter (3) — THE LAW WAS ALREADY WRITTEN.

    `_abandon`'s own docstring states it: once ANY descriptor exists the NAMES
    no longer identify the inodes we hold, so a name-based cleanup can only
    remove whatever occupies that name now. Twelve lines above it, the partial
    acquisition path did exactly that — closed the caps it had, then
    `_discard_fresh_claim(qdir)` and `os.rmdir(dest)` by name.

    Acquire qdir_fd, let a rename-and-reuse put an EMPTY replacement at that
    name, then fail dest_fd. rmdir refuses a non-empty directory, so the
    kernel covers the loud case for free and leaves precisely the quiet one:
    an empty stranger, silently removed."""

    SID = "sess-partialcap"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-partialcap-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_empty_replacement_survives_a_partial_acquisition(self):  # noqa: VACUOUS_ASSERTION — every assertion here is a PRESENCE: the swap fired, an error was returned, the stranger's directory still exists, and the row is still at its path. There is no absence assertion; the observable is os.path.isdir on the replacement, asserted unconditionally, and mutation-proven — restoring the by-name cleanup reddens it
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)

        # BOTH SEAMS, because B moved one of them. The claim directory is
        # acquired through _open_member on the pinned parent now, not by
        # _dir_fd on a pathname — so the swap has to happen where the claim is
        # actually opened, and the failure has to be injected where the UNDO
        # destination is actually opened. Leaving the swap on _dir_fd made it
        # never fire, and the arm reported the partial path was never taken
        # (which was true, and not what it is for).
        real_dir, real_member = todos._dir_fd, todos._open_member
        state = {"qdir": None, "qdir_id": None, "swapped": False,
                 "failed": None}

        def swap_the_claim(dir_fd, name, flags=None):
            fd = real_member(dir_fd, name, flags)
            if (fd is not None and not state["swapped"]
                    and name.startswith(todos.CLAIM_PREFIX)
                    and flags is not None):
                st = os.fstat(fd)
                state["qdir"] = os.path.join(self.d, name)
                state["qdir_id"] = (st.st_dev, st.st_ino)
                os.rename(state["qdir"], state["qdir"] + ".ours")
                os.mkdir(state["qdir"])       # an EMPTY stranger at our name
                state["swapped"] = True
            return fd

        def fail_the_dest(path):
            # the per-claim UNDO destination, and only that
            if state["swapped"] and todos.TRASH in path \
                    and os.path.basename(path).startswith(todos.CLAIM_PREFIX):
                state["failed"] = path
                return None
            return real_dir(path)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_open_member", swap_the_claim), \
                mock.patch.object(todos, "_dir_fd", fail_the_dest):
            err = todos._trash(self.SID, full, row, rep)

        self.assertTrue(state["swapped"], "the swap never armed — vacuous")
        self.assertTrue(state["failed"],
                        "no per-claim undo destination open failed, so the "
                        "partial-acquisition path was never taken")
        self.assertEqual(os.path.basename(state["failed"]),
                         os.path.basename(state["qdir"]),
                         "the failed open was not this claim's own undo "
                         "destination: %r" % (state["failed"],))
        self.assertIsNotNone(err, "a partial acquisition reported success")
        # THE CLAIM WE HELD IS STILL THE INODE WE PINNED, under its new name.
        moved = os.stat(state["qdir"] + ".ours")
        self.assertEqual((moved.st_dev, moved.st_ino), state["qdir_id"],
                         "the claim we acquired is not the one that survived")
        # THE STRANGER'S DIRECTORY IS STILL THERE.
        self.assertTrue(os.path.isdir(state["qdir"]),
                        "an empty replacement was removed by a name that was "
                        "no longer ours: %r" % (sorted(os.listdir(self.d)),))
        # and the row was never taken
        self.assertTrue(os.path.exists(full),
                        "the row left its path on a failed acquisition")


class TheCLAIMLOCKContractIsPINNEDNotDocumentedTest(unittest.TestCase):
    """The row 111 — A VALUE NO CALLER READS CANNOT BE TESTED.

    `_claim_lock` documented three return shapes and its sole consumer
    inspects `why` only when the handle is None, so the success label was
    unobservable: a mutation flipping "taken" to "held" left every control
    green. The cure was to delete the value rather than to write an arm that
    pins a label to nothing — success IS a non-None handle.

    What remains is a real contract with three distinguishable shapes, and
    this pins all three against each other so no two can collapse."""

    SID = "sess-lockshape"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-lockshape-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _claim(self):
        q = _mint_claim(self.d, self.SID)
        with open(os.path.join(q, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                       "row": "1.json", "payload": "1.json"}, fh)
        return q

    def test_the_three_shapes_are_distinguishable(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone calls are HALF of a shape assertion, each paired with an assertEqual on the reason in the same statement pair; the unconditional positive control is case (1) at the top, which requires a real handle and no label on the same call. Mutation-proven at BOTH unlockable sites: collapsing either into "held" reddens it
        q = self._claim()
        qfd = todos._dir_fd(q)
        self.assertIsNotNone(qfd, "fixture could not pin the claim")
        try:
            # (1) TAKEN — a handle, and NO reason
            handle, why = todos._claim_lock(qfd)
            self.assertIsNotNone(handle, "a free claim did not lock")
            self.assertIsNone(why, "success carries a label again — that value "
                                   "is unobservable and a mutation can flip it "
                                   "for free")

            # (2) HELD — the same claim, locked by a live holder
            held_handle, held_why = todos._claim_lock(qfd)
            self.assertIsNone(held_handle, "a locked claim handed out a "
                                           "second handle")
            self.assertEqual(held_why, "held")
            handle.close()
        finally:
            os.close(qfd)

        # (3) UNLOCKABLE — the metadata cannot be opened at all, and this must
        #     NOT read as "held": held is silence, unlockable is a report.
        q2 = self._claim()
        q2fd = todos._dir_fd(q2)
        try:
            os.unlink(os.path.join(q2, todos.CLAIM_META))
            os.symlink("nowhere", os.path.join(q2, todos.CLAIM_META))
            bad_handle, bad_why = todos._claim_lock(q2fd)
            self.assertIsNone(bad_handle)
            self.assertEqual(bad_why, "unlockable",
                             "a claim we could not even ask about reported "
                             "%r — collapsing it into `held` is how an "
                             "unresolved claim goes unmentioned" % (bad_why,))
        finally:
            os.close(q2fd)
        # and no claim.json was CREATED by asking (the probe must not
        # manufacture the condition it reports)
        self.assertFalse(os.path.isfile(os.path.join(q2, todos.CLAIM_META)),
                         "asking whether a claim was live minted its metadata")

        # (3b) THE SECOND UNLOCKABLE RETURN. `unlockable` is decided at TWO
        # sites — the open and the fdopen — and the case above only reaches
        # the first, so mutating the second stayed green while this arm
        # claimed to pin the contract. A predicate implemented at N sites
        # needs all N reached before a survival says anything about the test.
        q3 = self._claim()
        q3fd = todos._dir_fd(q3)
        real_fdopen, fired = os.fdopen, []
        try:
            def fail_the_wrap(fd, *a, **kw):
                if not fired:
                    fired.append(fd)
                    # NOT closed here: production's own except branch closes
                    # the raw fd on this path, and closing it in the injection
                    # made it exact-TWICE (EBADF) — the very defect this lane
                    # cured one function over.
                    raise OSError(errno.ENOMEM, "simulated fdopen failure")
                return real_fdopen(fd, *a, **kw)

            with mock.patch.object(os, "fdopen", fail_the_wrap):
                h3, why3 = todos._claim_lock(q3fd)
            self.assertTrue(fired, "the fdopen failure never armed")
            self.assertIsNone(h3)
            self.assertEqual(why3, "unlockable",
                             "the second unlockable site reported %r" % (why3,))
        finally:
            os.close(q3fd)


class EveryLIVEReportedPathNamesTheExpectedBytesTest(unittest.TestCase):
    """The second immutable refuter (2) — THE VERIFIER WAS ONLY ON ONE SIDE.

    Recovery got a report door; the LIVE removal path never did, and it runs
    every single time a row is removed. `payload` and `undo` were built by
    joining a directory pathname to a member name, so a rename-and-reuse of
    the claim or undo directory between the publish and the report sent the
    operator to a location holding someone else's file.

    Both are bound through the descriptors the transaction still holds. The
    committed rename/reuse arm moves qdir but only checks DIRECTORIES; this
    checks the emitted PATHS."""

    SID = "sess-livepaths"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-livepaths-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _remove_one(self, mangle=None):
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        held = os.stat(full)
        rep = {"removed": [], "failed": []}
        if mangle is None:
            self.assertIsNone(todos._trash(self.SID, full, row, rep))
        else:
            with mock.patch.object(todos, "_verified_path", mangle):
                self.assertIsNone(todos._trash(self.SID, full, row, rep))
        return rep, held

    def test_the_ordinary_removal_names_paths_that_hold_the_judged_bytes(self):
        """UNCONDITIONAL POSITIVE CONTROL, and the common case: with nothing
        mangled, every emitted location must exist and be the judged inode."""
        rep, held = self._remove_one()
        res = rep["residuals"][0]
        self.assertTrue(res.get("payload"),
                        "no payload location reported: %r" % (res,))
        self.assertTrue(res.get("undo"), "no undo location reported: %r" % (res,))
        for key in ("payload", "undo"):
            st = os.stat(res[key], follow_symlinks=False)
            self.assertEqual((st.st_dev, st.st_ino), (held.st_dev, held.st_ino),
                             "the reported %s does not name the judged inode"
                             % key)
        self.assertEqual(_undo_of(rep, "1.json"), res["undo"],
                         "two surfaces report different undo locations")

    def test_a_reused_claim_name_yields_unknown_not_a_strangers_path(self):
        """The claim directory is renamed away and a stranger takes both its
        name and its member. A joined string still names that stranger; a
        location verified through the held descriptor cannot."""
        real, moved = todos._verified_path, []

        def swap_the_claim(path, pinned_fd, expect=None):
            if not moved and todos.CLAIM_PREFIX in path \
                    and path.endswith("1.json"):
                moved.append(path)
                q = os.path.dirname(path)
                os.rename(q, q + ".ours")
                os.mkdir(q)
                with open(path, "w") as fh:      # a stranger at the same name
                    json.dump({"id": "1", "subject": "NOT OURS"}, fh)
            return real(path, pinned_fd, expect)

        rep, held = self._remove_one(swap_the_claim)
        self.assertEqual(len(moved), 1, "the swap never armed — vacuous")
        res = rep["residuals"][0]
        if res.get("payload") is not None:
            st = os.stat(res["payload"], follow_symlinks=False)
            self.assertEqual((st.st_dev, st.st_ino),
                             (held.st_dev, held.st_ino),
                             "reported %s, which is a STRANGER's file"
                             % res["payload"])
        else:
            self.assertTrue(res.get("payload_unknown"),
                            "dropped the payload location without saying so")
        # AND THE STRANGER IS UNTOUCHED — this leg reports, it never
        # adjudicates a name it no longer owns.
        with open(moved[0]) as fh:
            self.assertEqual(json.load(fh).get("subject"), "NOT OURS")

    def _replace_the_undo(self, holder):
        """Rename a stranger OVER the published undo, BEFORE the report opens
        it.

        THE INJECTION POINT IS THE WHOLE ARM. My first version swapped inside
        `_verified_path`, which runs AFTER `_open_member` — so the descriptor
        already held the real undo and the circular check compared the
        stranger against the REAL identity, mismatched, and answered unknown.
        The mutation therefore SURVIVED and the arm proved nothing.

        The defect being measured is "the expectation is derived from whatever
        is there NOW", so the stranger has to be there before the open."""
        real = todos._open_member

        def swap_then_open(dir_fd, name, flags=None):
            if not holder and flags is None:
                try:
                    where = os.readlink("/proc/self/fd/%d" % dir_fd)
                except OSError:
                    where = ""
                if todos.TRASH in where:
                    holder.append(os.path.join(where, name))
                    s = holder[0] + ".s"
                    with open(s, "w") as fh:
                        json.dump({"id": "1", "subject": "NOT THE UNDO"}, fh)
                    os.rename(s, holder[0])
            return real(dir_fd, name, flags)
        return swap_then_open

    def test_a_replaced_undo_is_never_reported_as_the_undo_same_fs(self):
        """the verifier must not derive its expectation from the
        thing it is checking. Opening dest/name and taking the identity from
        THAT descriptor always passes, so a stranger reports as the undo."""
        fired = []
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        held = os.stat(full)
        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_open_member",
                               self._replace_the_undo(fired)):
            self.assertIsNone(todos._trash(self.SID, full, row, rep))
        self.assertEqual(len(fired), 1, "the undo replacement never armed")
        res = rep["residuals"][0]
        if res.get("undo") is not None:
            st = os.stat(res["undo"], follow_symlinks=False)
            self.assertEqual((st.st_dev, st.st_ino),
                             (held.st_dev, held.st_ino),
                             "reported %s as the undo — a stranger took that "
                             "name after the publish" % res["undo"])
        else:
            self.assertTrue(res.get("undo_unknown"),
                            "dropped the undo location without saying so")

    def test_a_replaced_undo_is_never_reported_as_the_undo_cross_fs(self):  # noqa: VACUOUS_ASSERTION — the branch that reports undo_unknown is the ABSENCE half, and its unconditional positive control is the same-FS sibling above, which requires a reported undo naming the judged inode on the identical fixture shape. Both must-fire observables here are counts, and the cross-FS branch is required by assertFalse(same_inode) before any of it
        """Cross-filesystem the undo is a distinct COPY, so identity cannot be
        the expectation — it answers to the digest that was written, compared
        against the claim's own claimed_digest.

        SAME INJECTION POINT AS THE SIBLING, and I got this wrong once by
        fixing only the sibling: swapping inside `_verified_path` runs after
        `_open_member` has captured the real copy, so the digest matches and
        the arm measures nothing. The stranger goes in BEFORE the
        report opens the member."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        ours = _digest_of(full)
        fired, forced = [], []
        real_link = todos.os.link

        def exdev(src, dst, **kw):
            if todos.TRASH in str(dst) or kw.get("dst_dir_fd") is not None:
                forced.append(dst)
                raise OSError(errno.EXDEV, "simulated cross-filesystem undo")
            return real_link(src, dst, **kw)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos.os, "link", exdev):
            with mock.patch.object(todos, "_open_member",
                                   self._replace_the_undo(fired)):
                self.assertIsNone(todos._trash(self.SID, full, row, rep))
        self.assertGreaterEqual(len(forced), 1,
                                "EXDEV never fired — not the cross-FS path")
        self.assertEqual(len(fired), 1, "the undo replacement never armed")
        res = rep["residuals"][0]
        self.assertFalse(res.get("same_inode"), "this is not the cross-FS path")
        if res.get("undo") is not None:
            self.assertEqual(_digest_of(res["undo"]), ours,
                             "reported %s as the undo but its bytes are not "
                             "the ones this claim wrote" % res["undo"])
        else:
            self.assertTrue(res.get("undo_unknown"),
                            "dropped the undo location without saying so")

    def test_a_nonregular_member_never_fabricates_an_empty_row(self):  # noqa: VACUOUS_ASSERTION — the spy assertions (fired/linked) exist to prove the fallback is REACHABLE, which is the failure mode the first version of this arm had; the real observable is unconditional and on the disk — _trash must return an error AND the row's path must hold the original bytes or nothing, never an invented empty file. Mutation-proven: removing the `claimed is None` guard reddens it
        """The (A) — binding `claimed` to b"" cured an UnboundLocalError
        and bought a worse bug: a directory opens fine, fails _regular_fd,
        fails the fd-link, and the fallback WROTE b"" as a regular file at the
        operator's path while reporting that nothing was removed. Inventing
        data is worse than the loss it pretends to cover."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        # THE COMPOSITION IS THE CASE, and one half of it does not reach the
        # code under test: with only the nonregular verdict, _put_back's
        # fd-link SUCCEEDS (the row's own path is free after the claim rename)
        # and the fallback never runs — the mutation survived and the arm
        # measured nothing. A directory payload fails BOTH, which is exactly
        # the reported scenario.
        real_reg, real_link = todos._regular_fd, todos._link_fd
        fired, linked = [], []

        want = os.stat(full)
        rid = (want.st_dev, want.st_ino)

        def not_regular(fd):
            # BOUND TO THE ROW'S OWN INODE. Measured: _regular_fd is called
            # TWICE in a one-row sweep, so "the first call" is a coin toss
            # about which object this arm is really about — and a cure that
            # adds an earlier regularity check would silently retarget it.
            try:
                st = os.fstat(fd)
            except OSError:
                return real_reg(fd)
            if not fired and (st.st_dev, st.st_ino) == rid:
                fired.append((st.st_dev, st.st_ino))
                return False
            return real_reg(fd)

        def link_refuses(fd, dst_name, dst_dir_fd):
            if not linked:
                linked.append(dst_name)
                return OSError(errno.EPERM, "a directory cannot be linked")
            return real_link(fd, dst_name, dst_dir_fd)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_regular_fd", not_regular):
            with mock.patch.object(todos, "_link_fd", link_refuses):
                err = todos._trash(self.SID, full, row, rep)
        self.assertEqual(fired, [rid],
                         "the nonregular verdict did not fire on the ROW's "
                         "inode; saw %r, wanted %r" % (fired, rid))
        self.assertEqual(len(linked), 1,
                         "the put-back link failed %d time(s), not once — the "
                         "fallback is unreachable or reached twice, and this "
                         "arm proves neither" % len(linked))
        self.assertIsNotNone(err, "a nonregular payload reported success")
        # THE ROW'S PATH HOLDS EITHER THE ORIGINAL OR NOTHING — never an
        # invented empty file.
        if os.path.exists(full):
            with open(full, "rb") as fh:
                body = fh.read()
            self.assertNotEqual(body, b"",
                                "an EMPTY row was fabricated at %s while the "
                                "report said %r" % (full, err))
            self.assertEqual(json.loads(body.decode()).get("subject"), "OURS",
                             "something other than the original row is at its "
                             "path")

    def _double_race(self, full, row, extra=None):
        """The DOUBLE race: a writer takes the row's path AND the claimed
        object is replaced, which is the branch that copies bytes to the undo
        directory rather than linking anything."""
        real_link = todos._link_fd
        state = {"link": 0}

        def link_refuses(fd, dst_name, dst_dir_fd):
            # THE PUT-BACK LINK, NOT THE UNDO PUBLISH. Both target the row's
            # basename, so the NAME cannot tell them apart and refusing "the
            # first call" hit the undo publish instead — the arm then measured
            # "the undo path is already occupied", a different branch
            # entirely. The put-back is the one that writes into the ROW's own
            # directory.
            try:
                where = os.readlink("/proc/self/fd/%d" % dst_dir_fd)
            except OSError:
                where = ""
            is_put_back = (where == self.d)
            if is_put_back and not state["link"]:
                state["link"] += 1
                # BOTH HALVES, or the copy branch is unreachable. A writer
                # holds the row's path AND the claimed object is orphaned —
                # with only the first, `orphaned` is False and _put_back
                # returns "preserved at <quarantine>" without ever copying
                # anything, so the arm measured a branch it was not about.
                # Taking the payload's last name is what the second half IS.
                try:
                    where = os.readlink("/proc/self/fd/%d" % fd)
                    if os.path.exists(where):
                        os.unlink(where)
                        state["orphaned"] = where
                except OSError:
                    pass
                return FileExistsError(errno.EEXIST, "a writer holds it")
            return real_link(fd, dst_name, dst_dir_fd)

        # THE PAYLOAD STAYS REGULAR. Forcing _regular_fd False means nothing
        # is ever READ, so `claimed` is None and the copy branch correctly
        # refuses — the arm would then measure the refusal, not the copy. The
        # double race is a race, not a malformed payload.
        # A TRIGGER THAT STILL READS THE BYTES. _put_back only runs on a
        # failure, and the failure must happen AFTER the read loop or
        # `claimed` is None and the copy branch refuses (correctly) instead of
        # copying. An identity change under the sweep is exactly such a
        # failure: the payload was read, then judged to be a different row.
        real_json, seen = todos._json_fd, []

        def changed_under_us(fd):
            if not seen:
                seen.append(fd)
                return {"id": "999", todos.STAMP: "somebody-elses"}
            return real_json(fd)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_json_fd", changed_under_us), \
                mock.patch.object(todos, "_link_fd", link_refuses):
            if extra is None:
                err = todos._trash(self.SID, full, row, rep)
            else:
                with extra:
                    err = todos._trash(self.SID, full, row, rep)
        return err, rep, state

    def test_the_double_race_copy_is_never_reported_short(self):
        """The (2) — the same short-write/wrong-object gap already cured
        in _retain_from_fd lived in the double-race copy: one write call read
        as the whole buffer, checked only by INODE. Identity answers which
        OBJECT; only the bytes answer which CONTENTS."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "L" * 4000}
        with open(full, "w") as fh:
            json.dump(row, fh)
        ours = _digest_of(full)
        shorted = []
        real_fdopen = os.fdopen

        class ShortWriter:
            """A file object that writes ONE byte per call and says so.

            The C file type is immutable, so the short write is injected by
            wrapping the object `os.fdopen` hands back — which is also closer
            to the real thing: a short write is a normal, legal return value,
            not an error to route around."""

            def __init__(self, fh):
                self._fh = fh

            def write(self, data):
                if len(data) > 1:
                    shorted.append(len(data))
                    return self._fh.write(data[:1])
                return self._fh.write(data)

            def __getattr__(self, name):
                return getattr(self._fh, name)

            def __enter__(self):
                self._fh.__enter__()
                return self

            def __exit__(self, *a):
                return self._fh.__exit__(*a)

        def wrap(fd, *a, **kw):
            return ShortWriter(real_fdopen(fd, *a, **kw))

        err, rep, state = self._double_race(
            full, row, mock.patch.object(os, "fdopen", wrap))
        self.assertTrue(state["link"], "the double race never armed")
        self.assertTrue(state.get("orphaned"),
                        "the claimed object was never orphaned, so the copy "
                        "branch is unreachable and this arm proves nothing")
        self.assertTrue(shorted, "no short write occurred — arm is vacuous")
        self.assertIsNotNone(err, "the double race reported success")
        # UNCONDITIONAL: the judged bytes must exist SOMEWHERE at full length.
        # Iterating the paths a message happens to mention is vacuous when it
        # mentions none, and "mentions none" is one of the outcomes this arm
        # has to be able to tell apart.
        intact = []
        for root, _dirs, files in os.walk(self.tmp):
            for n in files:
                fp = os.path.join(root, n)
                if _digest_of(fp) == ours:
                    intact.append(fp)
        self.assertTrue(intact,
                        "no full-length copy of the judged bytes exists "
                        "anywhere after a short-written double race; err=%r"
                        % (err,))
        # AND THE COPY MUST SUCCEED, not merely fail honestly. Accepting the
        # "came back short" refusal let a mutation removing the write-all loop
        # SURVIVE, because the read-back check — a different cure — noticed the
        # truncation and declined to name anything. Both behaviours are
        # correct; only one is what this arm measures. A short write is a
        # normal write.
        self.assertIn("preserved at", str(err),
                      "the double race did not preserve the judged bytes at "
                      "the undo; it said %r. A short write is a legal return "
                      "value, not a condition to give up on." % (err,))
        # AND every path this refusal names must be one of them.
        for token in str(err).split():
            path = token.rstrip(".,;")
            if path.startswith("/") and os.path.isfile(path):
                self.assertIn(path, intact,
                              "the refusal names %s as the preserved bytes "
                              "but it is truncated" % path)

    def test_an_unrecorded_refusal_never_names_a_reused_claim(self):
        """The (3) — the UNRECORDED return said "will be seen again at
        <qdir>" with a raw join, so a rename-and-reuse pointed the operator at
        a stranger's directory.

        This branch is NOT on the double-race path (my first fixture aimed
        there and never armed): it needs a refusal whose put-back SUCCEEDS and
        whose sweep then records no decision."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        moved, real_say = [], todos._say_where

        def swap_then_say(path, pinned_fd, expect, claim_id):
            if not moved and todos.CLAIM_PREFIX in os.path.basename(path):
                moved.append(path)
                os.rename(path, path + ".ours")
                os.mkdir(path)            # an empty stranger at our name
            return real_say(path, pinned_fd, expect, claim_id)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_sha256_fd", lambda fd: None):
            with mock.patch.object(todos, "_sweep_claim",
                                   lambda *a, **k: todos.UNRECORDED):
                with mock.patch.object(todos, "_say_where", swap_then_say):
                    err = todos._trash(self.SID, full, row, rep)

        self.assertIsNotNone(err, "the refusal reported success")
        self.assertIn("seen again", str(err),
                      "not the UNRECORDED branch — arm is aimed wrong: %r"
                      % (err,))
        self.assertEqual(len(moved), 1,
                         "the claim reuse fired %d time(s), not once — a "
                         "second firing means the arm is measuring a "
                         "different call than it names" % len(moved))
        self.assertNotIn(moved[0], str(err),
                         "the refusal names %s, which is now a STRANGER's "
                         "directory: %r" % (moved[0], err))
        self.assertIn("unverified location", str(err),
                      "the refusal dropped the path without saying why: %r"
                      % (err,))
        # and the row came back
        self.assertTrue(os.path.exists(full),
                        "the row was not put back by the refusal")

    def test_the_cross_fs_undo_copy_is_never_written_short(self):  # noqa: VACUOUS_ASSERTION — the only absence assertion is assertIsNone(err), which here means the removal SUCCEEDED; it is paired with unconditional presences on the same observable: an undo location must be reported and its digest must equal the row's. Mutation-proven: reverting the write-all reddens it
        """The the third short-write site. The undo is the ONLY backup of
        a row about to leave its path: a truncated undo that the removal then
        proceeded on means the row went away and what stood in for it was not
        it."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "t1", "subject": "L" * 4000}
        with open(full, "w") as fh:
            json.dump(row, fh)
        ours = _digest_of(full)
        forced, shorted = [], []
        real_link, real_fdopen = todos.os.link, os.fdopen

        def exdev(src, dst, **kw):
            if todos.TRASH in str(dst) or kw.get("dst_dir_fd") is not None:
                forced.append(dst)
                raise OSError(errno.EXDEV, "simulated cross-filesystem undo")
            return real_link(src, dst, **kw)

        class ShortWriter:
            def __init__(self, fh):
                self._fh = fh

            def write(self, data):
                if len(data) > 1:
                    shorted.append(len(data))
                    return self._fh.write(data[:1])
                return self._fh.write(data)

            def __getattr__(self, name):
                return getattr(self._fh, name)

            def __enter__(self):
                self._fh.__enter__()
                return self

            def __exit__(self, *a):
                return self._fh.__exit__(*a)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos.os, "link", exdev), \
                mock.patch.object(os, "fdopen",
                                  lambda fd, *a, **k:
                                  ShortWriter(real_fdopen(fd, *a, **k))):
            err = todos._trash(self.SID, full, row, rep)
        self.assertTrue(forced, "EXDEV never fired — not the cross-FS path")
        self.assertTrue(shorted, "no short write occurred — arm is vacuous")
        # THE REMOVAL MUST SUCCEED, with a complete undo. A short write is a
        # legal return value, not a condition to give up on.
        self.assertIsNone(err, "the cross-FS removal refused instead of "
                               "writing the whole undo: %r" % (err,))
        undo = _undo_of(rep, "1.json")
        self.assertTrue(undo, "no undo location was reported: %r" % (rep,))
        self.assertEqual(_digest_of(undo), ours,
                         "the undo at %s is not the row it backs up" % undo)


class TheManifestSurvivesACrashMidTRANSITIONTest(unittest.TestCase):
    """The architecture result C — TRUNCATE HAS NO SAFE MOMENT.

    Every manifest transition used to seek(0) + truncate() + rewrite, so a
    crash between the truncate and the fsync left the claim's ONLY record
    empty or half-written. A claim whose manifest cannot be parsed is
    invisible to every surface while its payload sits beside it, which is the
    silently-absent row this whole leg exists to prevent — reached, this time,
    through the record rather than the bytes.

    No ordering of truncate-then-write avoids it: there is a window in which
    the correct answer is unrepresentable. Append-only moves the commit to a
    SINGLE BYTE (the frame's closing LF), and a byte either landed or it did
    not — which is the granularity a crash actually operates at."""

    SID = "sess-manifest"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-manifest-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _framed(*snapshots):
        out = b""
        for s in snapshots:
            out += todos.RS + json.dumps(s, separators=(",", ":")).encode() \
                + b"\n"
        return out

    def test_EVERY_prefix_reads_as_the_old_state_or_the_new_one(self):
        """THE FORMAT'S CENTRAL CLAIM, proven exhaustively rather than at a
        few hand-picked cutpoints: a crash can stop the write at ANY byte, so
        every byte is a cutpoint."""
        old = {"claim_id": "c", "sid": self.SID, "state": "old"}
        new = {"claim_id": "c", "sid": self.SID, "state": "new"}
        # THE BASELINE IS ALREADY DURABLE.
        # Slicing the whole file from byte zero and allowing None proves a
        # WEAKER thing: it permits "the claim has no record", which is exactly
        # the state truncate produced and this format exists to make
        # impossible. The real invariant starts from a complete baseline on
        # disk and cuts only the NEXT append — every prefix of it must read as
        # the OLD state or the NEW one, and never as nothing.
        baseline = self._framed(old)
        tail = self._framed(new)
        seen = set()
        for i in range(len(tail) + 1):
            frames = todos._frames(baseline + tail[:i])
            self.assertTrue(frames,
                            "a %d-byte cut of the next append left the claim "
                            "with NO record at all — the baseline was lost"
                            % i)
            latest = frames[-1]
            self.assertIn(latest, (old, new),
                          "a %d-byte cut read as %r — neither the durable old "
                          "state nor the new one" % (i, latest))
            seen.add(latest["state"])
        # BOTH PHASES ACTUALLY REACHED, so this is not N copies of one case.
        self.assertEqual(seen, {"old", "new"},
                         "the cuts never covered both phases: %r" % (seen,))
        # AND THE COMMIT IS THE LAST BYTE: one byte short is still the old
        # state, which is what makes the closing LF the commit point rather
        # than a formatting detail.
        self.assertEqual(todos._frames(baseline + tail[:-1])[-1], old)
        self.assertEqual(todos._frames(baseline + tail)[-1], new)

    def test_a_TORN_tail_is_ignored_but_a_COMMITTED_one_fails_closed(self):
        """These are different facts and must not collapse. A torn tail is an
        interrupted append — the crash this format survives. A frame that HAS
        its closing byte and still does not parse is committed corruption, and
        skipping it would silently promote a SUPERSEDED state over a newer
        one, which is worse than refusing."""
        good = {"claim_id": "c", "sid": self.SID, "state": "old"}
        torn = self._framed(good) + todos.RS + b'{"state": "hal'
        self.assertEqual(todos._frames(torn), [good],
                         "a torn tail was not ignored")
        committed = self._framed(good) + todos.RS + b'{"state": "hal\n'
        with self.assertRaises(todos.ManifestCorrupt):
            todos._frames(committed)

    def test_a_RETRY_after_a_torn_frame_is_read_not_stopped_at(self):
        """The caught on the mutable diff. A retry appends a FRESH frame
        past a torn one, so `old + torn + complete` is a normal file — and
        stopping at the torn frame would serve the SUPERSEDED state while a
        complete newer one sat right behind it. An incomplete frame is only a
        TAIL if nothing follows it."""
        old_s = {"claim_id": "c", "sid": self.SID, "state": "old"}
        new_s = {"claim_id": "c", "sid": self.SID, "state": "new"}
        blob = (self._framed(old_s) + todos.RS + b'{"state": "interrupt'
                + self._framed(new_s))
        self.assertEqual(todos._frames(blob), [old_s, new_s],
                         "the reader stopped at a torn frame a retry had "
                         "already written past")
        # and the tail case is unchanged: a torn LAST frame is still ignored
        self.assertEqual(
            todos._frames(self._framed(old_s) + todos.RS + b'{"st'),
            [old_s], "a torn TAIL was not ignored")

    def test_a_LEGACY_manifest_is_still_readable(self):
        """A claim written before this format is a bare JSON object with no
        framing. Orphaning those would strand exactly the crashed claims this
        code exists to recover."""
        legacy = {"claim_id": "c", "sid": self.SID, "row": "1.json"}
        self.assertEqual(todos._frames(json.dumps(legacy).encode()), [legacy])
        # and a legacy file that has since been APPENDED to reads as the newer
        newer = dict(legacy, state=todos.RESIDUAL)
        blob = json.dumps(legacy).encode() + self._framed(newer)
        self.assertEqual(todos._frames(blob)[-1], newer)

    def test_a_REAL_transition_never_destroys_the_previous_record(self):
        """The end-to-end claim: run a real removal, then make the terminal
        transition's fsync fail mid-flight, and the claim must still be
        readable — as ONE of its two states, never as nothing."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "task/424242", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        rep = {"removed": [], "failed": []}
        self.assertIsNone(todos._trash(self.SID, full, row, rep))
        q = os.path.dirname(rep["residuals"][0]["payload"]
                            or os.path.join(self.d, "x"))
        meta = os.path.join(q, todos.CLAIM_META)
        blob = open(meta, "rb").read()
        # EVERY TRANSITION IS STILL THERE. A normal removal makes three: the
        # initial record, the digest-bound one, and the terminal residual.
        # Counting RS is too weak — the manifest is opened O_APPEND, so even a
        # reintroduced truncate leaves a LATER frame behind and the count
        # survives. What a truncate destroys is the EARLIER states, so those
        # are what get asserted.
        frames = todos._frames(blob)
        self.assertEqual(len(frames), 3,
                         "a real removal left %d frame(s), not the three "
                         "transitions it makes — an earlier record was "
                         "destroyed rather than superseded: %r"
                         % (len(frames), frames))
        self.assertEqual(frames[0].get("payload_sha256"), "",
                         "the FIRST record (written before the payload was "
                         "read) is gone")
        self.assertTrue(frames[1].get("payload_sha256"),
                        "the digest-bound record is gone")
        self.assertIsNone(frames[1].get("state"),
                          "the digest-bound record was written as terminal")
        self.assertEqual(frames[2].get("state"), todos.RESIDUAL,
                         "the terminal record is not the latest")
        self.assertEqual(frames[2].get("payload_sha256"),
                         frames[1].get("payload_sha256"),
                         "the terminal snapshot lost the digest — it is a "
                         "delta, not a full snapshot")
        # ...and every prefix of it is readable as some complete state.
        for i in range(len(blob) + 1):
            try:
                todos._frames(blob[:i])
            except todos.ManifestCorrupt:
                self.fail("a real manifest has a prefix at %d bytes that "
                          "reads as committed corruption" % i)

    def _claim_with_manifest(self):
        """A real claim, so the manifest under test is production's own."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "task/424242", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        rep = {"removed": [], "failed": []}
        self.assertIsNone(todos._trash(self.SID, full, row, rep))
        q = os.path.dirname(rep["residuals"][0]["payload"])
        return q, os.path.join(q, todos.CLAIM_META)

    def test_a_NON_DICT_completed_frame_fails_closed(self):  # noqa: VACUOUS_ASSERTION — every assertion is a REQUIRED RAISE, which is a presence not an absence: four junk payloads and a junk legacy prefix must each raise ManifestCorrupt. Its unconditional positive control is the sibling prefix matrix, which requires well-formed frames to parse on the same reader. Mutation-proven: dropping the dict check reddens it
        """A scalar or a list parses fine and is not a snapshot. Accepting one
        would let `null` or `[]` read as the claim's state."""
        good = {"claim_id": "c", "sid": self.SID}
        for junk in (b"null", b"[]", b'"a string"', b"7"):
            blob = self._framed(good) + todos.RS + junk + b"\n"
            with self.assertRaises(todos.ManifestCorrupt,
                                   msg="%r was accepted as a record" % junk):
                todos._frames(blob)
        # and the legacy prefix is held to the same rule
        with self.assertRaises(todos.ManifestCorrupt):
            todos._frames(b"[1, 2, 3]")

    def test_EVERY_tail_prefix_over_a_LEGACY_baseline_reads_as_one_state(self):
        """The same invariant, with the baseline in the OLD shape — which is
        the state every claim on disk is in when this format first ships, and
        therefore the one that must not lose records."""
        legacy = {"claim_id": "c", "sid": self.SID, "row": "1.json"}
        new = dict(legacy, state=todos.RESIDUAL)
        baseline = json.dumps(legacy).encode()
        tail = self._framed(new)
        seen = set()
        for i in range(len(tail) + 1):
            frames = todos._frames(baseline + tail[:i])
            self.assertTrue(frames,
                            "a %d-byte cut of the first append onto a LEGACY "
                            "manifest left no record at all" % i)
            self.assertIn(frames[-1], (legacy, new),
                          "a %d-byte cut read as %r" % (i, frames[-1]))
            seen.add(frames[-1].get("state"))
        self.assertEqual(seen, {None, todos.RESIDUAL},
                         "the cuts never covered both phases: %r" % (seen,))

    def test_a_FAULT_mid_append_leaves_the_previous_record_intact(self):  # noqa: VACUOUS_ASSERTION — the acceptance is an EQUALITY against a required non-empty baseline (assertTrue(before) runs unconditionally before the fault), so it cannot pass with nothing there; the fired-list is the must-fire observable, not the acceptance
        """ACTUALLY INJECTED, not reasoned about. A BaseException part-way
        through the write is the crash this format exists for, and the arm
        that claimed to cover it injected nothing at all."""
        q, meta = self._claim_with_manifest()
        before = todos._frames(open(meta, "rb").read())
        self.assertTrue(before, "fixture: no baseline record")

        fired = []

        class DiesPartway:
            """A PROXY, not an attribute assignment. `fh.write = ...` on a raw
            _io.FileIO is generally read-only and is not a reliable injection
            seam — it can silently not take, which would make this
            arm pass by never firing. The proxy forwards the real descriptor,
            so production still writes to the real file."""

            def __init__(self, fh):
                self._fh = fh

            def write(self, data):
                if not fired:
                    fired.append(len(data))
                    self._fh.write(data[:len(data) // 2])   # a torn frame
                    raise KeyboardInterrupt("crash mid-append")
                return self._fh.write(data)

            def __getattr__(self, name):
                return getattr(self._fh, name)

        fds_before = _fd_targets()
        qfd = todos._dir_fd(q)
        self.assertIsNotNone(qfd, "fixture could not pin the claim")
        try:
            with open(meta, "rb+", buffering=0) as fh:
                with self.assertRaises(KeyboardInterrupt):
                    todos._append_manifest(DiesPartway(fh),
                                           {"claim_id": "x", "sid": self.SID},
                                           qfd)
        finally:
            os.close(qfd)
        self.assertTrue(fired, "the fault never fired — arm is vacuous")
        after = todos._frames(open(meta, "rb").read())
        self.assertEqual(after, before,
                         "a crash mid-append changed the readable record: "
                         "%r -> %r" % (before, after))
        _assert_no_leak(self, fds_before, _fd_targets(), 1)

    def test_a_RETRY_after_a_real_torn_append_lands(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is the helper's SUCCESS contract, paired in the same arm with a required presence: the retried snapshot must be the readable latest state after a real torn append
        """The recovery half of the same story: after a torn append, the next
        one must be readable — which is the resynchronisation property, proven
        through the real helper rather than on a hand-built blob."""
        q, meta = self._claim_with_manifest()
        with open(meta, "ab", buffering=0) as fh:
            fh.write(todos.RS + b'{"claim_id": "torn')      # no LF: torn
        wanted = {"claim_id": "after-the-tear", "sid": self.SID}
        fds_before = _fd_targets()
        qfd = todos._dir_fd(q)
        try:
            with open(meta, "rb+", buffering=0) as fh:
                self.assertIsNone(todos._append_manifest(fh, wanted, qfd),
                                  "the retry reported a failure")
        finally:
            os.close(qfd)          # OURS to close; an inline open has no owner
        _assert_no_leak(self, fds_before, _fd_targets(), 1)
        self.assertEqual(todos._frames(open(meta, "rb").read())[-1], wanted,
                         "the retry did not become the readable state")

    def test_death_AT_the_fsync_still_leaves_a_complete_state(self):  # noqa: VACUOUS_ASSERTION — every assertion is a required PRESENCE on the disk: the fault must have fired, the manifest must still parse to a non-empty chain, and its latest frame must be one of the two known states. There is no absence assertion here
        """the frame is COMPLETE and the fsync is what dies. The
        bytes may or may not have reached the platter, so the reader must find
        the old state or the new one — and never nothing, because the baseline
        was already durable."""
        q, meta = self._claim_with_manifest()
        before = todos._frames(open(meta, "rb").read())
        self.assertTrue(before, "fixture: no baseline record")
        wanted = {"claim_id": "at-the-fsync", "sid": self.SID}
        fired, real_fsync = [], os.fsync
        fds_before = _fd_targets()
        qfd = todos._dir_fd(q)

        want = os.stat(meta)
        mid = (want.st_dev, want.st_ino)

        def die_at_fsync(fd):
            # THE MANIFEST'S OWN INODE, not "the first fsync of anything"
            # (the third recurrence of this exact shape).
            # An injection bound to ORDINALITY passes whichever call happens
            # to come first, so a reordering that syncs something else — or a
            # cure that adds a sync earlier — silently retargets the arm.
            try:
                st = os.fstat(fd)
            except OSError:
                return real_fsync(fd)
            if not fired and (st.st_dev, st.st_ino) == mid:
                fired.append((st.st_dev, st.st_ino))
                raise OSError(errno.EIO, "power lost at the fsync")
            return real_fsync(fd)

        try:
            with open(meta, "rb+", buffering=0) as fh:
                with mock.patch.object(os, "fsync", die_at_fsync):
                    why = todos._append_manifest(fh, wanted, qfd)
        finally:
            os.close(qfd)
        self.assertEqual(fired, [mid],
                         "the fault did not fire on the MANIFEST's inode; "
                         "saw %r, wanted %r" % (fired, mid))
        self.assertIsNotNone(why, "an unsynced append reported success")
        frames = todos._frames(open(meta, "rb").read())
        self.assertTrue(frames,
                        "the claim has NO readable record after a death at "
                        "the fsync — the durable baseline was lost")
        self.assertIn(frames[-1], (before[-1], wanted),
                      "the readable state is neither the durable old one nor "
                      "the new one: %r" % (frames[-1],))
        _assert_no_leak(self, fds_before, _fd_targets(), 1)

    def test_a_SYNCED_append_survives_close_and_reopen(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is the helper's SUCCESS contract, and the acceptance is an unconditional PRESENCE on a fresh descriptor: the appended snapshot must be the latest frame a brand-new reader sees. Nothing here asserts an absence
        """The other half: when the fsync SUCCEEDS the record must be there
        for a reader that opens the file fresh — not merely visible through
        the handle that wrote it."""
        q, meta = self._claim_with_manifest()
        wanted = {"claim_id": "durable", "sid": self.SID}
        qfd = todos._dir_fd(q)
        try:
            with open(meta, "rb+", buffering=0) as fh:
                self.assertIsNone(todos._append_manifest(fh, wanted, qfd))
        finally:
            os.close(qfd)
        with open(meta, "rb") as fresh:          # a NEW descriptor entirely
            self.assertEqual(todos._frames(fresh.read())[-1], wanted,
                             "a synced append is not visible to a reader that "
                             "opened the file fresh")

    def test_the_manifest_lock_and_the_record_are_ONE_inode(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone/assertEqual pair is a required SHAPE (a second LOCK_NB must be refused and say "held"), and it is paired with two unconditional presences on the same observable: the held handle's inode must equal the member's, and the first handle must still hold a real lock. A lock on a different object would satisfy neither
        """A lock on one object and a record on another is decoration. The
        held descriptor's inode must BE the member at CLAIM_META, and a second
        LOCK_NB must be refused while the first is held."""
        q, _meta = self._claim_with_manifest()
        qfd = todos._dir_fd(q)
        self.assertIsNotNone(qfd)
        try:
            handle, why = todos._claim_lock(qfd)
            self.assertIsNotNone(handle, "a free claim did not lock: %r" % why)
            try:
                mine = os.fstat(handle.fileno())
                there = os.stat(todos.CLAIM_META, dir_fd=qfd,
                                follow_symlinks=False)
                self.assertEqual((mine.st_dev, mine.st_ino),
                                 (there.st_dev, there.st_ino),
                                 "the locked object is not the manifest at "
                                 "its own name")
                # THE LOCK IS REAL: a second non-blocking attempt is refused.
                second, why2 = todos._claim_lock(qfd)
                self.assertIsNone(second, "a locked claim handed out a second "
                                          "handle")
                self.assertEqual(why2, "held")
                # AND THE RECORD READS THROUGH THE LOCKED HANDLE.
                self.assertIsInstance(todos._read_manifest(handle), dict,
                                      "the locked handle cannot read the "
                                      "record it guards")
            finally:
                handle.close()
        finally:
            os.close(qfd)

    def test_a_HOSTILE_preexisting_manifest_is_never_truncated(self):  # noqa: VACUOUS_ASSERTION — the acceptance is an unconditional EQUALITY on the hostile bytes plus a required error return; the planted-list is the must-fire observable. Mutation-proven with codex-3's exact O_TRUNC mutation
        """The C4. The creator opens O_EXCL and deliberately not O_TRUNC,
        and nothing pinned that: switching it to O_TRUNC after the emptiness
        check left every test green while hostile bytes at claim.json were
        destroyed and the removal proceeded.

        A freshly minted claim cannot already hold a manifest, so one that
        does is somebody else's — and truncating it is the one move that makes
        the situation unrecoverable."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "task/424241", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        theirs = b'{"somebody": "else was here"}'
        # PLANTED AFTER THE EMPTINESS CHECK ACCEPTS, which is the only moment
        # that reaches the creator. Planting earlier makes _adopted_shape
        # refuse a non-empty directory and the arm then measures THAT check —
        # my first version did exactly this and an O_TRUNC mutation survived
        # it, which is what review C4 says.
        planted, real = [], todos._adopted_shape

        def plant_after_the_check(dfd):
            why = real(dfd)
            if why is None and not planted:
                planted.append(dfd)
                fd = os.open(todos.CLAIM_META,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                             dir_fd=dfd)
                try:
                    os.write(fd, theirs)
                finally:
                    os.close(fd)
            return why

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_adopted_shape", plant_after_the_check):
            err = todos._trash(self.SID, full, row, rep)
        self.assertEqual(len(planted), 1, "the plant never armed — vacuous")
        self.assertIsNotNone(err, "the removal proceeded over a manifest it "
                                  "did not write")
        # THE HOSTILE BYTES ARE EXACTLY AS THEY WERE, wherever the claim is.
        found = []
        for root, _dirs, files in os.walk(self.d):
            if todos.CLAIM_META in files:
                with open(os.path.join(root, todos.CLAIM_META), "rb") as fh:
                    found.append(fh.read())
        self.assertTrue(found, "no manifest survived at all")
        self.assertIn(theirs, found,
                      "a manifest this transaction did not write was "
                      "truncated or overwritten: %r" % (found,))
        self.assertTrue(os.path.exists(full),
                        "the row was removed despite the refusal")

    def test_the_TRANSITION_binds_the_held_inode_to_its_own_name(self):
        """The C5. The final held-inode-versus-named-member check exists
        and nothing pinned it: removing it left the suite green, while a
        manifest renamed away mid-transition reported the record durable and
        recovery then opened the REPLACEMENT.

        A record made durable on an inode nobody can reach by name is a
        decision written where no reader will look."""
        q, meta = self._claim_with_manifest()
        qfd = todos._dir_fd(q)
        self.assertIsNotNone(qfd)
        moved = []
        try:
            with open(meta, "rb+", buffering=0) as fh:
                real_fsync, held = os.fsync, os.fstat(fh.fileno())

                def rename_away(fd):
                    # between the record's own sync and the name check
                    if not moved:
                        try:
                            st = os.fstat(fd)
                        except OSError:
                            return real_fsync(fd)
                        if (st.st_dev, st.st_ino) == (held.st_dev,
                                                      held.st_ino):
                            moved.append(1)
                            os.rename(meta, meta + ".moved-away")
                            with open(meta, "wb") as other:
                                other.write(b"A REPLACEMENT\n")
                    return real_fsync(fd)

                with mock.patch.object(os, "fsync", rename_away):
                    why = todos._append_manifest(
                        fh, {"claim_id": "x", "sid": self.SID}, qfd)
        finally:
            os.close(qfd)
        self.assertTrue(moved, "the rename never armed — vacuous")
        self.assertIsNotNone(why,
                             "a transition whose record is no longer at its "
                             "own name was reported durable")
        self.assertIn("no longer the one at its name", why)

    def test_the_BODY_is_durable_BEFORE_the_commit_byte_is_written(self):
        """The C1, and it corrects the format's central claim.

        The closing LF is what makes a frame complete, so a crash must read as
        the old state or the new one. Writing RS+JSON+LF in ONE call and
        syncing once does NOT establish that: nothing orders the persistence
        of bytes within a single write, so the commit byte can reach the
        platter while the body behind it has not — and the reader then sees a
        COMPLETE frame whose contents are partly whatever was in that block
        before. The writer would be manufacturing the very
        committed-corruption case the reader fails closed on.

        Two writes, two syncs, commit byte last and alone."""
        q, meta = self._claim_with_manifest()
        wrote, real_write, real_fsync = [], None, os.fsync
        qfd = todos._dir_fd(q)
        try:
            with open(meta, "rb+", buffering=0) as fh:
                held = os.fstat(fh.fileno())
                real_write = fh.write

                class Watched:
                    def __init__(self, inner):
                        self._fh = inner

                    def write(self, data):
                        wrote.append(("write", bytes(data)))
                        return self._fh.write(data)

                    def __getattr__(self, name):
                        return getattr(self._fh, name)

                def note_fsync(fd):
                    try:
                        st = os.fstat(fd)
                        if (st.st_dev, st.st_ino) == (held.st_dev, held.st_ino):
                            wrote.append(("fsync", b""))
                    except OSError:
                        pass
                    return real_fsync(fd)

                with mock.patch.object(os, "fsync", note_fsync):
                    self.assertIsNone(todos._append_manifest(
                        Watched(fh), {"claim_id": "c1", "sid": self.SID}, qfd))
        finally:
            os.close(qfd)

        kinds = [k for k, _ in wrote]
        self.assertGreaterEqual(kinds.count("fsync"), 2,
                                "the manifest was synced %d time(s); the body "
                                "and the commit byte each need one, or the "
                                "commit byte is not a commit: %r"
                                % (kinds.count("fsync"), wrote))
        # THE COMMIT BYTE IS WRITTEN ALONE, AFTER A SYNC.
        commit = [i for i, (k, d) in enumerate(wrote)
                  if k == "write" and d == todos.FRAME_END]
        self.assertTrue(commit,
                        "the commit byte was never written on its own — it "
                        "went out with the body: %r" % (wrote,))
        first_commit = commit[0]
        self.assertIn("fsync", kinds[:first_commit],
                      "the commit byte was written before the body was made "
                      "durable: %r" % (wrote,))
        self.assertIn("fsync", kinds[first_commit:],
                      "the commit byte itself was never made durable: %r"
                      % (wrote,))
        # AND THE BODY CARRIED NO COMMIT BYTE OF ITS OWN.
        body = [d for k, d in wrote[:first_commit] if k == "write"]
        self.assertTrue(body, "no body was written at all")
        self.assertNotIn(todos.FRAME_END, b"".join(body),
                         "the body write already contained the commit byte, "
                         "so the two-phase order is decorative")

    def test_a_COMPLETE_frame_followed_by_GARBAGE_fails_closed(self):
        """The C2. A final chunk holding an LF but not ending with one is
        a COMPLETE frame followed by something else — and ignoring it as a
        torn tail served the SUPERSEDED state while a committed newer one sat
        right there in the file. A torn tail contains no commit byte at all."""
        old_s = {"claim_id": "c", "sid": self.SID, "state": "old"}
        new_s = {"claim_id": "c", "sid": self.SID, "state": "new"}
        blob = self._framed(old_s, new_s) + b"trailing matter"
        with self.assertRaises(todos.ManifestCorrupt):
            todos._frames(blob)
        # A GENUINE TORN TAIL — no commit byte anywhere in it — is still just
        # ignored, which is the case the format exists to survive.
        self.assertEqual(
            todos._frames(self._framed(old_s) + todos.RS + b'{"st'),
            [old_s], "a real torn tail stopped being ignored")

    def test_a_HARD_LINKED_manifest_is_refused_before_it_is_written(self):
        """The finding 15. A claim.json hard-linked from an external
        valid manifest (nlink=2) was accepted by public recovery, which then
        appended residual state onto the SHARED inode and changed bytes
        belonging to whatever else held that link.

        No symlink was involved and no preflight was bypassed: the record was
        simply not private, and nothing checked. O_EXCL creates a file with
        ONE link; anything else is somebody's shared file."""
        q = _mint_claim(self.d, self.SID)
        outside = os.path.join(self.tmp, "SOMEBODY-ELSES.json")
        with open(outside, "w") as fh:
            json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                       "row": "1.json", "payload": "1.json"}, fh)
        before = open(outside, "rb").read()
        meta = os.path.join(q, todos.CLAIM_META)
        if os.path.exists(meta):
            os.unlink(meta)
        os.link(outside, meta)                 # nlink == 2, shared
        self.assertEqual(os.stat(meta).st_nlink, 2, "fixture: not linked")

        out = todos.recover_claims(self.SID, apply=True)
        # THE EXTERNAL BYTES ARE UNCHANGED.
        self.assertEqual(open(outside, "rb").read(), before,
                         "recovery wrote onto a shared inode and changed "
                         "bytes outside this transaction")
        self.assertTrue(out, "recovery reported nothing at all")
        self.assertTrue(all(o.get("outcome") == "unreadable" for o in out),
                        "a shared manifest was acted on rather than refused: "
                        "%r" % ([o.get("outcome") for o in out],))

    def test_a_REPLACED_lock_member_does_not_serialize_the_claim(self):  # noqa: VACUOUS_ASSERTION — the self.fail is reached only when a handle IS issued, which is a presence; the unconditional positive control is the first _claim_lock at the top, which must return a real handle before anything is replaced. Mutation-proven: the arm reproduced the defect against an earlier cure
        """The finding 14. flock serializes access to the INODE it was
        taken on, and the claim's identity is the DIRECTORY — so replacing
        claim.json with a byte-copy at the same name hands a second actor a
        different inode to lock, and both proceed believing they hold the
        claim.

        Verifying the binding after a write is too late: by then the other
        actor has already mutated. The handle must not be issued at all when
        the binding does not hold."""
        q = _mint_claim(self.d, self.SID)
        meta = os.path.join(q, todos.CLAIM_META)
        with open(meta, "w") as fh:
            json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                       "row": "1.json", "payload": "1.json"}, fh)
        qfd = todos._dir_fd(q)
        self.assertIsNotNone(qfd)
        try:
            first, why = todos._claim_lock(qfd)
            self.assertIsNotNone(first, "fixture could not lock: %r" % (why,))
            try:
                # REPLACE THE MEMBER with a byte-identical distinct inode
                body = open(meta, "rb").read()
                os.rename(meta, meta + ".aside")
                with open(meta, "wb") as fh:
                    fh.write(body)
                self.assertNotEqual(os.stat(meta).st_ino,
                                    os.fstat(first.fileno()).st_ino,
                                    "fixture: the replacement must be a "
                                    "different inode")
                # A SECOND ACTOR OPENS ITS OWN DESCRIPTOR, which is what
                # real concurrency looks like — reusing the first actor's fd
                # models nothing, because flock is idempotent on one
                # descriptor and the arm would pass without serializing.
                other = todos._dir_fd(q)
                self.assertIsNotNone(other)
                try:
                    second, why2 = todos._claim_lock(other)
                finally:
                    os.close(other)
                if second is not None:
                    second.close()
                    self.fail("a second actor was handed a claim handle while "
                              "the first still holds one — the flock does not "
                              "serialize the claim once the member is "
                              "replaceable")
                # "HELD" IS THE RIGHT ANSWER, and a better one than the
                # binding check would have given: the claim DIRECTORY is
                # locked by the first actor, so the second is refused because
                # somebody owns this claim — not because a record looked
                # wrong. That distinction is the whole point of moving the
                # lock off a replaceable member and onto the identity.
                self.assertEqual(why2, "held",
                                 "the second actor was refused for the wrong "
                                 "reason: %r" % (why2,))
                # AND THE FIRST ACTOR'S OWN WRITES NOW REFUSE, before touching
                # anything, rather than discovering it afterwards.
                self.assertIsNotNone(
                    todos._append_manifest(first, {"claim_id": "x",
                                                   "sid": self.SID}, qfd),
                    "the original holder wrote onto a claim it no longer "
                    "binds")
            finally:
                first.close()
        finally:
            os.close(qfd)

    def test_CLOSING_the_handle_RELEASES_the_claim(self):  # noqa: VACUOUS_ASSERTION — the self.fail is reached only when a handle IS issued, a presence; the unconditional positive control is the first acquire at the top, which must return a real handle before anything else runs, and the acceptance is assertIsNotNone on the RE-acquire after close. Mutation-proven: reducing _ClaimHandle.close to self._fh.close() reddens it
        """The (3), and it was my own unpinned finding — a lock that
        serializes but never releases is a deadlock, and moving the lock onto
        the claim DIRECTORY made that my responsibility.

        Their mutation: reduce _ClaimHandle.close to self._fh.close() only.
        The three existing lock-owner tests stayed green (3/3) because every
        one of them acquires ONCE — nothing asked whether a released claim can
        be taken again, which is the half that makes exclusion usable rather
        than terminal."""
        q, _meta = self._claim_with_manifest()
        first_fd = todos._dir_fd(q)
        second_fd = todos._dir_fd(q)
        self.assertIsNotNone(first_fd)
        self.assertIsNotNone(second_fd)
        try:
            first, why = todos._claim_lock(first_fd)
            self.assertIsNotNone(first, "fixture could not lock: %r" % (why,))
            # WHILE HELD: a second actor with its OWN descriptor is refused.
            blocked, why2 = todos._claim_lock(second_fd)
            if blocked is not None:
                blocked.close()
                self.fail("two actors hold the same claim at once")
            self.assertEqual(why2, "held")
            first.close()
            # AFTER THE CLOSE: the claim is available again. This is the
            # assertion the mutation kills.
            again, why3 = todos._claim_lock(second_fd)
            self.assertIsNotNone(
                again,
                "the claim was not released when its handle closed (%r) — "
                "exclusion that never ends is a deadlock, not a lock" % (why3,))
            again.close()
            # AND THE ORIGINAL DESCRIPTOR CAN TAKE IT TOO, so the release is a
            # property of the claim rather than of one descriptor.
            third, why4 = todos._claim_lock(first_fd)
            self.assertIsNotNone(third, "the original holder cannot re-take a "
                                        "released claim: %r" % (why4,))
            third.close()
        finally:
            os.close(first_fd)
            os.close(second_fd)

    def test_privacy_is_bound_ACROSS_the_write_not_sampled_before_it(self):
        """The item 3. _private_record checked nlink == 1 and then wrote;
        a link created at the first write() landed the append on an inode that
        was now shared, and the call reported SUCCESS while an external file's
        bytes changed.

        Nothing can PREVENT a hard link — anyone with access can make one at
        any instant — so the property is not exclusivity, it is HONESTY: if
        the record stopped being private at any point across the write, this
        transition did not happen on private storage and must not be reported
        as though it did."""
        q, meta = self._claim_with_manifest()
        outside = os.path.join(self.tmp, "SOMEBODY-ELSES-LINK")
        qfd = todos._dir_fd(q)
        self.assertIsNotNone(qfd)
        linked = []
        try:
            with open(meta, "rb+", buffering=0) as fh:

                class LinksAtTheFirstWrite:
                    def __init__(self, inner):
                        self._fh = inner

                    def write(self, data):
                        if not linked:
                            linked.append(1)
                            os.link(meta, outside)   # AFTER the check
                        return self._fh.write(data)

                    def __getattr__(self, name):
                        return getattr(self._fh, name)

                why = todos._append_manifest(
                    LinksAtTheFirstWrite(fh),
                    {"claim_id": "shared", "sid": self.SID}, qfd)
        finally:
            os.close(qfd)
        self.assertEqual(len(linked), 1, "the link never armed — vacuous")
        self.assertEqual(os.stat(meta).st_nlink, 2,
                         "fixture: the manifest must have become shared")
        self.assertIsNotNone(
            why, "an append onto an inode that became shared mid-write "
                 "reported SUCCESS")
        self.assertIn("private", why)

    def test_LIVE_and_RECOVERY_cannot_own_one_claim_at_once(self):
        """The item 2 — split ownership.

        Recovery locked the claim DIRECTORY; the live removal locked only its
        own claim.json inode. Two different owner identities for one logical
        claim, so: rename the manifest aside mid-transaction, install a
        byte-identical replacement, and recovery takes the directory and the
        REPLACEMENT manifest while live carries on under the old inode. Both
        believe they hold the claim; reproduced, recovery restores while live
        publishes.

        A member can always be replaced. The directory cannot be, without the
        pinned parent changing — so the directory is the identity, and both
        paths take THAT lock or the exclusion means nothing."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "task/424241", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        seen, real = [], todos._sweep_claim

        def become_a_second_owner(qdir, why, held, pdir_fd, qdir_fd):
            meta = os.path.join(qdir, todos.CLAIM_META)
            with open(meta, "rb") as fh:
                body = fh.read()
            os.rename(meta, meta + ".aside")
            with open(meta, "wb") as fh:          # byte-identical replacement
                fh.write(body)
            other = todos._dir_fd(qdir)
            try:
                handle, why2 = todos._claim_lock(other)
                seen.append(why2 if handle is None else "GOT A HANDLE")
                if handle is not None:
                    handle.close()
            finally:
                os.close(other)
            return real(qdir, why, held, pdir_fd, qdir_fd)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_sweep_claim", become_a_second_owner):
            err = todos._trash(self.SID, full, row, rep)
        # THE CALL RESULT IS CONSUMED, not discarded: the removal must have
        # SUCCEEDED, so the arm measures a live transaction that really held
        # the claim rather than one that refused early for another reason.
        self.assertIsNone(err, "the removal refused, so nothing was holding "
                               "the claim while the second owner tried: %r"
                               % (err,))
        self.assertFalse(os.path.exists(full),
                         "the row is still at its path, so the transaction "
                         "never reached the span this arm is about")
        self.assertEqual(seen, ["held"],
                         "a second actor became an owner of a claim a live "
                         "transaction is holding: %r" % (seen,))

    def test_the_MANIFEST_FLAGS_are_pinned_exactly(self):
        """The item 13. Removing O_APPEND from manifest creation left the
        hostile-manifest, body/LF ordering, transition-history and
        close/reopen arms all green — because every one of them writes through
        a single handle whose offset happens to be at the end.

        O_APPEND is what makes 'append' true regardless of where any reader
        left the offset. Without it, a reader that seeks to parse frames and a
        writer sharing that open file description can place the next frame ON
        TOP of an earlier durable one. The absence of O_TRUNC matters for the
        same reason it did in item C4: a manifest that unexpectedly exists is
        somebody else's.

        Flags are a contract, and a contract nothing asserts is a comment."""
        seen = {}
        real = todos._open_member

        def note(dir_fd, name, flags=None):
            if name == todos.CLAIM_META and flags is not None:
                seen["create"] = flags
            return real(dir_fd, name, flags)

        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "task/424241", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        with mock.patch.object(todos, "_open_member", note):
            self.assertIsNone(todos._trash(self.SID, full, row,
                                           {"removed": [], "failed": []}))
        self.assertIn("create", seen,
                      "the manifest was never created through _open_member — "
                      "this arm is watching the wrong door")
        flags = seen["create"]
        for want in ("O_RDWR", "O_CREAT", "O_EXCL", "O_APPEND"):
            self.assertTrue(flags & getattr(os, want),
                            "the manifest creator dropped %s; every frame "
                            "guarantee downstream assumes it" % want)
        self.assertFalse(flags & getattr(os, "O_TRUNC", 0),
                         "the creator gained O_TRUNC, which destroys a "
                         "manifest this transaction did not write")

    def test_the_FRAME_BYTES_are_pinned_as_literals(self):  # noqa: VACUOUS_ASSERTION — every assertion is an equality against a raw byte literal, which is the opposite of an absence: the two assertNotIn calls forbid whitespace separators and are paired with the exact-bytes equality above them
        """The C6. Every manifest fixture builds its frames from todos.RS,
        so changing RS from 0x1e to anything else keeps all twelve of them
        green — while every claim already on disk becomes unreadable, because
        the bytes in those files do not change when the constant does.

        A wire format's literals are the one thing that cannot be expressed in
        terms of itself. Pinned here as raw bytes, and the helper's output is
        pinned against a hand-built expectation rather than against RS."""
        self.assertEqual(todos.RS, b"\x1e",
                         "the record separator changed; every manifest "
                         "already on disk is now unreadable")
        self.assertEqual(todos.FRAME_END, b"\n",
                         "the commit byte changed; every manifest already on "
                         "disk is now unreadable")
        # AND THE FRAME THE WRITER PRODUCES IS THOSE BYTES — built by hand,
        # not by reusing the constants under test.
        snap = {"a": 1}
        blob = self._framed(snap)
        self.assertEqual(blob, b"\x1e" + b'{"a":1}' + b"\n",
                         "the framing is not RS + compact JSON + LF: %r"
                         % (blob,))
        self.assertEqual(todos._frames(b"\x1e" + b'{"a":1}' + b"\n"), [snap],
                         "the reader does not accept the literal framing")
        # compact separators, because a space between members is a byte on
        # disk and a different frame
        self.assertNotIn(b", ", blob)
        self.assertNotIn(b": ", blob)


class ONEPersonalDirectoryForTheWholeSweepTest(unittest.TestCase):
    """The capability result A — THREE RESOLUTIONS IS THREE WINDOWS.

    Recovery, enumeration and every removal each resolved personal_dir(sid)
    for themselves. Rename the session directory between any two and a LATER
    phase acts on a different inode than the one that judged: recovery
    restores into the old directory while the enumeration reads a
    replacement, and the removal refuses rows it was just handed.

    The exposure GREW with the row count, which is the tell that it was a
    per-call resolution rather than a per-sweep capability."""

    SID = "sess-onepdir"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-onepdir-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)
        self.ledger = os.path.join(self.tmp, "ledger.jsonl")

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self, n):
        with open(self.ledger, "w") as fh:
            for i in range(1, n + 1):
                with open(os.path.join(self.d, "%d.json" % i), "w") as rf:
                    json.dump({"id": str(i), todos.STAMP: "task/42424%d" % i,
                               "subject": "ROW %d" % i}, rf)
                fh.write(json.dumps({"id": "task/42424%d" % i,
                                     "status": "closed", "owner": "cj"}) + "\n")

    def _opens(self):
        """Every personal-directory descriptor handed out during one sweep."""
        seen, real = [], todos._dir_fd
        # KEYED ON THE NAME ASKED FOR, not on where it currently resolves
        # Filtering by realpath(path) == target stops matching the
        # moment the directory is swapped — so a mutant that RE-OPENS the
        # personal directory after a rename would go uncounted and the arm
        # would report "one capability" while the code took two.
        want = os.path.abspath(self.d)

        def note(path):
            fd = real(path)
            if fd is not None and os.path.abspath(path) == want:
                st = os.fstat(fd)
                seen.append((st.st_dev, st.st_ino))
            return fd
        return seen, note

    def test_the_sweep_takes_ONE_capability_however_many_rows(self):  # noqa: VACUOUS_ASSERTION — the only absence is assertFalse(file_unknown) inside the per-row loop, and the loop cannot be empty: the assertEqual on removed ids above it requires exactly three rows, unconditionally, before the loop runs. Mutation-proven: flipping the removal branch to gone_ok=False reddens it
        """The count IS the property. Removal succeeds either way on a quiet
        disk — what a per-call resolution costs you is a WINDOW per call, and
        the window count is what this measures."""
        self._rows(3)
        seen, note = self._opens()
        with mock.patch.object(todos, "_dir_fd", note):
            rep = todos.demote(self.SID, "cj", path=self.ledger)
        self.assertEqual(sorted(str(x.get("id")) for x in rep["removed"]),
                         ["1", "2", "3"], rep)
        self.assertEqual(len(seen), 1,
                         "a 3-row sweep took %d personal-directory "
                         "descriptors; every one past the first is a window "
                         "in which the directory can be swapped: %r"
                         % (len(seen), seen))
        # AND A SUCCESSFUL REMOVAL STILL NAMES ITS PATH. Its member is
        # intentionally gone, so the door is asked only for the parent
        # binding; getting that distinction wrong turns every ordinary
        # removal into "location unknown", which is the report going quiet on
        # the common case rather than on the rare one.
        for r in rep["removed"]:
            self.assertTrue(r.get("file"),
                            "an ordinary removal reported no path at all: %r"
                            % (r,))
            self.assertEqual(os.path.realpath(os.path.dirname(r["file"])),
                             os.path.realpath(self.d),
                             "a removal named a path outside the swept "
                             "directory: %r" % (r["file"],))
            self.assertFalse(r.get("file_unknown"))

    def test_the_ROW_READ_comes_from_the_pinned_directory_not_the_name(self):
        """A's own property, scoped to what A delivers.

        Swap the session directory for one holding a same-name, same-id row
        between recovery and enumeration. The enumeration must read OUR row,
        through the pinned descriptor, rather than following the NAME to the
        decoy.

        WHAT THIS DOES NOT YET PROVE, deliberately: the claim is still minted
        by pathname (mkdtemp under dirname), so the removal writes into
        whatever directory holds that name at the time. That is B's property,
        not A's, and a review scoped it there — an arm asserting it here would
        be asserting a cure that has not shipped. It gets its own arm when the
        transaction-created identity lands."""
        self._rows(1)
        swapped, got, real = [], [], todos._personal_rows_at

        def swap_between_recovery_and_enumeration(d, pdir_fd, **kw):
            if not swapped:
                swapped.append(d)
                os.rename(self.d, self.d + ".ours")
                os.makedirs(self.d)
                with open(os.path.join(self.d, "1.json"), "w") as fh:
                    json.dump({"id": "1", todos.STAMP: "task/424241",
                               "subject": "A DECOY"}, fh)
            rows, err = real(d, pdir_fd, **kw)
            got.extend(row.get("subject") for _p, row in rows)
            return rows, err

        with mock.patch.object(todos, "_personal_rows_at",
                               swap_between_recovery_and_enumeration):
            todos.demote(self.SID, "cj", path=self.ledger)
        self.assertTrue(swapped, "the swap never armed — arm is vacuous")
        self.assertEqual(got, ["ROW 1"],
                         "the enumeration read %r — it followed the NAME to "
                         "the decoy instead of reading through the pinned "
                         "descriptor" % (got,))
        # AND THE DECOY IS UNTOUCHED — nothing of a stranger's was read or
        # judged.
        with open(os.path.join(self.d, "1.json")) as fh:
            self.assertEqual(json.load(fh).get("subject"), "A DECOY")

    def test_all_three_phases_receive_the_SAME_inode(self):
        """The capability claim, asserted directly rather than via the count.

        Recovery, enumeration and every removal must be handed one identity.
        The open-count arm proves no SECOND descriptor was taken; this proves
        the one that WAS taken reached all three phases."""
        self._rows(2)
        got = {"recover": [], "enumerate": [], "trash": []}
        r_rec, r_enum, r_trash = (todos._recover_claims_at,
                                  todos._personal_rows_at, todos._trash_at)

        def ident(fd):
            st = os.fstat(fd)
            return (st.st_dev, st.st_ino)

        def rec(sid, apply, d, pdir_fd):
            got["recover"].append(ident(pdir_fd))
            return r_rec(sid, apply, d, pdir_fd)

        def enum(d, pdir_fd, **kw):
            got["enumerate"].append(ident(pdir_fd))
            return r_enum(d, pdir_fd, **kw)

        def trash(sid, full, row, rep, pdir_fd, judged=None):
            got["trash"].append(ident(pdir_fd))
            return r_trash(sid, full, row, rep, pdir_fd, judged)

        with mock.patch.object(todos, "_recover_claims_at", rec), \
                mock.patch.object(todos, "_personal_rows_at", enum), \
                mock.patch.object(todos, "_trash_at", trash):
            rep = todos.demote(self.SID, "cj", path=self.ledger)
        self.assertEqual(sorted(str(x.get("id")) for x in rep["removed"]),
                         ["1", "2"], rep)
        for phase in ("recover", "enumerate", "trash"):
            self.assertTrue(got[phase], "%s never ran — arm is vacuous"
                            % phase)
        every = got["recover"] + got["enumerate"] + got["trash"]
        self.assertEqual(len(set(every)), 1,
                         "the phases were handed %d different directories: %r"
                         % (len(set(every)), got))

    def test_the_borrowed_descriptor_is_never_closed_by_a_callee(self):
        """A capability that can be closed by whoever it is passed to is not a
        capability. Two callees closed this one during the split and produced
        EBADF; the sweep must finish with exactly the descriptors it started
        with and no more."""
        self._rows(2)
        before = _fd_targets()
        rep = todos.demote(self.SID, "cj", path=self.ledger)
        self.assertEqual(sorted(str(x.get("id")) for x in rep["removed"]),
                         ["1", "2"], "the sweep did not run: %r" % (rep,))
        _assert_no_leak(self, before, _fd_targets(), 1)

    def test_a_swap_between_ENUMERATION_and_TRASH_never_reports_a_decoy_path(self):
        """The second swap point. Rename the session directory
        after the rows are read but before the removal, install a decoy, and
        no reported path may name anything inside it."""
        self._rows(1)
        swapped, real = [], todos._trash_at

        def swap_then_trash(sid, full, row, rep, pdir_fd, judged=None):
            if not swapped:
                swapped.append(full)
                os.rename(self.d, self.d + ".ours")
                os.makedirs(self.d)
                with open(os.path.join(self.d, "1.json"), "w") as fh:
                    json.dump({"id": "1", "subject": "A DECOY"}, fh)
            return real(sid, full, row, rep, pdir_fd, judged)

        with mock.patch.object(todos, "_trash_at", swap_then_trash):
            rep = todos.demote(self.SID, "cj", path=self.ledger)
        self.assertTrue(swapped, "the swap never armed — arm is vacuous")
        decoy = os.path.realpath(self.d)
        named = []
        for key in ("removed", "failed", "would_remove", "orphaned",
                    "unparseable", "unreadable"):
            for r in rep.get(key) or []:
                if isinstance(r, dict) and r.get("file"):
                    named.append((key, r["file"]))
        for key, path in named:
            self.assertNotEqual(os.path.realpath(os.path.dirname(path)), decoy,
                                "rep[%r] names %s, which is inside the DECOY "
                                "directory" % (key, path))
        # AND THE DECOY'S OWN BYTES ARE UNTOUCHED.
        with open(os.path.join(self.d, "1.json")) as fh:
            self.assertEqual(json.load(fh).get("subject"), "A DECOY")

    def test_a_swap_before_the_CLAIM_never_writes_into_the_replacement(self):  # noqa: VACUOUS_ASSERTION — the acceptance is an unconditional EQUALITY on the decoy's bytes plus a required PRESENCE (the claim must exist inside the pinned directory); the swapped-list is the must-fire observable, not the acceptance
        """The capability result B, and the defect this lane's own decoy
        arm found: the claim was minted with mkdtemp(dir=<pathname>), which
        creates inside whatever holds that name AT THAT MOMENT. A swap between
        the enumeration and the removal therefore put the claim — and the row
        it carries — into the REPLACEMENT directory.

        The property is the one a review named, not a claim about preventing
        the swap: NO STRANGER DATA TOUCHED. The decoy directory must contain
        exactly what was put there and nothing else."""
        self._rows(1)
        swapped, real = [], todos._trash_at

        def swap_then_trash(sid, full, row, rep, pdir_fd, judged=None):
            if not swapped:
                swapped.append(full)
                os.rename(self.d, self.d + ".ours")
                os.makedirs(self.d)
                with open(os.path.join(self.d, "decoy.json"), "w") as fh:
                    json.dump({"id": "decoy"}, fh)
            return real(sid, full, row, rep, pdir_fd, judged)

        with mock.patch.object(todos, "_trash_at", swap_then_trash):
            todos.demote(self.SID, "cj", path=self.ledger)
        self.assertTrue(swapped, "the swap never armed — arm is vacuous")
        # THE DECOY DIRECTORY HOLDS EXACTLY WHAT WE PUT IN IT.
        self.assertEqual(sorted(os.listdir(self.d)), ["decoy.json"],
                         "the removal wrote into the REPLACEMENT directory: "
                         "%r" % (sorted(os.listdir(self.d)),))
        with open(os.path.join(self.d, "decoy.json")) as fh:
            self.assertEqual(json.load(fh), {"id": "decoy"})
        # AND THE CLAIM WENT WHERE THE CAPABILITY POINTED.
        ours = sorted(os.listdir(self.d + ".ours"))
        self.assertTrue([n for n in ours if n.startswith(todos.CLAIM_PREFIX)],
                        "no claim was made inside the pinned directory: %r"
                        % (ours,))

    def test_a_NONEMPTY_replacement_claim_is_refused_not_written_into(self):  # noqa: VACUOUS_ASSERTION — the acceptance is an unconditional EQUALITY on the stranger's bytes and a required PRESENCE (the row must still be at its path, since the removal refused); the swapped-list is the must-fire observable
        """The B option (a). mkdirat proves nothing was there when we
        created it; it cannot prove the thing we then OPENED is that same
        object, because a same-UID substitution in between ignores any lease
        we could invent. What CAN be established through the opened
        descriptor is that it is EMPTY — and an empty directory is one where
        adoption is provably harmless. A replacement WITH CONTENTS is somebody
        else's, and this transaction has no business writing beside it."""
        self._rows(1)
        swapped, real = [], todos._open_member

        def swap_the_claim_for_a_populated_one(dir_fd, name, flags=None):
            if (not swapped and name.startswith(todos.CLAIM_PREFIX)
                    and flags is not None):
                swapped.append(name)
                # replace the freshly minted claim with a NON-EMPTY stranger
                os.rmdir(os.path.join(self.d, name))
                os.mkdir(os.path.join(self.d, name))
                with open(os.path.join(self.d, name, "theirs.json"), "w") as f:
                    json.dump({"id": "not ours"}, f)
            return real(dir_fd, name, flags)

        with mock.patch.object(todos, "_open_member",
                               swap_the_claim_for_a_populated_one):
            rep = todos.demote(self.SID, "cj", path=self.ledger)
        self.assertTrue(swapped, "the swap never armed — arm is vacuous")
        # THE STRANGER'S FILE IS EXACTLY AS THEY LEFT IT.
        theirs = os.path.join(self.d, swapped[0], "theirs.json")
        self.assertTrue(os.path.exists(theirs),
                        "the stranger's file was removed")
        with open(theirs) as fh:
            self.assertEqual(json.load(fh), {"id": "not ours"},
                             "the stranger's bytes were overwritten")
        self.assertEqual(sorted(os.listdir(os.path.dirname(theirs))),
                         ["theirs.json"],
                         "the transaction wrote into a directory it did not "
                         "make: %r" % (sorted(os.listdir(
                             os.path.dirname(theirs))),))
        # AND THE ROW IS STILL OURS, UNREMOVED — a refusal cancels.
        self.assertTrue(os.path.exists(os.path.join(self.d, "1.json")),
                        "the row left its path even though the claim was "
                        "refused: %r" % (rep,))

    def test_an_UNPROVABLE_identity_KEEPS_the_row(self):
        """G2 — my own dogfood finding, now armed.

        The cure for "the judged identity never reaches the mutation" threaded
        it as judged=None behind an is-not-None guard, so the one case where
        nothing is known got NO check. An optional ERROR may default to None,
        because absence means no error; an optional FACT may not, because
        absence means cannot-tell — and cannot-tell has meant KEEP on this leg
        since the first round.

        Probe that produced it: force the identity capture to None on the live
        path, swap the row for a different inode after the judgement. Before
        the cure the stranger's row was REMOVED and reported a success."""
        self._rows(1)
        row_path = os.path.join(self.d, "1.json")
        swapped, real = [], todos._trash_at

        def swap_then_trash(sid, full, row, rep, pdir_fd, judged=None):
            if not swapped:
                swapped.append(judged)
                other = full + ".other"
                with open(other, "w") as fh:
                    json.dump({"id": "1", todos.STAMP: "task/424241",
                               "subject": "A STRANGER"}, fh)
                os.replace(other, full)
            return real(sid, full, row, rep, pdir_fd, judged)

        with mock.patch.object(todos, "_member_id_of", lambda fd: None), \
                mock.patch.object(todos, "_trash_at", swap_then_trash):
            rep = todos.demote(self.SID, "cj", path=self.ledger)
        self.assertEqual(swapped, [None],
                         "the fixture did not reach _trash_at with an "
                         "unprovable identity, so this arm proves nothing: %r"
                         % (swapped,))
        # THE ROW NOBODY JUDGED IS STILL THERE.
        self.assertTrue(os.path.exists(row_path),
                        "a row this sweep could not identify was removed")
        with open(row_path) as fh:
            self.assertEqual(json.load(fh).get("subject"), "A STRANGER")
        self.assertFalse(rep["removed"],
                         "the report claims a removal that did not happen")
        self.assertTrue(rep["failed"], "the refusal was not reported")
        self.assertIn("could not be identified",
                      str(rep["failed"][0].get("failed")))

    def test_a_symlink_ANYWHERE_in_the_undo_chain_refuses(self):  # noqa: VACUOUS_ASSERTION — the empty-leaked assertion is paired, in the same subTest, with a REQUIRED RAISE: _makedirs_durable must raise and the message must name a symlink, else self.fail. A walk that followed the link raises nothing and fails there before any absence is examined
        """G3 — the ancestry finding, both variants, now armed.

        The walk stopped at the first EXISTING directory, so it checked only
        the components this transaction CREATES. The link is always ABOVE what
        we make, which is why nothing we made looked suspicious — and their
        second variant put it at .state, INSIDE the derived namespace, so this
        was never about an owner symlinking their home."""
        from helm import record
        for where in ("under-home", "at-state"):
            with self.subTest(where=where):
                outside = os.path.join(self.tmp, "OUTSIDE-" + where)
                os.makedirs(outside)
                sess = record.session_dir(self.SID)
                os.makedirs(sess, exist_ok=True)
                troot = os.path.join(sess, todos.TRASH)
                if where == "under-home":
                    # the link IS the trash root's own parent chain
                    os.symlink(outside, troot)
                else:
                    # the link is an ANCESTOR that already exists through it
                    os.makedirs(os.path.join(outside, "deep"))
                    inner = os.path.join(sess, "linked-state")
                    os.symlink(outside, inner)
                    troot = os.path.join(inner, "deep", todos.TRASH)
                full = os.path.join(self.d, "1.json")
                row = {"id": "1", todos.STAMP: "task/424241"}
                with open(full, "w") as fh:
                    json.dump(row, fh)
                try:
                    todos._makedirs_durable(os.path.join(troot, "x"))
                except OSError as e:
                    self.assertIn("symlink", str(e),
                                  "refused for the wrong reason: %s" % e)
                else:
                    self.fail("a symlink in the undo chain was followed "
                              "(%s variant)" % where)
                # NOTHING WAS PUBLISHED THROUGH THE LINK.
                leaked = [n for _r, d, f in os.walk(outside) for n in d + f
                          if todos.CLAIM_PREFIX in n or n == todos.TRASH]
                self.assertEqual(leaked, [],
                                 "published through the symlink: %r" % leaked)
                shutil.rmtree(outside, ignore_errors=True)
                for stale in (troot, os.path.join(sess, "linked-state")):
                    if os.path.islink(stale):
                        os.unlink(stale)

    def test_the_CLAIM_MOVE_refuses_a_destination_planted_after_adoption(self):
        """The item 14 — the cross-dirfd no-clobber seam had NO direct
        arm. Replacing fsops.rename_noreplace with a clobbering cross-dirfd
        os.rename left every focused cure test green.

        The window is exact: a racer creates the destination AFTER
        _adopted_shape has accepted the claim directory as empty and BEFORE
        the source is moved into it. With a clobbering rename the stranger's
        bytes are overwritten and the removal proceeds; with NOREPLACE the
        syscall itself refuses, and both files survive.

        This is the primitive the whole lane rests on, and nothing was
        pointing at it."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "task/424241", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        theirs = b'{"planted": "by a racer"}'
        planted, real = [], todos._adopted_shape

        def plant_the_destination(dfd):
            why = real(dfd)
            if why is None and not planted:
                planted.append(dfd)
                fd = os.open("1.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600, dir_fd=dfd)
                try:
                    os.write(fd, theirs)
                finally:
                    os.close(fd)
            return why

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_adopted_shape", plant_the_destination):
            err = todos._trash(self.SID, full, row, rep)
        self.assertEqual(len(planted), 1, "the plant never armed — vacuous")
        self.assertIsNotNone(err, "the move overwrote a destination that "
                                  "appeared after the emptiness check")
        # BOTH FILES SURVIVE: the racer's bytes and our row.
        found = []
        for root, _dirs, files in os.walk(self.d):
            for n in files:
                with open(os.path.join(root, n), "rb") as fh:
                    if fh.read() == theirs:
                        found.append(os.path.join(root, n))
        self.assertTrue(found, "the planted destination was overwritten")
        self.assertTrue(os.path.exists(full),
                        "the row was removed even though the move refused")

    def test_a_row_SWAPPED_after_the_judgement_is_never_moved(self):
        """The refuter 1, finding 1. The enumeration's identity was
        carried only into the REPORT, never into the mutation — so a file
        swapped after the judgement, carrying the SAME id and stamp, was moved
        and removed although nothing had ever decided about it.

        The existing re-read compares LOGICAL fields, which is precisely what
        such a swap satisfies. Only the inode can tell them apart."""
        self._rows(1)
        row_path = os.path.join(self.d, "1.json")
        ours = os.stat(row_path)
        swapped, real = [], todos._trash_at

        def swap_then_trash(sid, full, row, rep, pdir_fd, judged=None):
            if not swapped:
                swapped.append(full)
                # a DIFFERENT INODE carrying the same logical row
                other = row_path + ".other"
                with open(other, "w") as fh:
                    json.dump({"id": "1", todos.STAMP: "task/424241",
                               "subject": "SOMEBODY ELSE'S WORK"}, fh)
                os.replace(other, row_path)
            return real(sid, full, row, rep, pdir_fd, judged)

        with mock.patch.object(todos, "_trash_at", swap_then_trash):
            rep = todos.demote(self.SID, "cj", path=self.ledger)
        self.assertEqual(len(swapped), 1, "the swap never armed — vacuous")
        # THE SWAPPED FILE IS STILL THERE, UNJUDGED AND UNTOUCHED.
        self.assertTrue(os.path.exists(row_path),
                        "a row this sweep never judged was removed")
        with open(row_path) as fh:
            self.assertEqual(json.load(fh).get("subject"),
                             "SOMEBODY ELSE'S WORK",
                             "the swapped-in row was replaced or moved")
        now = os.stat(row_path)
        self.assertNotEqual((now.st_dev, now.st_ino), (ours.st_dev, ours.st_ino),
                            "fixture: the swap must change the inode")
        # AND THE REFUSAL IS REPORTED, not silent.
        self.assertFalse(rep["removed"],
                         "the report claims a removal that did not happen: %r"
                         % (rep["removed"],))
        self.assertTrue(rep["failed"], "the refusal was not reported: %r" % (rep,))
        self.assertIn("no longer the row this sweep judged",
                      str(rep["failed"][0].get("failed")))

    def test_a_symlink_SUBSTITUTED_mid_creation_is_refused(self):  # noqa: VACUOUS_ASSERTION — the empty-listdir assertion is paired with a REQUIRED RAISE naming a symlink, and with a must-fire count on the substitution. A creation that followed the link raises nothing and fails on assertRaises before any absence is examined
        """The item 5 — the TOCTOU that lstat-then-makedirs cannot close.

        The previous version validated every component with lstat and then
        called os.makedirs on the PATHNAME: two resolutions with a window
        between them, so replacing a validated ancestor with a symlink after
        the last check made the create follow the new link and build the undo
        outside the namespace.

        No ordering of check-then-create fixes that, because the check and the
        act address the name twice. Creation is descriptor-relative now, and
        O_NOFOLLOW is not a check BEFORE the act — it is a constraint ON it,
        refused inside the same syscall that would have followed the link, so
        there is no interval to race.

        The injection sits in the exact window: the component has just been
        created and has not yet been opened."""
        outside = os.path.join(self.tmp, "SUBSTITUTED")
        os.makedirs(outside)
        base = os.path.join(self.tmp, "base")
        os.makedirs(base)
        target = os.path.join(base, "mid", "deep", "leaf")
        real_mkdir, swapped = os.mkdir, []

        def mkdir_then_substitute(name, *a, **k):
            r = real_mkdir(name, *a, **k)
            if not swapped and name == "mid" and k.get("dir_fd") is not None:
                swapped.append(name)
                os.rmdir("mid", dir_fd=k["dir_fd"])
                os.symlink(outside, "mid", dir_fd=k["dir_fd"])
            return r

        with mock.patch.object(os, "mkdir", mkdir_then_substitute):
            with self.assertRaises(OSError) as caught:
                todos._makedirs_durable(target)
        self.assertEqual(len(swapped), 1,
                         "the substitution never armed — arm is vacuous")
        self.assertIn("symlink", str(caught.exception))
        self.assertEqual(sorted(os.listdir(outside)), [],
                         "creation followed the substituted link and built "
                         "outside the namespace: %r"
                         % (sorted(os.listdir(outside)),))



class DurabilityHappensBEFORETheStateSaysSoTest(unittest.TestCase):
    """The C3 — AN ORDERING NOBODY WAS WATCHING.

    The cross-filesystem undo is a distinct copy, so its DIRECTORY ENTRY has
    to be durable before the manifest goes terminal: a terminal state means
    "the removal completed and the backup exists", and writing it on an
    unsynced entry records a decision the disk has not made.

    The sync was there and nothing proved it — no-op it and every test stayed
    green, which is the same shape as C4 and C5. A cure with no arm is a cure
    until someone edits near it."""

    SID = "sess-durorder"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-durorder-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _removal(self, cross_fs):
        """One real removal, recording every fsync by inode IN ORDER, and the
        moment the terminal state is written."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "task/424241", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        order, real_fsync, real_mark = [], os.fsync, todos._mark_residual

        def note_fsync(fd):
            try:
                st = os.fstat(fd)
                order.append(("fsync", st.st_dev, st.st_ino))
            except OSError:
                pass
            return real_fsync(fd)

        def note_terminal(why, held, qdir_fd):
            order.append(("terminal", None, None))
            return real_mark(why, held, qdir_fd)

        real_link = todos.os.link
        forced = []

        def exdev(src, dst, **kw):
            if cross_fs and (todos.TRASH in str(dst)
                             or kw.get("dst_dir_fd") is not None):
                forced.append(dst)
                raise OSError(errno.EXDEV, "simulated cross-filesystem undo")
            return real_link(src, dst, **kw)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(os, "fsync", note_fsync), \
                mock.patch.object(todos, "_mark_residual", note_terminal), \
                mock.patch.object(todos.os, "link", exdev):
            err = todos._trash(self.SID, full, row, rep)
        self.assertIsNone(err, "the removal refused: %r" % (err,))
        if cross_fs:
            self.assertTrue(forced, "EXDEV never fired — not the cross-FS path")
        return order, rep

    def test_the_CROSS_FS_undo_directory_is_synced_before_the_terminal_state(self):
        order, rep = self._removal(cross_fs=True)
        undo = _undo_of(rep, "1.json")
        self.assertTrue(undo, "no undo location was reported: %r" % (rep,))
        want = os.stat(os.path.dirname(undo))
        dest_id = ("fsync", want.st_dev, want.st_ino)
        self.assertIn(dest_id, order,
                      "the undo DIRECTORY was never synced; a terminal state "
                      "on an unsynced entry records a decision the disk has "
                      "not made. syncs seen: %r" % (order,))
        self.assertIn(("terminal", None, None), order,
                      "the terminal state was never written — arm is vacuous")
        self.assertLess(order.index(dest_id),
                        order.index(("terminal", None, None)),
                        "the claim went terminal BEFORE its undo entry was "
                        "durable: %r" % (order,))

    def test_the_SAME_FS_removal_still_syncs_its_undo_directory(self):
        """The sibling, so the arm above measures the ordering rather than a
        removal that stopped syncing at all. Same-filesystem the undo is a
        second name for one inode, and its entry still has to be durable
        before the state says the removal completed."""
        order, rep = self._removal(cross_fs=False)
        undo = _undo_of(rep, "1.json")
        self.assertTrue(undo, "no undo location was reported: %r" % (rep,))
        want = os.stat(os.path.dirname(undo))
        dest_id = ("fsync", want.st_dev, want.st_ino)
        self.assertIn(dest_id, order,
                      "the undo directory was never synced: %r" % (order,))
        self.assertLess(order.index(dest_id),
                        order.index(("terminal", None, None)),
                        "the claim went terminal before its undo entry was "
                        "durable: %r" % (order,))

    def test_THIS_MODULE_NEVER_DELETES_BY_PATHNAME(self):  # noqa: VACUOUS_ASSERTION — the empty-offenders assertion is the point, and the DETECTOR is seeded with must-hits on synthetic sources in the same arm (a pathname rmdir and unlink must be found, a dir_fd one must not), so an instrument that finds nothing fails before the module is judged
        """The finding 2, as a PROPERTY rather than a case.

        Seven review rounds each removed one delete-by-name; the eighth would
        have found another, because a per-case handler cannot be completed by
        adding cases. What can be completed is the invariant: nothing in this
        module removes anything except relative to a held directory
        descriptor. That is four lines of AST and it cannot be satisfied by
        enumeration."""
        import ast as _ast
        src = open(os.path.join(os.path.dirname(todos.__file__),
                                "todos.py")).read()
        # WIDENED, AND ITS LIMIT STATED IN THE TEST (item 15). The
        # first version matched three spellings on `os.`, so shutil.rmtree,
        # Path(p).unlink, an imported alias, or os.removedirs all walked past
        # it. Enumerating more names is the same per-case trap that produced
        # this lane — the next spelling is always outside the set — so this
        # matches on the OPERATION NAME wherever it appears as a call, whoever
        # owns it, and treats a dir_fd keyword as the only exemption.
        #
        # WHAT IT STILL CANNOT SEE, said plainly rather than implied: a
        # deletion reached through a variable holding a function
        # (`f = os.unlink; f(p)`), or through a helper in another module that
        # this file merely calls. Those are covered behaviourally by the
        # capability arms — a delete-by-name in any of them removes a
        # stranger's directory and reddens the partial-acquisition and
        # rename-reuse arms. This is a cheap tripwire, not the proof.
        DELETERS = ("rmdir", "unlink", "remove", "removedirs", "rmtree")
        offenders = []
        for n in _ast.walk(_ast.parse(src)):
            if not isinstance(n, _ast.Call):
                continue
            name = (getattr(n.func, "attr", None)
                    or getattr(n.func, "id", None))
            if name not in DELETERS:
                continue
            if any(k.arg == "dir_fd" for k in n.keywords):
                continue           # descriptor-relative: the safe form
            owner = getattr(getattr(n.func, "value", None), "id", "?")
            offenders.append("%s:%d %s.%s" % ("todos.py", n.lineno, owner,
                                              name))
        self.assertEqual(offenders, [],
                         "a delete by PATHNAME came back — POSIX has no "
                         "conditional remove-by-inode, so between resolving "
                         "that name and acting on it the name can lead "
                         "somewhere else: %r" % (offenders,))
        # THE DETECTOR IS SEEDED WITH A MUST-HIT, because an empty result
        # proves nothing about the code until the instrument is shown to find
        # what it is looking for. My first version asserted the module still
        # had descriptor-relative deletes as its non-vacuity evidence — and
        # that FAILED, which is itself the finding: after the structural
        # ruling (never delete after the anchor) and finding 2 (nor before
        # it), this module now removes NOTHING AT ALL. Disposal belongs to
        # recovery under a lock, and to the operator. So the instrument gets
        # proven against a synthetic source instead.
        def _pathname_deletes(text):
            out = []
            for n in _ast.walk(_ast.parse(text)):
                if not isinstance(n, _ast.Call):
                    continue
                nm = (getattr(n.func, "attr", None)
                      or getattr(n.func, "id", None))
                if nm in DELETERS and not any(k.arg == "dir_fd"
                                              for k in n.keywords):
                    out.append(n.lineno)
            return out
        self.assertEqual(_pathname_deletes("import os\nos.rmdir(p)\n"), [2],
                         "the detector does not find a pathname rmdir")
        self.assertEqual(
            _pathname_deletes("import shutil\nshutil.rmtree(p)\n"), [2],
            "the detector does not find shutil.rmtree — the spelling that "
            "removes the MOST at once")
        self.assertEqual(
            _pathname_deletes("from os import unlink\nunlink(p)\n"), [2],
            "the detector does not find an imported alias")
        self.assertEqual(_pathname_deletes("import os\nos.unlink(p)\n"), [2],
                         "the detector does not find a pathname unlink")
        self.assertEqual(
            _pathname_deletes("import os\nos.unlink(n, dir_fd=d)\n"), [],
            "the detector flags a descriptor-relative delete, which is the "
            "safe form and must not be reported")

    def test_a_POPULATED_undo_directory_is_refused_like_a_populated_claim(self):  # noqa: VACUOUS_ASSERTION — every assertion is a presence: the plant must fire, an error must be returned, the stranger's file must still exist with its exact bytes and be ALONE in that directory, and the row must still be at its path
        """The finding 3. dest was minted, opened and then TRUSTED — the
        only capability in this transaction with no check at all. Replace the
        fresh undo directory with a populated one that does not hold the row's
        basename, and the removal published the undo beside a stranger's bytes
        while the directory we actually made stayed empty."""
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "task/424241", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        theirs = b'{"not": "ours"}'
        planted, real = [], todos._dir_fd

        def populate_the_dest(path):
            if not planted and todos.TRASH in path \
                    and os.path.basename(path).startswith(todos.CLAIM_PREFIX):
                planted.append(path)
                with open(os.path.join(path, "somebody-elses.json"),
                          "wb") as fh:
                    fh.write(theirs)
            return real(path)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(todos, "_dir_fd", populate_the_dest):
            err = todos._trash(self.SID, full, row, rep)
        self.assertEqual(len(planted), 1, "the plant never armed — vacuous")
        self.assertIsNotNone(err, "the removal wrote into a directory it had "
                                  "not established was empty")
        # THE STRANGER'S FILE IS UNTOUCHED AND ALONE.
        self.assertEqual(sorted(os.listdir(planted[0])),
                         ["somebody-elses.json"],
                         "the undo was published beside a stranger's bytes: "
                         "%r" % (sorted(os.listdir(planted[0])),))
        with open(os.path.join(planted[0], "somebody-elses.json"), "rb") as fh:
            self.assertEqual(fh.read(), theirs)
        self.assertTrue(os.path.exists(full), "the row was removed anyway")

    def test_a_SYMLINKED_undo_root_refuses_instead_of_publishing_outside(self):
        """The finding 4. os.path.isdir FOLLOWS links and os.makedirs
        walks the pathname, so a promoted-trash symlinked at an external
        directory made a removal SUCCEED while publishing its undo outside the
        namespace every recovery root is derived from — unreachable by the
        only code that knows to look for it, with the report saying the
        removal completed."""
        from helm import record
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "task/424241", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        outside = os.path.join(self.tmp, "ELSEWHERE")
        os.makedirs(outside)
        sess = record.session_dir(self.SID)
        os.makedirs(sess, exist_ok=True)
        os.symlink(outside, os.path.join(sess, todos.TRASH))

        rep = {"removed": [], "failed": []}
        err = todos._trash(self.SID, full, row, rep)
        self.assertIsNotNone(err, "the removal published through a symlinked "
                                  "undo root")
        self.assertIn("symlink", err)
        self.assertEqual(sorted(os.listdir(outside)), [],
                         "something was published outside the session's "
                         "namespace: %r" % (sorted(os.listdir(outside)),))
        self.assertTrue(os.path.exists(full),
                        "the row left its path on a refused removal")


class EveryStandInMatchesTheFunctionItReplacesTest(unittest.TestCase):
    """A STAND-IN WITH THE WRONG SIGNATURE IS A TEST THAT CANNOT RUN, and this
    class has now cost two gate cycles.

    Adding a parameter to a production function leaves every mock of it with
    the old arity. The failure is loud when it happens (TypeError) but it
    happens only in the arms that reach that call, so a per-method local run
    finds none of them — the same blind spot as a moved seam, one step over.

    Enumerating the mocks and fixing them is what I did twice. This checks the
    PROPERTY instead: every stand-in installed for a helm.todos function must
    accept the calls that function accepts. It is derived from the test file
    itself, so a mock added tomorrow is covered without anyone remembering."""

    def test_every_patched_stand_in_accepts_the_real_calls(self):  # noqa: VACUOUS_ASSERTION — the empty-offenders assertion is the point and it is guarded by an unconditional presence in the same arm: `checked` must be non-empty, so a scan that finds no stand-ins fails before any verdict is reached. Mutation-proven against the CALLED arity, which is the regression that matters: reverting a stand-in to the pre-`judged` 5-positional signature reddens it. An earlier version of this comment claimed proof it did not have — it compared to REQUIRED arity and missed exactly the optional-parameter case
        import ast as _ast
        import inspect
        src = open(__file__).read()
        tree = _ast.parse(src)
        # RESOLVED BY SCOPE, NOT BY NAME (found by this property's own first
        # sharpening). A global name->def map accused `mangle` of standing in
        # for _verified_path when the `mangle` at that call site is a
        # PARAMETER of the enclosing helper and the def of that name lives in
        # another class entirely. A check that can blame the wrong function
        # can also excuse the wrong one, so the resolution has to respect
        # scope — and a name it cannot resolve statically is UNVERIFIABLE,
        # reported as such rather than silently passed.
        funcs, params = {}, set()
        for n in _ast.walk(tree):
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                funcs.setdefault(n.name, []).append(n)
                for a in list(n.args.args) + list(n.args.kwonlyargs):
                    params.add(a.arg)

        def _arity(node):
            a = node.args
            required = len(a.posonlyargs) + len(a.args) - len(a.defaults)
            most = None if a.vararg else len(a.posonlyargs) + len(a.args)
            return required, most

        checked, offenders, unverifiable = [], [], []
        for n in _ast.walk(tree):
            if not (isinstance(n, _ast.Call)
                    and getattr(n.func, "attr", "") == "object"
                    and len(n.args) >= 3
                    and isinstance(n.args[0], _ast.Name)
                    and n.args[0].id == "todos"
                    and isinstance(n.args[1], _ast.Constant)
                    and isinstance(n.args[2], _ast.Name)):
                continue
            target, stand_in = n.args[1].value, n.args[2].id
            real = getattr(todos, target, None)
            if not callable(real):
                continue
            if stand_in in params or stand_in not in funcs \
                    or len(funcs[stand_in]) > 1:
                # a parameter, an unknown, or an ambiguous name: this checker
                # cannot say, and saying nothing quietly is what it exists to
                # prevent elsewhere
                unverifiable.append("%s (stands in for todos.%s)"
                                    % (stand_in, target))
                continue
            try:
                sig = inspect.signature(real)
            except (TypeError, ValueError):
                continue
            # THE MAXIMUM POSITIONALS THE FUNCTION IS ACTUALLY CALLED WITH,
            # derived from the call sites — not its REQUIRED count.
            # Comparing to required lets a 5-positional stand-in pass for a
            # function with 5 required + 1 optional that its owner calls with
            # 6, which is precisely the regression that cost a gate cycle. My
            # noqa claimed this was mutation-proven; it was proven for the
            # easy half only, and that sentence is corrected with the code.
            prod = _ast.parse(open(os.path.join(
                os.path.dirname(todos.__file__), "todos.py")).read())
            called = 0
            for c in _ast.walk(prod):
                if isinstance(c, _ast.Call) and (
                        getattr(c.func, "id", None) == target
                        or getattr(c.func, "attr", None) == target):
                    called = max(called, len(c.args))
            need = max(called, sum(
                1 for p in sig.parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)))
            most_real = None if any(p.kind == p.VAR_POSITIONAL
                                    for p in sig.parameters.values()) \
                else sum(1 for p in sig.parameters.values()
                         if p.kind in (p.POSITIONAL_ONLY,
                                       p.POSITIONAL_OR_KEYWORD))
            for node in funcs[stand_in]:
                required, most = _arity(node)
                checked.append((target, stand_in))
                if most is not None and most < need:
                    offenders.append(
                        "%s stands in for todos.%s, which is called with %d "
                        "positional argument(s), but accepts at most %d"
                        % (stand_in, target, need, most))
                elif most_real is not None and required > most_real:
                    offenders.append(
                        "%s stands in for todos.%s but REQUIRES %d "
                        "positional argument(s); the real one is called with "
                        "at most %d" % (stand_in, target, required, most_real))
        self.assertTrue(checked,
                        "no stand-ins were found at all — the scan is looking "
                        "in the wrong place and would pass on anything")
        # WHAT IT COULD NOT CHECK IS DISCLOSED, never dropped. A property
        # that silently skips the hard names reports a clean result about the
        # easy ones.
        self.assertLessEqual(
            len(unverifiable), len(checked),
            "more stand-ins were UNVERIFIABLE than verified, so this property "
            "is mostly not running: %r" % (sorted(set(unverifiable)),))
        self.assertEqual(offenders, [],
                         "a stand-in cannot accept the calls its target "
                         "accepts, so every arm using it dies with a "
                         "TypeError instead of testing anything:\n  %s"
                         % "\n  ".join(offenders))


class TheSyscallWrapperNeverREPORTSSuccessItCannotProveTest(unittest.TestCase):
    """The finding 7 (G1) — the cross-interpreter errno contract.

    I recorded this as unarmable-from-this-host because it was found on
    GraalPy, and that was wrong: the defect is not an INTERPRETER, it is a
    CONTRACT — a syscall that returns nonzero while errno reads 0. That is
    reproducible anywhere by injecting both, and I wrote "I cannot close this"
    into a ledger before spending the sixty seconds to try.

    Two halves, both measured on CPython:
      * errno.ENOTSUP is absent on some interpreters, so raising it to REPORT
        an unavailable syscall failed in the same breath as the report.
      * a nonzero return with errno 0 surfaced as OSError(0, "Success"), which
        a caller reads as neither a recognised error nor a None — while the
        kernel had in fact refused and preserved the bytes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-errno-")
        self.d = self.tmp
        self.a = os.path.join(self.tmp, "a")
        self.b = os.path.join(self.tmp, "b")
        with open(self.a, "w") as fh:
            fh.write("SOURCE")
        with open(self.b, "w") as fh:
            fh.write("STRANGER")
        self.dfd = os.open(self.tmp, os.O_RDONLY | os.O_DIRECTORY)

    def tearDown(self):
        try:
            os.close(self.dfd)
        except OSError:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_failure_with_NO_ERRNO_is_still_a_failure(self):
        """The exact GraalPy shape: nonzero return, errno unset. It must NOT
        come back as None (success) and must NOT come back as
        OSError(0, 'Success'), because a caller checking for FileExistsError
        sees neither and proceeds."""
        import ctypes as _ctypes
        with mock.patch.object(fsops, "_RENAMEAT2", lambda *a, **k: -1), \
                mock.patch.object(_ctypes, "get_errno", lambda: 0):
            err = fsops.rename_noreplace(self.dfd, b"a", self.dfd, b"b")
        self.assertIsNotNone(err, "a failed rename reported SUCCESS")
        self.assertIsInstance(err, OSError)
        self.assertTrue(err.errno, "the error carries errno 0, which reads as "
                                   "'Success' to every caller that prints it")
        self.assertIn("did not report why", str(err))
        # AND THE STRANGER'S BYTES ARE UNTOUCHED — the kernel refused.
        with open(self.b) as fh:
            self.assertEqual(fh.read(), "STRANGER")

    def test_an_UNAVAILABLE_syscall_reports_rather_than_AttributeErrors(self):
        """errno.ENOTSUP is absent on some interpreters. Raising it to say
        'this syscall is unavailable' then fails while REPORTING the failure —
        the report breaking in the same breath as the thing it reports."""
        with mock.patch.object(fsops, "_RENAMEAT2", None):
            self.assertFalse(fsops.have_renameat2(),
                             "the capability probe disagrees with the binding")
            err = fsops.rename_noreplace(self.dfd, b"a", self.dfd, b"b")
        self.assertIsInstance(err, OSError)
        self.assertTrue(err.errno, "an unavailable syscall reported errno 0")
        self.assertIn("unavailable", str(err))

    def test_a_BOUND_symbol_is_not_a_working_syscall(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the LAST line, outside every subTest: this filesystem must PASS the probe, so the arm measures the unsupported answers rather than a probe that refuses everything. Each subTest asserts a required presence (a reason string naming no-clobber)
        """The item 8. have_renameat2 was true whenever libc exported the
        symbol, so a kernel answering ENOSYS — or a filesystem answering
        EINVAL to the NOREPLACE flag — passed the preflight, and the claim
        directory, undo directory and manifest were all created before the
        first real call discovered it. The transaction left artifacts behind
        while reporting that nothing was removed.

        The preflight EXERCISES the operation with the exact flag on the
        directory the transaction will use. ENOENT means the machinery works
        and only the source was missing, which is the answer we want."""
        import ctypes as _ctypes
        for bad in (getattr(errno, "ENOSYS", 38), errno.EINVAL,
                    getattr(errno, "EOPNOTSUPP", 95)):
            with self.subTest(errno=bad):
                with mock.patch.object(fsops, "_RENAMEAT2",
                                       lambda *a, **k: -1), \
                        mock.patch.object(_ctypes, "get_errno",
                                          lambda: bad):
                    why = fsops.supports_noreplace(self.dfd)
                self.assertIsNotNone(
                    why, "errno %d passed the substrate probe, so a host that "
                         "cannot promise no-clobber would proceed" % bad)
                self.assertIn("no-clobber", why)
        # UNCONDITIONAL POSITIVE CONTROL: this filesystem DOES support it, so
        # the arm measures the unsupported answers rather than a probe that
        # refuses everything.
        self.assertIsNone(fsops.supports_noreplace(self.dfd),
                          "the probe refuses a substrate that works")

    def test_configs_delegates_its_renameat2_semantics(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is section (c) in the same arm: a REAL collision must still raise FileExistsError and the stranger's bytes must be intact, so the two injected sections measure the shared contract rather than a wrapper that raises on everything
        """The item 10. The configs wrapper kept its own copy of the
        error contract beside this module, and the copy carried both bugs the
        owner had already fixed: errno.ENOTSUP is absent on some interpreters,
        so reporting an unavailable syscall raised AttributeError out of
        write_file's OSError handling; and a nonzero result with errno 0
        became OSError(0, 'Success').

        Extracting a binding and leaving the old wrapper behind creates TWO
        contracts for one syscall — the shape where a fix lands in one copy
        and the other keeps the defect. This pins that they are ONE."""
        import ctypes as _ctypes
        from helm.configs import _io as configs_io
        # (a) unavailable: an OSError, never an AttributeError
        with mock.patch.object(fsops, "_RENAMEAT2", None):
            with self.assertRaises(OSError) as caught:
                configs_io._renameat2(self.dfd, "a", "b", 1)
        self.assertTrue(caught.exception.errno,
                        "configs reported an unavailable syscall with errno 0")
        # (b) a failure it cannot name is still a failure
        with mock.patch.object(fsops, "_RENAMEAT2", lambda *a, **k: -1), \
                mock.patch.object(_ctypes, "get_errno", lambda: 0):
            with self.assertRaises(OSError) as caught2:
                configs_io._renameat2(self.dfd, "a", "b", 1)
        self.assertTrue(caught2.exception.errno,
                        "configs surfaced OSError(0, 'Success') again")
        # (c) UNCONDITIONAL POSITIVE CONTROL: real behaviour is unchanged —
        # a genuine collision is still EEXIST and the stranger survives.
        with self.assertRaises(FileExistsError):
            configs_io._renameat2(self.dfd, "a", "b", 1)
        with open(self.b) as fh:
            self.assertEqual(fh.read(), "STRANGER")
        # and the monkeypatch seam its own tests rely on is intact
        from helm import configs as configs_pkg
        self.assertTrue(hasattr(configs_pkg, "_renameat2"),
                        "the configs._renameat2 seam disappeared")

    def test_a_REAL_no_clobber_collision_is_still_distinguishable(self):  # noqa: VACUOUS_ASSERTION — this arm IS the unconditional positive control for the two above it; requiring it to carry its own inverts what it exists for. Its assertions are presences: a genuine collision must be a FileExistsError, the stranger's bytes must be intact, and a free-name move must SUCCEED (assertIsNone is that success contract)
        """UNCONDITIONAL POSITIVE CONTROL on the same observable: with nothing
        injected, a genuine collision must come back as a FileExistsError —
        so the two arms above measure the unnameable-failure path rather than
        a wrapper that calls everything an error."""
        err = fsops.rename_noreplace(self.dfd, b"a", self.dfd, b"b")
        self.assertIsInstance(err, FileExistsError,
                              "a real collision is no longer distinguishable "
                              "from any other failure: %r" % (err,))
        with open(self.b) as fh:
            self.assertEqual(fh.read(), "STRANGER")
        # and a move onto a FREE name still succeeds
        self.assertIsNone(fsops.rename_noreplace(self.dfd, b"a", self.dfd,
                                                 b"c"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "c")))


class TheCapabilityCensusIsDERIVEDNotRecalledTest(unittest.TestCase):
    """The item 11 — I CHECKED ONE DIRECTION OF A TWO-DIRECTIONAL QUESTION.

    The census was hand-maintained. When I "derived" it I printed the os.*
    calls taking a dir_fd against the declared list and confirmed nothing was
    MISSING — never asking whether anything was SURPLUS. It required
    descriptor forms of rename, unlink and rmdir that this module has no
    executable use of.

    Surplus is not the harmless direction. A census that over-claims REFUSES
    the whole sweep on hosts that are perfectly capable of running it, so an
    invented requirement strands rows exactly as surely as a forgotten one —
    on the machines least able to spare it. Both directions are asserted."""

    @staticmethod
    def _used():
        import ast as _ast
        src = open(os.path.join(os.path.dirname(todos.__file__),
                                "todos.py")).read()
        used = set()
        for n in _ast.walk(_ast.parse(src)):
            if (isinstance(n, _ast.Call)
                    and isinstance(n.func, _ast.Attribute)
                    and isinstance(n.func.value, _ast.Name)
                    and n.func.value.id == "os"
                    and any(k.arg in ("dir_fd", "src_dir_fd", "dst_dir_fd")
                            for k in n.keywords)):
                used.add(n.func.attr)
        return used

    def test_the_census_matches_the_call_sites_EXACTLY(self):
        used = self._used()
        declared = set(todos._NEED_DIR_FD)
        self.assertTrue(used, "no descriptor-relative os calls found at all — "
                              "the scan is looking in the wrong place")
        self.assertEqual(sorted(used - declared), [],
                         "a primitive is CALLED with a dir_fd but not "
                         "censused, so the preflight says safe and the "
                         "mutation raises")
        self.assertEqual(sorted(declared - used), [],
                         "a primitive is REQUIRED but never called that way, "
                         "so a capable host is refused for a capability the "
                         "code does not use")

    def test_a_MISSING_requirement_and_a_SURPLUS_one_are_both_caught(self):
        """The instrument, proven in both directions on synthetic input —
        because a checker that can only see one direction is what produced the
        finding."""
        used, declared = {"link", "mkdir"}, {"link", "mkdir"}
        self.assertEqual(sorted(used - declared), [])
        self.assertEqual(sorted(declared - used), [])
        self.assertEqual(sorted({"link", "stat"} - declared), ["stat"],
                         "the missing-direction check does not detect a gap")
        self.assertEqual(sorted(declared | {"rmdir"}) and
                         sorted((declared | {"rmdir"}) - used), ["rmdir"],
                         "the surplus-direction check does not detect an "
                         "invented requirement")



class RealCrossDeviceDurabilityOrderingTest(unittest.TestCase):
    """The item 12 — the ordering was pinned on a FORCED EXDEV.

    Two arms existed and neither covered this: the real /dev/shm to /tmp arm
    checked uninterrupted success without watching fsync order, and the
    ordering arm forced EXDEV while the claim and undo both sat on ONE
    filesystem. So a mutant that syncs the undo directory only when it shares
    st_dev with the claim passed both — skipping the durability barrier in
    exactly the case that has one, because same-device is the case where the
    barrier matters least.

    This uses genuinely different devices (/dev/shm and /tmp, measured
    distinct at setUp) and watches the real order."""

    SID = "sess-realxdev"

    def setUp(self):
        if not os.access("/dev/shm", os.W_OK):
            self.skipTest("/dev/shm is not writable on this host")
        if os.stat("/dev/shm").st_dev == os.stat("/tmp").st_dev:
            self.skipTest("/dev/shm and /tmp are the same device here")
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        # THE ROWS ON ONE DEVICE, THE UNDO ON ANOTHER — which is exactly the
        # layout that produced the EXDEV round earlier in this lane, since the
        # two roots come from two different environment variables.
        self.rows_tmp = tempfile.mkdtemp(prefix="helm-xdev-rows-", dir="/tmp")
        self.state_tmp = tempfile.mkdtemp(prefix="helm-xdev-state-",
                                          dir="/dev/shm")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.rows_tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.state_tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.rows_tmp, ignore_errors=True)
        shutil.rmtree(self.state_tmp, ignore_errors=True)

    def test_the_undo_directory_is_synced_before_the_terminal_state(self):  # noqa: VACUOUS_ASSERTION — every assertion is a required PRESENCE or an ORDER: the removal must succeed, an undo location must be reported, the fixture must be genuinely cross-device, the undo directory's sync must APPEAR, the terminal write must appear, and the sync must precede it. Nothing here asserts an absence
        full = os.path.join(self.d, "1.json")
        row = {"id": "1", todos.STAMP: "task/424241", "subject": "OURS"}
        with open(full, "w") as fh:
            json.dump(row, fh)
        order, real_fsync, real_mark = [], os.fsync, todos._mark_residual

        def note_fsync(fd):
            try:
                st = os.fstat(fd)
                order.append(("fsync", st.st_dev, st.st_ino))
            except OSError:
                pass
            return real_fsync(fd)

        def note_terminal(why, held, qdir_fd):
            order.append(("terminal", None, None))
            return real_mark(why, held, qdir_fd)

        rep = {"removed": [], "failed": []}
        with mock.patch.object(os, "fsync", note_fsync), \
                mock.patch.object(todos, "_mark_residual", note_terminal):
            err = todos._trash(self.SID, full, row, rep)
        self.assertIsNone(err, "the real cross-device removal refused: %r"
                               % (err,))
        undo = _undo_of(rep, "1.json")
        self.assertTrue(undo, "no undo location reported: %r" % (rep,))
        # THE DEVICES REALLY DIFFER — the arm asserts its own fixture, so it
        # cannot quietly stop being a cross-device test.
        self.assertNotEqual(os.stat(os.path.dirname(undo)).st_dev,
                            os.stat(self.d).st_dev,
                            "fixture: the undo and the rows are on ONE "
                            "device, so this is not the cross-device case")
        want = os.stat(os.path.dirname(undo))
        dest_id = ("fsync", want.st_dev, want.st_ino)
        self.assertIn(dest_id, order,
                      "the undo directory on the OTHER device was never "
                      "synced; a terminal state on an unsynced entry records "
                      "a decision that device has not made: %r" % (order,))
        self.assertIn(("terminal", None, None), order,
                      "the terminal state was never written — arm is vacuous")
        self.assertLess(order.index(dest_id),
                        order.index(("terminal", None, None)),
                        "the claim went terminal BEFORE its cross-device undo "
                        "entry was durable: %r" % (order,))


class UnavailableIsNeverReportedAsAbsentTest(unittest.TestCase):
    """The items 6 and 7 — cured and probed, but nothing pinned them.

    The author's own cross-check against the census caught that, which is the
    argument for doing the count rather than trusting the impression of
    having done the work."""

    SID = "sess-unavail"

    def setUp(self):
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_HOME", "CLAUDE_CONFIG_DIR")}
        self.tmp = tempfile.mkdtemp(prefix="helm-unavail-")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.d = todos.personal_dir(self.SID)
        os.makedirs(self.d, exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _claim(self):
        q = _mint_claim(self.d, self.SID)
        with open(os.path.join(q, todos.CLAIM_META), "w") as fh:
            json.dump({"claim_id": os.path.basename(q), "sid": self.SID,
                       "row": "1.json", "payload": "1.json"}, fh)
        return q

    def test_UNAVAILABLE_is_never_reported_as_no_claims(self):
        """Item 6. Returning [] says 'this session has no crashed claims',
        which is a POSITIVE finding about the disk — and it was returned for
        'I could not look'. A caller cannot tell those apart, and a claim
        nobody can see is the failure recovery exists to prevent."""
        self._claim()
        # (a) the capability cannot be opened
        with mock.patch.object(todos, "_dir_fd", lambda p: None):
            out = todos.recover_claims(self.SID, apply=False)
        self.assertTrue(out, "an unopenable task directory reported NO CLAIMS")
        self.assertEqual([o.get("outcome") for o in out], ["unreadable"], out)
        # (b) enumeration itself fails
        real = todos.os.listdir

        def blind(x, *a, **k):
            if isinstance(x, int):
                raise OSError(errno.EIO, "simulated enumeration failure")
            return real(x, *a, **k)

        with mock.patch.object(todos.os, "listdir", blind):
            out2 = todos.recover_claims(self.SID, apply=False)
        self.assertTrue(out2, "a failed enumeration reported NO CLAIMS")
        self.assertEqual([o.get("outcome") for o in out2], ["unreadable"], out2)
        # UNCONDITIONAL POSITIVE CONTROL: genuine absence still answers empty,
        # so the arm measures the collapse rather than a recovery that reports
        # unreadable about everything.
        shutil.rmtree(self.d)
        self.assertEqual(todos.recover_claims(self.SID, apply=False), [],
                         "a session with no task directory at all must "
                         "report an empty list, not an error")

    def test_an_unopenable_claim_never_names_a_stranger(self):
        """Item 7. The unopenable branch built its path from the enumerated
        pathname and returned it directly, bypassing the member-before-parent
        door — so a renamed-and-reused personal directory made the report
        resolve to a REPLACEMENT claim."""
        q = self._claim()
        name = os.path.basename(q)
        real, swapped = todos._open_member, []

        def fail_and_swap(dir_fd, n, flags=None):
            if (not swapped and n.startswith(todos.CLAIM_PREFIX)
                    and flags is not None):
                swapped.append(n)
                os.rename(self.d, self.d + ".ours")
                os.makedirs(self.d)
                os.makedirs(os.path.join(self.d, n))     # a STRANGER
                with open(os.path.join(self.d, n, "theirs.json"), "w") as fh:
                    fh.write("{}")
                return None                              # and the open fails
            return real(dir_fd, n, flags)

        with mock.patch.object(todos, "_open_member", fail_and_swap):
            out = todos.recover_claims(self.SID, apply=False)
        self.assertEqual(len(swapped), 1, "the swap never armed — vacuous")
        self.assertTrue(out, "the unopenable claim was skipped silently")
        r = out[0]
        self.assertEqual(r.get("outcome"), "unreadable")
        self.assertEqual(r.get("claim_id"), name,
                         "the stable claim ID was not reported")
        if r.get("claim"):
            self.assertIn(".ours", r["claim"],
                          "the report names the STRANGER's claim at the "
                          "reused pathname: %r" % (r["claim"],))
        else:
            self.assertTrue(r.get("claim_unknown"),
                            "the path was dropped without saying so")
        # THE STRANGER IS UNTOUCHED.
        self.assertTrue(os.path.exists(
            os.path.join(self.d, name, "theirs.json")))
