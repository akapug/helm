#!/usr/bin/env python3
"""helm chat — RAM-room groupchat tests. Hermetic: HELM_CHAT_DIR + HELM_HOME
are tmp dirs (read through home.env at call time); the real /dev/shm/helm-chat
and ~/.helm are never touched. --follow itself is interactive and untested;
the polling read primitive it loops on (chat.read) is pinned here."""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, home, projscope, reflex  # noqa: E402
from tests._tmphome import declare as _tmp_declare  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_EVENT_DIR", "MELD_CHAT_EVENT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_SCRATCH_GC", "HELM_CACHE_DIR")


class TwoBodiesRefusalTest(unittest.TestCase):
    """`chat post` with BOTH a positional message and stdin content must
    refuse — choosing either silently discards the other. The discriminator
    is CONTENT: scripted callers (timers, hooks) post positionally with stdin
    at /dev/null, and refusing on mere non-tty-ness would break all of them.
    ASSERTS each pole's rc AND what actually landed in the room file."""

    def setUp(self):
        import tempfile, shutil
        self.tmp = tempfile.mkdtemp(prefix="helm-test-twobody-")
        self.prior = {k: os.environ.get(k) for k in
                      ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
                       "HELM_CHAT_NAME")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "h")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "c")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "probe-seat"
        self.addCleanup(lambda: [os.environ.update({k: v}) if v is not None
                                 else os.environ.pop(k, None)
                                 for k, v in self.prior.items()])
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmp, ignore_errors=True))

    def _post(self, argv, stdin_text=None):
        import subprocess
        helm = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bin", "helm")
        p = subprocess.run([helm, "chat", "post", "--room", "r"] + argv,
                           input=stdin_text, capture_output=True, text=True,
                           env=os.environ.copy(), timeout=30)
        return p.returncode, p.stdout + p.stderr

    def _rows(self):
        import json
        path = os.path.join(os.environ["HELM_CHAT_DIR"], "r.jsonl")
        if not os.path.exists(path):
            return []
        return [json.loads(l)["text"] for l in open(path) if l.strip()]

    def test_both_bodies_refuse_and_nothing_posts(self):
        rc, out = self._post(["positional body"], stdin_text="heredoc body\n")
        self.assertEqual(rc, 2, out)
        self.assertIn("REFUSING", out)
        self.assertEqual(self._rows(), [],
                         "a refusal must not post either body")

    def test_stdin_only_posts_the_stdin_body(self):
        rc, out = self._post([], stdin_text="stdin body\n")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._rows(), ["stdin body"])

    def test_scripted_positional_with_devnull_stdin_still_posts(self):
        """The pole the first cut broke: every timer and hook posts
        positionally with stdin at /dev/null — non-tty, instant EOF, no
        content. Refusing on the fd's SHAPE would have broken all of them."""
        rc, out = self._post(["scripted positional"], stdin_text="")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._rows(), ["scripted positional"])


class ChatBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-chat-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_EVENT_DIR"] = os.path.join(self.tmp, "chat-events")
        # SET-BUT-EMPTY disables the signed transport — v1 behavior, hermetic
        # even when a real room node is live on this machine
        os.environ["HELM_CHAT_NODE_URL"] = ""
        # this module drives the stop-guard hook, whose silent-mechanical lane
        # runs the scratch reaper — a real DELETE under /tmp/claude-*. A test
        # never mutates a harness store (tests/test_scratch.py pins this).
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        # cwd hermeticity: the default room resolves through seats.
        # resolve_homing, which derives a project room from a git cwd — run
        # from tmp (not the helm checkout) so defaults stay 'main'
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, args, stdin_text=""):
        out, err = io.StringIO(), io.StringIO()
        stdin_prior = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = chat.cmd_chat(list(args))
        finally:
            sys.stdin = stdin_prior
        return rc, out.getvalue(), err.getvalue()


#: The rooms every arm below plants. A MODULE CONSTANT rather than a local:
#: an expected value a reader cannot resolve to a literal is a value that
#: could be empty, and an arm comparing against one proves nothing.
PLANTED_ROOMS = ["alpha", "beta", "gamma"]


class ListRoomsIsOneGetdentsPerPassTest(ChatBase):
    """The room list is a FLAT-DIRECTORY SCAN and its cost is not in the rooms.

    Measured on the live bus: 98,518 directory entries, of which 50,058 are
    `.lock` and 450 are rooms — so one call reads ninety-eight thousand
    dirents to answer a question about four hundred, and the number that
    drives it is one no caller can see from the signature. `helm chat seats`
    asked it once per roster row, 39 times, for 2.8s of a 4.2s screen: ONE
    distinct question.

    AND THE LIFETIME IS THE HAZARD, which is why the memo is projscope's and
    not a module-global dict. A room is born the first time anything posts to
    it, so a cache that outlives the pass makes a NEW ROOM INVISIBLE for as
    long as the process lives — the stale-surface class, arriving through the
    door labelled optimisation. Every arm below is about the lifetime as much
    as the count."""

    def _plant(self, rooms=PLANTED_ROOMS, locks=12):
        d = chat.chat_dir()
        os.makedirs(d, exist_ok=True)
        for r in rooms:
            with open(os.path.join(d, r + ".jsonl"), "a", encoding="utf-8"):
                pass
        # THE DIRENTS THAT ARE NOT ROOMS ARE THE COST, so the fixture carries
        # them: a scan counted in ROOMS would look cheap at any lock count.
        for i in range(locks):
            with open(os.path.join(d, "lock-%d.lock" % i), "a",
                      encoding="utf-8"):
                pass
        return sorted(rooms)

    def _counted(self):
        """(counter, patch) — every getdents against the chat directory."""
        seen = []
        real = os.listdir

        def spy(path=".", *a, **kw):
            if os.path.abspath(str(path)) == os.path.abspath(chat.chat_dir()):
                seen.append(str(path))
            return real(path, *a, **kw)

        return seen, mock.patch.object(os, "listdir", spy)

    def test_four_calls_are_four_scans_bare_and_ONE_scan_in_a_pass(self):
        """BOTH ARMS IN ONE METHOD, ONE OBSERVABLE. The bare count is the
        unconditional positive control: without it, a `list_rooms` that had
        stopped scanning at all — or a fixture whose directory the spy never
        matched — would satisfy the scoped assertion perfectly."""
        self.assertEqual(["alpha", "beta", "gamma"], self._plant(),
                         "control: the fixture planted rooms this scan can "
                         "actually find")
        seen, patch = self._counted()
        with patch:
            bare = [chat.list_rooms() for _ in range(4)]
            unscoped = len(seen)
            del seen[:]
            with projscope.scope():
                inside = [chat.list_rooms() for _ in range(4)]
            scoped = len(seen)
        self.assertEqual(unscoped, 4,
                         "control: outside a scope the memo is inert by "
                         "contract, so four calls are four scans")
        self.assertEqual(scoped, 1,
                         "one pass asks the directory once")
        four = [["alpha", "beta", "gamma"]] * 4
        self.assertEqual(four, bare)  # noqa: VACUOUS_ASSERTION — the expected value is a repeated LIST LITERAL, which this rung cannot resolve through the repetition; the unconditional positives on the same observables are the scan counts asserted at 4 and 1 above
        self.assertEqual(four, inside,  # noqa: VACUOUS_ASSERTION — same repeated-literal shape as the line above, same positives
                         "and the memoised answer is the SAME answer")

    def test_a_room_born_after_the_pass_is_seen_by_the_NEXT_pass(self):
        """THE TRAP THIS MEMO IS ONE STEP AWAY FROM. A room list is a fact
        about NOW. Inside one pass a later read answering from an earlier one
        is CORRECT — that is what one instant means — but a cache that
        survived the pass would hide the new room from every later read in the
        process, and a `helm chat post` to it would then look like a post to
        nowhere.

        Both halves are asserted, so neither a memo that never caches nor one
        that caches forever can pass."""
        self.assertEqual(["alpha", "beta", "gamma"], self._plant())
        with projscope.scope():
            first = chat.list_rooms()
            self._plant(rooms=("delta",), locks=0)
            self.assertEqual(["alpha", "beta", "gamma"], first)
            self.assertEqual(["alpha", "beta", "gamma"],
                             chat.list_rooms(),
                             "inside ONE pass the list is ONE instant")
        after = chat.list_rooms()
        self.assertIn("delta", after,
                      "the memo must not outlive the pass that opened it")
        self.assertEqual(["alpha", "beta", "delta", "gamma"], after)

    def test_each_caller_gets_its_own_list_not_the_cached_one(self):
        """Callers OWN their result — `seats_mute` builds a set from it, other
        readers bind it to a local — so handing two callers in one pass the
        same mutable object would make one caller's edit the other's input.
        The control is the third read, taken after the mutation: it is still
        the real answer."""
        self.assertEqual(["alpha", "beta", "gamma"], self._plant(),
                         "control: there is a real answer to corrupt")
        with projscope.scope():
            mine = chat.list_rooms()
            theirs = chat.list_rooms()
            mine.append("not-a-room")
            self.assertEqual(["alpha", "beta", "gamma"], theirs)
            self.assertEqual(["alpha", "beta", "gamma"],
                             chat.list_rooms())
        self.assertIsNot(mine, theirs)


class _ChatClock:
    """chat.py's OWN `time`: the keyed lock's clock and nap are doubles, and
    every other attribute is the real module.

    `chat.time` IS the process-wide time module, so a double patched onto it
    answers for EVERY caller in the process. post() spawns git before it
    reaches the keyed lock (`rev-parse` for the home room, `config` for the
    owner name), and the stdlib reaps each child in Popen._wait with a capped
    backoff (1 ms doubling to 50 ms) through that same `time.sleep`. Under CPU
    load the child is often not yet reapable when its pipes close, and those
    naps landed on the test's `sleep`: 110 of 200 loaded runs failed with
    `Called N times`, and every call came from Popen._wait (task/3039).
    Binding the doubles to chat's module global scopes them to the code the
    claim is about. It narrows no bound: the planted instants are the same."""

    def __init__(self, *instants):
        self.monotonic = mock.Mock(side_effect=instants)
        self.sleep = mock.Mock()

    def __getattr__(self, name):
        return getattr(time, name)


class RoomTest(ChatBase):
    def test_post_read_roundtrip_and_since(self):
        chat.post("first", who="a1")
        chat.post("second", who="a2")
        msgs, total = chat.read()
        self.assertEqual(total, 2)
        self.assertEqual([m["text"] for m in msgs], ["first", "second"])
        for m in msgs:
            self.assertEqual(sorted(m), ["from", "id", "text", "ts"])
            self.assertEqual(len(m["id"]), 12)   # the stable per-row id (H5)
        tail, total = chat.read(since=1)
        self.assertEqual(total, 2)
        self.assertEqual([m["text"] for m in tail], ["second"])
        self.assertEqual(chat.read(since=2), ([], 2))  # caught up

    def test_post_event_id_is_idempotent(self):
        first = chat.post("first rendering", who="a1", sign=False,
                          event_id="refusal-event-123")
        retried = chat.post("retry rendering changed", who="a1", sign=False,
                            event_id="refusal-event-123")
        rows, total = chat.read()
        self.assertEqual(total, 1)
        self.assertEqual(rows, [first])
        self.assertEqual(retried, first)
        self.assertEqual(first["text"], "first rendering")

    def test_event_id_uses_the_canonical_room_destination(self):
        first = chat.post("one event", room="Team Alpha", who="a1",
                          sign=False, event_id="refusal-event-room")
        retried = chat.post("retry", room="team.alpha", who="a1",
                            sign=False, event_id="refusal-event-room")
        rows, total = chat.read("team-alpha")
        self.assertEqual(total, 1)
        self.assertEqual(rows, [first])
        self.assertEqual(retried, first)

    def test_event_receipts_are_scoped_to_the_chat_bus(self):
        bus_one = os.path.join(self.tmp, "bus-one")
        bus_two = os.path.join(self.tmp, "bus-two")
        os.environ["HELM_CHAT_DIR"] = bus_one
        first = chat.post("first bus", who="a1", sign=False,
                          event_id="refusal-event-bus")
        first_receipt = chat._event_receipt_path("main")

        os.environ["HELM_CHAT_DIR"] = bus_two
        second = chat.post("second bus", who="a1", sign=False,
                           event_id="refusal-event-bus")
        second_receipt = chat._event_receipt_path("main")
        self.assertNotEqual(first_receipt, second_receipt)
        self.assertEqual(chat.read("main")[0], [second])

        os.environ["HELM_HOME"] = os.path.join(self.tmp, "other-home")
        os.environ["HELM_CHAT_DIR"] = bus_one
        self.assertEqual(chat._event_receipt_path("main"), first_receipt)
        self.assertEqual(chat.post("retry", who="a1", sign=False,
                                   event_id="refusal-event-bus"), first)

    def test_empty_event_root_override_uses_host_durable_root(self):
        os.environ["HELM_CHAT_EVENT_DIR"] = ""
        os.environ["HELM_CHAT_DIR"] = ""
        durable = os.path.join(self.tmp, "durable-home")
        with mock.patch.object(home, "default_home", return_value=durable):
            path = chat._event_receipt_path("main")
        self.assertTrue(path.startswith(os.path.join(
            durable, home.GLOBAL, ".state", "chat-event-receipts") + os.sep))

    def test_same_bus_mixed_provenance_shares_one_receipt_root(self):
        os.environ["HELM_CHAT_DIR"] = ""
        bus = chat.chat_dir()
        first = chat.post("first rendering", who="a1", sign=False,
                          event_id="refusal-event-mixed-provenance")
        first_receipt = chat._event_receipt_path("main")
        with mock.patch.object(chat, "SIZE_CAP", 400):
            for i in range(20):
                chat.post("ordinary-%02d" % i, who="a2", sign=False)
        self.assertNotIn(first["id"], {row.get("id")
                                      for row in chat.read()[0]})
        shutil.rmtree(bus)

        os.environ["HELM_HOME"] = os.path.join(self.tmp, "other-home")
        os.environ["HELM_CHAT_DIR"] = bus
        self.assertEqual(chat._event_receipt_path("main"), first_receipt)
        retried = chat.post("retry rendering", who="a1", sign=False,
                            event_id="refusal-event-mixed-provenance")
        self.assertEqual(retried, first)
        self.assertEqual(chat.read(), ([], 0))

    def test_explicit_derived_bus_owner_is_not_state_dependent(self):
        os.environ.pop("HELM_CHAT_EVENT_DIR")
        estate = os.path.join(self.tmp, "fresh-estate")
        durable = os.path.join(self.tmp, "durable-home")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "other-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(estate, "helm-chat")
        with mock.patch.object(home, "default_home", return_value=durable):
            before = chat._event_receipt_path("main")
            self.assertFalse(os.path.exists(os.path.join(estate, home.GLOBAL)))
            os.makedirs(os.path.join(estate, home.GLOBAL))
            after = chat._event_receipt_path("main")
        self.assertEqual(after, before)
        self.assertTrue(after.startswith(os.path.join(
            durable, home.GLOBAL, ".state", "chat-event-receipts") + os.sep))

    def test_explicit_chat_surface_uses_a_durable_bus_keyed_root(self):
        os.environ.pop("HELM_CHAT_EVENT_DIR")
        durable = os.path.join(self.tmp, "durable-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join("/volatile", "chat-bus")
        with mock.patch.object(home, "default_home", return_value=durable):
            path = chat._event_receipt_path("main")
        self.assertTrue(path.startswith(os.path.join(
            durable, home.GLOBAL, ".state", "chat-event-receipts") + os.sep))
        self.assertFalse(path.startswith(os.environ["HELM_CHAT_DIR"]))

    def test_explicit_tmpfs_helm_chat_uses_durable_default_root(self):
        os.environ.pop("HELM_CHAT_EVENT_DIR")
        os.environ["HELM_CHAT_DIR"] = "/dev/shm/team/helm-chat"
        durable = os.path.join(self.tmp, "durable-home")
        with mock.patch.object(home, "default_home", return_value=durable):
            path = chat._event_receipt_path("main")
        self.assertTrue(path.startswith(os.path.join(
            durable, home.GLOBAL, ".state", "chat-event-receipts") + os.sep))
        self.assertFalse(path.startswith("/dev/shm/"))

    def test_event_receipt_files_are_private(self):
        configured = os.environ["HELM_CHAT_EVENT_DIR"]
        os.makedirs(configured, mode=0o755)
        os.chmod(configured, 0o755)
        chat.post("private event", who="a1", sign=False,
                  event_id="refusal-event-private")
        path = chat._event_receipt_path("main")
        self.assertEqual(stat.S_IMODE(os.stat(configured).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode),
                         0o700)

    def test_event_receipt_commit_fsyncs_file_and_directory(self):
        with mock.patch.object(os, "fsync", wraps=os.fsync) as fsync:
            chat.post("durable event", who="a1", sign=False,
                      event_id="refusal-event-fsync")
        self.assertGreaterEqual(fsync.call_count, 2)

    def test_post_replace_fsync_failure_keeps_row_and_receipt(self):
        os.makedirs(chat._event_receipts_dir(), mode=0o700)
        with mock.patch.object(os, "fsync",
                               side_effect=(None, OSError("dir fsync"))), \
                self.assertRaisesRegex(chat._EventReceiptPublishedError,
                                       "durability is unproven"):
            chat.post("first rendering", who="a1", sign=False,
                      event_id="refusal-event-post-replace")
        rows, total = chat.read()
        self.assertEqual(total, 1)
        self.assertTrue(os.path.exists(chat._event_receipt_path("main")))
        retried = chat.post("retry rendering", who="a1", sign=False,
                            event_id="refusal-event-post-replace")
        self.assertEqual(retried, rows[0])
        self.assertEqual(chat.read(), (rows, 1))

    def test_event_receipt_write_failure_rolls_back_row(self):
        ordinary = chat.post("kept", who="a2", sign=False)
        with mock.patch.object(chat, "_write_event_receipts",
                               side_effect=OSError("read-only")), \
                self.assertRaisesRegex(OSError, "read-only"):
            chat.post("must retry", who="a1", sign=False,
                      event_id="refusal-event-write-failed")
        self.assertEqual(chat.read(), ([ordinary], 1))

    def test_receipt_failure_never_truncates_fail_open_append(self):
        concurrent = {"ts": "2026-08-05T00:00:00Z", "from": "a2",
                      "text": "concurrent", "id": "222222222222"}

        def fail_after_concurrent_append(_path, _receipts):
            with open(chat.room_path("main"), "ab") as f:
                f.write((json.dumps(concurrent) + "\n").encode("utf-8"))
            raise OSError("read-only")

        with mock.patch.object(chat, "_write_event_receipts",
                               side_effect=fail_after_concurrent_append), \
                self.assertRaisesRegex(OSError, "could not be rolled back"):
            chat.post("keyed", who="a1", sign=False,
                      event_id="refusal-event-fail-open-race")
        rows, total = chat.read()
        self.assertEqual(total, 2)
        self.assertEqual([row["text"] for row in rows], ["keyed", "concurrent"])

        retried = chat.post("retry", who="a1", sign=False,
                            event_id="refusal-event-fail-open-race")
        self.assertEqual(retried, rows[0])
        self.assertEqual(chat.read(), (rows, 2))

    def test_event_receipt_migrates_released_ram_path(self):
        first = chat.post("before upgrade", who="a1", sign=False,
                          event_id="refusal-event-upgrade")
        durable = chat._event_receipt_path("main")
        legacy = chat._legacy_event_receipt_path("main")
        os.replace(durable, legacy)
        with mock.patch.object(chat, "SIZE_CAP", 400):
            for i in range(20):
                chat.post("ordinary-%02d" % i, who="a2", sign=False)
        self.assertNotIn(first["id"], {row.get("id")
                                      for row in chat.read()[0]})

        retried = chat.post("retry after upgrade", who="a1", sign=False,
                            event_id="refusal-event-upgrade")
        self.assertEqual(retried, first)
        self.assertTrue(os.path.exists(durable))
        self.assertFalse(os.path.exists(legacy))
        self.assertNotIn("retry after upgrade",
                         [row.get("text") for row in chat.read()[0]])

    def test_event_receipt_corruption_fails_closed(self):
        path = chat._event_receipt_path("main")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("not-json\n")
        with self.assertRaisesRegex(ValueError, "duplicate status is unknown"):
            chat.post("must retry", who="a1", sign=False,
                      event_id="refusal-event-corrupt-receipt")
        self.assertEqual(chat.read(), ([], 0))

    def test_event_receipt_survives_room_rotation(self):  # noqa: VACUOUS_ASSERTION — the fixture proves the first row existed and was actually rotated before asserting the retry appended nothing
        with mock.patch.object(chat, "SIZE_CAP", 400):
            first = chat.post("event before rotation", who="a1", sign=False,
                              event_id="refusal-event-rotation")
            for i in range(20):
                chat.post("ordinary-%02d" % i, who="a2", sign=False)
        rows, before = chat.read()
        self.assertNotIn(first["id"], {row.get("id") for row in rows},
                         "fixture did not rotate the event row out")
        retried = chat.post("retry after rotation", who="a1", sign=False,
                            event_id="refusal-event-rotation")
        rows, after = chat.read()
        self.assertEqual(after, before)
        self.assertEqual(retried, first)
        self.assertNotIn("retry after rotation",
                         [row.get("text") for row in rows])

    def test_event_post_refuses_when_room_lock_cannot_prove_uniqueness(self):  # noqa: VACUOUS_ASSERTION — the exact raised refusal positively proves the keyed append path executed before the untouched-room assertion
        @contextlib.contextmanager
        def unlocked(_room, timeout_s=None):
            self.assertEqual(timeout_s, 5)
            yield False

        with mock.patch.object(chat, "_room_lock", unlocked), \
                self.assertRaisesRegex(OSError, "unproven idempotent append"):
            chat.post("must retry", who="a1", sign=False,
                      event_id="refusal-event-locked")
        self.assertEqual(chat.read(), ([], 0))

    def test_keyed_room_lock_retries_then_acquires_before_its_bound(self):
        import fcntl
        calls = []

        def flock(_fd, flags):
            calls.append(flags)
            if len(calls) == 1:
                raise BlockingIOError

        clock = _ChatClock(10.0, 10.1)
        with mock.patch.object(fcntl, "flock", side_effect=flock), \
                mock.patch.object(chat, "time", clock):
            with chat._room_lock("main", timeout_s=5) as locked:
                self.assertTrue(locked)
        clock.sleep.assert_called_once_with(0.05)
        self.assertEqual(clock.monotonic.call_count, 2)  # set, checked once
        self.assertEqual(calls[:2], [fcntl.LOCK_EX | fcntl.LOCK_NB] * 2)
        self.assertEqual(calls[-1], fcntl.LOCK_UN)

    def test_room_lock_OSError_closes_handle_and_degrades_unlocked(self):
        import builtins, fcntl
        handle = mock.MagicMock()
        handle.fileno.return_value = 7
        with mock.patch.object(builtins, "open", return_value=handle), \
                mock.patch.object(fcntl, "flock", side_effect=OSError("planted")):
            with chat._room_lock("main", timeout_s=5) as locked:
                self.assertFalse(locked)
        handle.close.assert_called_once()

    def test_keyed_room_lock_times_out_instead_of_hanging_forever(self):
        # Contend ONLY the keyed room lock: `LOCK_EX|LOCK_NB` is the shape no
        # other writer-path lock uses. Every other flock (the rename-journal
        # proof's directory/roster/journal locks that `_append` takes BEFORE
        # `_room_lock`) runs for real against the fixture dir, so the seat-
        # rename refusal stays armed and passes on its own merit — a blanket
        # BlockingIOError double had been tripping it first.
        import fcntl
        real, calls = fcntl.flock, []

        def flock(fd, flags):
            calls.append(flags)
            if flags & fcntl.LOCK_NB:
                raise BlockingIOError
            return real(fd, flags)

        # The clock and the nap are chat's own (_ChatClock): the git children
        # post() reaps before the keyed lock nap through the stdlib, and those
        # naps are not the wait this arm is about.
        clock = _ChatClock(10.0, 15.1)
        with mock.patch.object(fcntl, "flock", side_effect=flock), \
             mock.patch.object(chat, "time", clock), \
             self.assertRaisesRegex(OSError, "unproven idempotent append"):
            chat.post("must not hang", who="a1", sign=False,
                      event_id="refusal-event-lock-timeout")
        # The deadline, not a nap, ended the wait. The positive control is on
        # the same clock: both planted instants were read (set, then expired),
        # which also proves the scoped doubles are the clock the lock reads.
        self.assertEqual(clock.sleep.call_args_list, [])
        self.assertEqual(clock.monotonic.call_count, 2)
        self.assertIn(fcntl.LOCK_EX | fcntl.LOCK_NB, calls)
        self.assertEqual(chat.read(), ([], 0))  # noqa: VACUOUS_ASSERTION — product law: a refused keyed write appends nothing; the exact refusal above is its positive control

    def test_event_post_refuses_when_room_cannot_prove_absence(self):  # noqa: VACUOUS_ASSERTION — planted malformed bytes and the exact refusal positively control the assertion that no replacement row was written
        path = chat.room_path("main")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("not-json\n")
        with self.assertRaisesRegex(ValueError, "duplicate status is unknown"):
            chat.post("must retry", who="a1", sign=False,
                      event_id="refusal-event-corrupt")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "not-json\n")

    def test_since_past_the_end_resets(self):
        # a rotation shrank the room under a poller — it re-syncs, never starves
        chat.post("only", who="a1")
        msgs, total = chat.read(since=99)
        self.assertEqual((len(msgs), total), (1, 1))

    def test_env_dir_and_room_layout(self):
        chat.post("hi", who="a1")
        self.assertEqual(chat.chat_dir(), os.environ["HELM_CHAT_DIR"])
        path = chat.room_path("main")
        self.assertTrue(path.startswith(os.environ["HELM_CHAT_DIR"]))
        with open(path) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "hi")  # one JSON obj/line
        mode = stat.S_IMODE(os.stat(chat.chat_dir()).st_mode)
        self.assertEqual(mode, 0o700)  # the room dir is the operator's

    def test_from_env_name_wins(self):
        os.environ["HELM_CHAT_NAME"] = "pilot"
        self.assertEqual(chat.post("x")["from"], "pilot")

    def test_unparseable_lines_skipped(self):
        chat.post("good", who="a1")
        with open(chat.room_path("main"), "a") as f:
            f.write("not json\n[1,2]\n")
        msgs, total = chat.read()
        self.assertEqual(total, 1)
        self.assertEqual(msgs[0]["text"], "good")

    def test_rotation_keeps_the_newest_half(self):
        with mock.patch.object(chat, "SIZE_CAP", 400):
            for i in range(20):
                chat.post("msg-%02d" % i, who="a1")
        msgs, total = chat.read()
        self.assertLess(total, 20)                       # oldest half rotated out
        self.assertEqual(msgs[-1]["text"], "msg-19")     # newest kept
        self.assertNotIn("msg-00", [m["text"] for m in msgs])
        self.assertLessEqual(os.path.getsize(chat.room_path("main")), 400)


class ReadFlagGuardTest(ChatBase):
    """guard_tail on `chat read` (the 9ce7b8c precedent) + a real --limit.
    An unknown flag used to be silently ignored: `--limit 1` meant 'the
    newest row' and returned scrollback (16 owner-asks misreported off it,
    2026-07-26; OI misread the room thrice in one session). Effect
    assertions only — an unknown flag produces rc 2; --limit N returns the
    NEWEST N; a react [n] tag stays whole-room so a quote still resolves."""

    def _rows(self, n):
        for i in range(n):
            self.run_cmd(["post", "row-%02d" % i])

    def test_an_unknown_flag_REFUSES_rc2_naming_the_flag(self):
        rc, _o, err = self.run_cmd(["read", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err)

    def test_pre_fix_limit_was_ignored_now_it_is_known(self):
        self._rows(5)
        rc, out, _e = self.run_cmd(["read", "--limit", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("row-03", out)
        self.assertIn("row-04", out)
        self.assertNotIn("row-00", out)   # the newest 2, not scrollback

    def test_limit_1_returns_the_newest_row_not_the_oldest(self):
        self._rows(3)
        rc, out, _e = self.run_cmd(["read", "--limit", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("row-02", out)
        self.assertNotIn("row-00", out)

    def test_limit_tags_stay_whole_room_so_a_react_resolves(self):
        self._rows(4)
        rc, out, _e = self.run_cmd(["read", "--limit", "1"])
        self.assertEqual(rc, 0)
        # the [n] beside the newest row is its whole-room index (4), not 1
        self.assertIn("[4]", out)

    def test_a_non_numeric_limit_refuses_with_the_since_route(self):
        rc, _o, err = self.run_cmd(["read", "--limit", "abc"])
        self.assertEqual(rc, 2)
        self.assertIn("--since", err)

    def test_since_still_works_and_composes_with_limit(self):
        self._rows(6)
        rc, out, _e = self.run_cmd(["read", "--since", "2", "--limit", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("row-04", out)
        self.assertIn("row-05", out)
        self.assertNotIn("row-01", out)

    def test_apply_readers_still_see_the_read_branch(self):
        """The 9ce7b8c trap: a guard placed in the dispatcher would retire
        ApplyReadersAreGuarded's view of the read verb. Placed in the read
        branch, a read with --help still prints usage through the guard."""
        rc, out, _e = self.run_cmd(["read", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("helm chat read", out)


class MarkerTest(ChatBase):
    def test_mark_and_consume_semantics(self):
        chat.post("agents?", who="daria")
        chat.mark_owner_unread()
        mp = chat.marker_path("main")
        with open(mp) as f:
            self.assertEqual(f.read(), "1")  # the count at owner-post time
        self.assertFalse(chat.consume(total=0))   # not consumed past the post
        self.assertTrue(os.path.exists(mp))
        self.assertTrue(chat.consume(total=1))    # seen it -> cleared
        self.assertFalse(os.path.exists(mp))
        self.assertFalse(chat.consume(total=9))   # no marker -> nothing to clear

    def test_cli_read_clears_the_marker(self):
        chat.post("fleet, look alive", who="daria")
        chat.mark_owner_unread()
        rc, out, _ = self.run_cmd(["read"])
        self.assertEqual(rc, 0)
        self.assertIn("daria: fleet, look alive", out)
        self.assertFalse(os.path.exists(chat.marker_path("main")))

    def test_reflex_fires_while_marker_exists_and_not_after(self):
        home.scaffold_global()  # seeds the pack; marker path resolves to tmp
        self.assertEqual(reflex.fire("any turn text"), [])
        chat.post("ship it", who="daria")
        chat.mark_owner_unread()
        fired = [e["id"] for e in reflex.fire("any turn text")]
        self.assertEqual(fired, ["owner-chat-unread"])
        self.run_cmd(["read"])  # the read consumes past the owner's post
        self.assertEqual(reflex.fire("any turn text"), [])


class CmdTest(ChatBase):
    def test_post_and_read_cycle(self):
        rc, out, _ = self.run_cmd(["post", "hello", "crew"])
        self.assertEqual(rc, 0)
        self.assertIn("hello crew", out)
        rc, out, _ = self.run_cmd(["read"])
        self.assertEqual(rc, 0)
        self.assertIn("hello crew", out)

    def test_read_since_flag(self):
        chat.post("one", who="a1")
        chat.post("two", who="a1")
        rc, out, _ = self.run_cmd(["read", "--since", "1"])
        self.assertEqual(rc, 0)
        self.assertNotIn("one", out)
        self.assertIn("two", out)

    def test_rooms_listing(self):
        chat.post("hi", who="a1")
        chat.post("ops talk", room="ops", who="a2")
        chat.mark_owner_unread("ops")
        rc, out, _ = self.run_cmd(["rooms"])
        self.assertEqual(rc, 0)
        self.assertIn("main  1 msg", out)
        self.assertIn("ops  1 msg [owner-unread]", out)

    def test_list_rooms_is_the_one_sorted_source(self):
        self.assertEqual(chat.list_rooms(), [])          # none until a post
        chat.post("hi", who="a1")                        # -> main
        chat.post("ops", room="ops", who="a2")
        chat.post("zeta", room="zeta", who="a3")
        self.assertEqual(chat.list_rooms(), ["main", "ops", "zeta"])  # sorted

    def test_room_flag_scopes_post_and_read(self):
        self.assertEqual(self.run_cmd(["post", "sidebar", "--room", "ops"])[0], 0)
        self.assertEqual(chat.read("main"), ([], 0))
        rc, out, _ = self.run_cmd(["read", "--room", "ops"])
        self.assertIn("sidebar", out)

    def test_default_io_consumes_the_one_homing_resolver(self):
        """The roster-scatter hole codex probed: the SessionStart join homed
        the seat to its project room (seats.resolve_homing, cwd-derived), but
        a no---room `helm chat post` privately defaulted to env-or-'main' and
        wrote ZERO rows to that home. Default chat I/O now consumes the SAME
        resolver: post and read land in the derived project room; --room
        still wins; a project-less cwd (the base-class chdir) keeps main."""
        import subprocess
        from helm import seats
        repo = os.path.join(self.tmp, "proj-a")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True,
                       capture_output=True)
        home_room = seats.derive_home_room(repo)
        self.assertIsNotNone(home_room)         # the join's answer, one truth
        os.chdir(repo)
        rc, out, _ = self.run_cmd(["post", "to", "my", "home"])
        self.assertEqual(rc, 0)
        self.assertIn("[%s]" % home_room, out)
        self.assertEqual(chat.read("main"), ([], 0))    # zero rows to main
        self.assertEqual(chat.read(home_room)[1], 1)    # the home room got it
        rc, out, _ = self.run_cmd(["read"])             # default read: home too
        self.assertIn("to my home", out)
        self.assertEqual(
            self.run_cmd(["post", "aside", "--room", "main"])[0], 0)
        self.assertEqual(chat.read("main")[1], 1)       # --room still beats

    def test_env_room_homes_the_default(self):
        """Team-room homing (slice 3): HELM_CHAT_ROOM re-homes every no---room
        verb — exactly what the hooks call — while an explicit --room still
        wins; un-homed sessions keep main (test_post_and_read_cycle)."""
        os.environ["HELM_CHAT_ROOM"] = "team-x"
        rc, out, _ = self.run_cmd(["post", "homed", "hello"])
        self.assertEqual(rc, 0)
        self.assertIn("[team-x]", out)
        self.assertEqual(chat.read("main"), ([], 0))     # nothing leaked to main
        self.assertEqual(chat.read("team-x")[1], 1)
        rc, out, _ = self.run_cmd(["read"])              # default read: homed too
        self.assertIn("homed hello", out)
        self.assertEqual(self.run_cmd(["post", "aside", "--room", "main"])[0], 0)
        self.assertEqual(chat.read("main")[1], 1)        # --room beats the env
        chat.mark_owner_unread("team-x")                 # a homed read consumes
        self.assertEqual(self.run_cmd(["read"])[0], 0)   # its OWN room's marker
        self.assertFalse(os.path.exists(chat.marker_path("team-x")))

    def test_join_dispatch_preserves_explicit_room_provenance(self):
        os.environ["HELM_CHAT_ROOM"] = "project-a"
        with mock.patch("helm.seats.cmd", return_value=0) as cmd:
            self.assertEqual(chat.cmd_chat(["join", "--room", "main"]), 0)
        cmd.assert_called_once_with(
            "join", [], "main", room_explicit=True, room_source=None)

    def test_join_dispatch_preserves_derived_environment_provenance(self):
        os.environ["HELM_CHAT_ROOM"] = "project-a"
        os.environ["HELM_CHAT_ROOM_SOURCE"] = "derived"
        with mock.patch("helm.seats.cmd", return_value=0) as cmd:
            self.assertEqual(chat.cmd_chat(["join"]), 0)
        cmd.assert_called_once_with(
            "join", [], "project-a", room_explicit=False,
            room_source="derived")

    def test_a_room_that_exists_and_is_empty_is_not_a_room_that_does_not(self):
        """TWO FACTS, ONE RENDERING, and the second one is a confident report
        about a world the reader never reached.

        Three reads of `dm:seat-b` answer "no messages" while three DMs sit
        delivered in the identity-keyed lane, so a reader who trusts the
        sentence concludes that messages were never sent."""
        # AN EXISTING ROOM keeps the ordinary invitation.
        os.makedirs(os.path.dirname(chat.room_path("side-room")),
                    exist_ok=True)
        open(chat.room_path("side-room"), "w").close()
        self.assertIn("no messages", chat.empty_room_line("side-room", "side"))
        self.assertNotIn("NO LIVE ROOM",
                         chat.empty_room_line("side-room", "side"))
        # A NAME WITH NO LIVE ROOM says exactly that -- and says it about the
        # LIVE bus, which is the only thing a stat can answer for.
        line = chat.empty_room_line("never-posted-here", "never-posted-here")
        self.assertIn("NO LIVE ROOM", line)
        self.assertIn("never-posted-here", line)
        self.assertNotIn("nothing has ever been posted", line)

    def test_a_question_that_could_not_be_asked_is_not_an_answered_absence(self):
        """Finding r1 F1: `os.path.exists` answers False for two different
        worlds -- the file is not there, and the question could not be asked.
        A room whose parent is not a directory raises NotADirectoryError from
        the same stat that a missing file answers FileNotFoundError to, and
        reporting the second as absence is a confident report about a world
        the reader never reached."""
        real = chat.dm_room("seat-b")
        os.makedirs(chat.chat_dir(), exist_ok=True)
        # The dm/ namespace's parent is a FILE, so every dm lane's stat fails
        # with an error that is not absence.
        with open(os.path.join(chat.chat_dir(), "dm"), "w") as f:
            f.write("not a directory\n")
        line = chat.empty_room_line(real, "dm")
        self.assertIn("CANNOT SAY", line)
        self.assertIn("UNKNOWN", line)
        self.assertNotIn("NO LIVE ROOM", line)
        # THE CONTROL, unconditional: make the parent a real directory again
        # and the same call answers the ordinary absence.
        os.unlink(os.path.join(chat.chat_dir(), "dm"))
        os.makedirs(os.path.dirname(chat.room_path(real)), exist_ok=True)
        back = chat.empty_room_line(real, "dm")
        self.assertIn("NO LIVE ROOM", back)
        self.assertNotIn("CANNOT SAY", back)

    def test_an_unrestored_journal_is_history_this_read_cannot_see(self):
        """Finding r1 F1, second half: rooms live in RAM and history lives in
        the journal, so an absent file after a reboot says nothing about what
        was EVER posted -- the first list or post restores it. The claim is
        bounded to the restore state this host can actually prove."""
        os.makedirs(chat.chat_dir(), exist_ok=True)
        # NO SENTINEL: nothing has proven the journal was restored here.
        unrestored = chat.empty_room_line("never-posted-here", "n")
        self.assertIn("cannot prove the journal was restored", unrestored)
        self.assertNotIn("has not been restored", unrestored,
                         "an absent sentinel proves UNPROVEN, not that the "
                         "restore did not happen")
        # THE CONTROL: stamp the sentinel this host's own restore path writes
        # and the caveat goes away, so it tracks the state instead of always
        # printing.
        with open(chat._restored_sentinel(), "w"):
            pass
        restored = chat.empty_room_line("never-posted-here", "n")
        self.assertIn("NO LIVE ROOM", restored)
        self.assertNotIn("cannot prove the journal was restored", restored)

    def test_a_lane_that_holds_bytes_but_no_rows_promises_nothing(self):
        """SIZE IS NOT A SUCCESSFUL PARSE. A torn or malformed DM lane holds
        bytes and yields ZERO rows, so a pointer that promises messages on the
        strength of a byte count sends the reader to a file that shows them
        nothing — a zero-byte cure is real and stops one case short."""
        real = chat.dm_room("seat-b")
        os.makedirs(os.path.dirname(chat.room_path(real)), exist_ok=True)
        with open(chat.room_path(real), "w") as f:
            f.write("{not json at all\n")
        line = chat.empty_room_line("dm:seat-b", "dm:seat-b")
        self.assertNotIn("HAS messages", line, line)
        # Finding r3 F1: NOT PROMISING MESSAGES IS NOT ENOUGH -- the parent of
        # this cure said HAS, the r3 cure said "exists and is empty", and both
        # are false about a lane nothing could parse. The pointer must say the
        # third answer, positively.
        self.assertIn("could not be read", line, line)
        self.assertIn("UNKNOWN", line, line)
        self.assertNotIn("exists and is empty", line, line)
        # THE CONTROL, unconditional and on the same lane: one PARSEABLE row
        # and the pointer promises messages again, so the refusal is about
        # the parse and not about a hint that stopped firing.
        with open(chat.room_path(real), "w") as f:
            f.write('{"ts": "2026-09-14T10:00:00Z", "text": "real"}\n')
        self.assertIn("HAS messages",
                      chat.empty_room_line("dm:seat-b", "dm:seat-b"))

    def test_a_room_of_torn_bytes_is_unknown_not_empty(self):
        """Finding r3 F1, the DIRECT room: a nonzero room file whose every line
        is torn yields zero rows through the fail-open reader, and the line
        that follows must not be the ordinary "no messages" invitation."""
        path = chat.room_path("torn-room")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{torn\n")
        line = chat.empty_room_line("torn-room", "torn-room")
        self.assertIn("CANNOT SAY", line, line)
        self.assertIn("torn-rows", line, line)
        self.assertNotIn("no messages", line, line)
        # THE CONTROL: the same bytes followed by a parseable row are a room
        # with a message, and a room that holds only line breaks is empty.
        with open(path, "w") as f:
            f.write('{torn\n{"ts": "2026-09-14T10:00:00Z", "text": "real"}\n')
        self.assertEqual(chat._live_room_state("torn-room"), ("has", ""))
        with open(path, "w") as f:
            f.write("\n\n")
        self.assertEqual(chat._live_room_state("torn-room"), ("empty", ""))

    def test_a_lane_that_stats_but_will_not_open_is_unknown(self):
        """Finding r3 F1, the second concrete state: the stat succeeds and the
        open fails. `read` turns that into ([], 0), the empty lane's answer;
        the stricter door reports the fault and the state is UNREADABLE."""
        real = chat.dm_room("seat-b")
        path = chat.room_path(real)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write('{"ts": "2026-09-14T10:00:00Z", "text": "real"}\n')
        os.chmod(path, 0)
        try:
            # THE PRECONDITION IS ASSERTED, NOT ASSUMED: a reader that can
            # still open a mode-0 file (root) would make this arm a control.
            self.assertFalse(os.access(path, os.R_OK),
                             "this arm needs an unreadable file; the process "
                             "can read mode 0, so it would test nothing")
            state, detail = chat._live_room_state(real)
            self.assertEqual(state, "unreadable", detail)
            line = chat.empty_room_line("dm:seat-b", "dm:seat-b")
            self.assertIn("could not be read", line, line)
            self.assertNotIn("exists and is empty", line, line)
            self.assertNotIn("HAS messages", line, line)
        finally:
            os.chmod(path, 0o600)
        # THE CONTROL: readable again, the same lane HAS its message.
        self.assertEqual(chat._live_room_state(real), ("has", ""))

    def test_a_lane_removed_between_the_stat_and_the_read_is_absent(self):
        """The fault-free empty answer has one more source: `read_checked`
        reports a file that vanished after the stat as a proven-empty room.
        The REAL reader is called; the spy only removes the file first."""
        path = chat.room_path("vanishing")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write('{"ts": "2026-09-14T10:00:00Z", "text": "real"}\n')
        real_read = chat.read_checked
        calls = []

        def removing(room, since=0):
            calls.append(room)
            os.unlink(path)
            return real_read(room, since)

        with mock.patch.object(chat, "read_checked", removing):
            state = chat._live_room_state("vanishing")
        self.assertEqual(calls, [chat.pk.slug("vanishing")])
        self.assertEqual(state, ("absent", ""))
        # THE CONTROL: without the removal the same file HAS its row.
        with open(path, "w") as f:
            f.write('{"ts": "2026-09-14T10:00:00Z", "text": "real"}\n')
        self.assertEqual(chat._live_room_state("vanishing"), ("has", ""))

    def test_a_dotted_seat_is_not_its_dashed_neighbour(self):  # noqa: VACUOUS_ASSERTION — both absences name `dashed`, and the unconditional positive for it is the `empty_room_line("dm:api-a", ...)` call in the middle of this arm, which proves the hint DOES emit that lane when the dashed seat is the one asked for
        """Finding r1 F2: slugging before resolving the seat destroys the
        identity the lane is keyed by. `api.a` and `api-a` are different
        seats with different `_seat_key` digests and different lanes, and
        both slug to `api-a` -- so a hint computed from the slug points a
        reader at the WRONG recipient's lane, in a sentence that reads as
        authoritative."""
        from helm import seats
        self.assertNotEqual(seats._seat_key("api.a"), seats._seat_key("api-a"),
                            "fixture: these two names must be distinct seats "
                            "or this arm is about nothing")
        dashed, dotted = chat.dm_room("api-a"), chat.dm_room("api.a")
        self.assertNotEqual(dashed, dotted)
        os.makedirs(os.path.dirname(chat.room_path(dashed)), exist_ok=True)
        with open(chat.room_path(dashed), "w") as f:
            f.write('{"ts": "2026-09-14T10:00:00Z", "text": "for api-a"}\n')
        line = chat.empty_room_line("dm:api.a", "dm:api.a")
        self.assertNotIn(dashed, line,
                         "the hint sent the reader to the OTHER seat's lane")
        # THE CONTROL FOR THAT ABSENCE, unconditional and on the same door:
        # ASK for the dashed seat and its lane is exactly what gets named, so
        # the absence above is a discrimination between two identities and
        # not a hint that never fires.
        self.assertIn(dashed, chat.empty_room_line("dm:api-a", "dm:api-a"))
        # THE CONTROL, on the same call: give the DOTTED seat a lane with
        # messages and the hint names that one.
        with open(chat.room_path(dotted), "w") as f:
            f.write('{"ts": "2026-09-14T10:01:00Z", "text": "for api.a"}\n')
        named = chat.empty_room_line("dm:api.a", "dm:api.a")
        self.assertIn(dotted, named)
        self.assertNotIn(dashed, named)

    def test_a_renamed_lane_is_followed_to_where_the_messages_are(self):
        """Finding r1 F2, second half: a rename leaves a redirect and the
        messages live at its target, so a hint that stops at the pre-rename
        lane names an empty file and tells the reader nothing is there."""
        was, now = chat.dm_room("seat-b"), chat.dm_room("seat-b-renamed")
        os.makedirs(os.path.dirname(chat.room_path(now)), exist_ok=True)
        with open(chat.room_path(now), "w") as f:
            f.write('{"ts": "2026-09-14T10:02:00Z", "text": "after"}\n')
        with open(chat._dm_redirect_path(was), "w") as f:
            f.write(now + "\n")
        line = chat.empty_room_line("dm:seat-b", "dm:seat-b")
        self.assertIn(now, line, "the hint stopped at the pre-rename lane")
        self.assertIn("HAS messages", line)
        # THE CONTROL: remove the redirect and the hint falls back to the
        # lane the name resolves to, which has nothing -- so the redirect is
        # what moved the answer.
        os.unlink(chat._dm_redirect_path(was))
        self.assertNotIn(now, chat.empty_room_line("dm:seat-b", "dm:seat-b"))
        self.assertIsNotNone(was)

    def test_a_dm_shaped_name_is_answered_with_the_lane_that_exists(self):
        """A DM NAME IS NOT A TYPO, IT IS A SHAPE THAT CANNOT EXIST. `pk.slug`
        folds `dm:seat-b` into the DM namespace, so the reader resolves a real
        path nothing will ever write — `dm_room` keys a lane by seat identity
        and appends a digest. DM lanes stay out of `list_rooms` by design, so
        this line is the only place a reader can be told."""
        real = chat.dm_room("seat-b")
        os.makedirs(os.path.dirname(chat.room_path(real)), exist_ok=True)
        with open(chat.room_path(real), "w") as f:
            f.write('{"ts": "2026-09-13T20:00:00Z", "text": "hi"}\n')
        line = chat.empty_room_line("dm:seat-b", "dm:seat-b")
        self.assertIn("NO LIVE ROOM", line)
        self.assertIn(real, line, "the answer did not name the lane that has "
                                  "the messages, which is the whole cure")
        self.assertIn("HAS messages", line)
        # A LANE THAT EXISTS AND IS EMPTY IS NOT A LANE WITH MESSAGES: the
        # pointer must not promise content a zero-byte file does not hold.
        open(chat.room_path(real), "w").close()
        empty = chat.empty_room_line("dm:seat-b", "dm:seat-b")
        self.assertIn(real, empty)
        self.assertNotIn("HAS messages", empty)
        self.assertIn("exists and is empty", empty)
        # THE CONTROL: with no such lane on disk, there is nothing to point at
        # and the line must NOT invent one.
        os.unlink(chat.room_path(real))
        bare = chat.empty_room_line("dm:seat-b", "dm:seat-b")
        self.assertIn("NO LIVE ROOM", bare)
        self.assertNotIn(real, bare)

    def test_preferred_room_does_not_inherit_legacy_source(self):
        os.environ["HELM_CHAT_ROOM"] = "explicit-new"
        os.environ["MELD_CHAT_ROOM_SOURCE"] = "derived"
        with mock.patch("helm.seats.cmd", return_value=0) as cmd:
            self.assertEqual(chat.cmd_chat(["join"]), 0)
        cmd.assert_called_once_with(
            "join", [], "explicit-new", room_explicit=False,
            room_source=None)

    def test_empty_read_and_bad_args(self):
        rc, out, _ = self.run_cmd(["read"])
        self.assertEqual(rc, 0)
        # THE FIXTURE'S ROOM HAS NO LIVE FILE, which is a different fact from
        # "it is empty" and now says so -- and a different fact again from
        # "nothing was ever posted", which a stat cannot establish.
        self.assertIn("NO LIVE ROOM", out)
        self.assertEqual(self.run_cmd(["post"])[0], 2)            # nothing to say
        self.assertEqual(self.run_cmd(["read", "--since", "x"])[0], 2)
        self.assertEqual(self.run_cmd(["post", "x", "--room"])[0], 2)
        self.assertEqual(self.run_cmd(["bogus"])[0], 2)

    def test_rooms_empty(self):
        rc, out, _ = self.run_cmd(["rooms"])
        self.assertEqual(rc, 0)
        self.assertIn("no rooms yet", out)

    def test_roster_aliases_seats(self):
        """`helm chat roster` is a friendlier spelling of `seats` — it must
        reach the same dispatch (rc 0), never the unknown-subcommand path
        (owner asked for the alias 2026-07-21)."""
        rc, _, err = self.run_cmd(["roster"])
        self.assertEqual(rc, 0)
        self.assertNotIn("unknown subcommand", err)


class PostAddresseeDisclosureTest(ChatBase):
    """A room post discloses unresolved addresses without refusing the row."""

    def _roster_with_other_seat(self, name="joined-seat"):
        from helm import seats
        seats.write_roster(name, presence_beat=False)

    def test_absent_addressee_is_disclosed_from_a_populated_roster(self):
        self._roster_with_other_seat()
        rc, out, err = self.run_cmd(["post", "@ghost", "please", "review"])
        self.assertEqual(rc, 0, err)
        self.assertIn(
            "helm chat: addressee @ghost is ABSENT — no current roster row; "
            "the post was still written.", out)

    def test_sentence_period_is_not_part_of_the_disclosed_addressee(self):
        self._roster_with_other_seat()
        rc, out, err = self.run_cmd(["post", "please", "tell", "@daria."])
        self.assertEqual(rc, 0, err)
        self.assertIn("addressee @daria is ABSENT", out)
        self.assertNotIn("addressee @daria.", out)
        row = chat.read("main")[0][0]
        self.assertEqual(row["addressees"][0]["raw"], "daria")

    def test_interior_period_remains_a_legal_joined_seat_name(self):
        self._roster_with_other_seat("joined.seat")
        rc, out, err = self.run_cmd(["post", "ask", "@joined.seat", "now"])
        self.assertEqual(rc, 0, err)
        self.assertIn("addressee @joined.seat is JOINED", out)
        row = chat.read("main")[0][0]
        self.assertEqual(row["addressees"][0]["raw"], "joined.seat")

    def test_url_path_fragment_is_not_disclosed_as_an_addressee(self):
        self._roster_with_other_seat()
        rc, out, err = self.run_cmd([
            "post", "see", "https://example.test/@handle", "then", "ask",
            "@joined-seat"])
        self.assertEqual(rc, 0, err)
        self.assertIn("addressee @joined-seat is JOINED", out)
        self.assertNotIn("addressee @handle", out)
        row = chat.read("main")[0][0]
        self.assertEqual([cap["raw"] for cap in row["addressees"]],
                         ["joined-seat"])

    def test_unknown_addressee_has_distinct_empty_roster_text(self):
        rc, out, err = self.run_cmd(["post", "@ghost", "please", "review"])
        self.assertEqual(rc, 0, err)
        self.assertIn(
            "helm chat: addressee @ghost is UNKNOWN — no seat has joined this "
            "box yet, so membership cannot be determined; the post was still "
            "written.", out)
        self.assertNotIn("ABSENT", out)
        self.assertNotIn("no current roster row", out)

    def test_unknown_addressee_has_distinct_unreadable_roster_text(self):
        from helm import seats
        chat._ensure_dir()
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write("{not-json")
        rc, out, err = self.run_cmd(["post", "@ghost", "please", "review"])
        self.assertEqual(rc, 0, err)
        self.assertIn(
            "helm chat: addressee @ghost is UNKNOWN — the roster could not be "
            "read, so membership cannot be determined; the post was still "
            "written.", out)
        self.assertNotIn("ABSENT", out)
        self.assertNotIn("no seat has joined this box yet", out)

    def test_absent_addressee_disclosure_does_not_refuse_persistence(self):
        self._roster_with_other_seat()
        rc, out, err = self.run_cmd(["post", "work", "for", "@prejoin"])
        self.assertEqual(rc, 0, err)
        self.assertIn("addressee @prejoin is ABSENT", out)
        rows, total = chat.read("main")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["text"], "work for @prejoin")
        self.assertIn("helm chat: id %s" % rows[0]["id"], out)

    def test_malformed_addressee_surfaces_the_resolver_grammar_error(self):
        rc, out, err = self.run_cmd(["post", "please", "ask", "@bad/name"])
        self.assertEqual(rc, 0, err)
        self.assertIn(
            "helm chat: addressee @bad/name is MALFORMED — recipient "
            "'bad/name' must be 1-64 chars of [A-Za-z0-9._-] — the exact seat "
            "token; the post was still written.", out)
        self.assertNotIn("ABSENT", out)
        self.assertNotIn("UNKNOWN", out)

    def test_automated_writer_gets_the_durable_machine_disclosure(self):  # noqa: VACUOUS_ASSERTION — the non-empty exact expected capability positively controls both returned and persisted row fields
        row = chat.post("rogue alarm for @UNKNOWN", who="watchdog", sign=False)
        expected = [{
            "raw": "UNKNOWN",
            "canonical": "unknown",
            "error": None,
            "membership": "UNKNOWN",
            "evidence": "empty",
        }]
        self.assertEqual(row["addressees"], expected)
        persisted = chat.read("main")[0][0]
        self.assertEqual(persisted["addressees"], expected)
        self.assertEqual(persisted["id"], row["id"])

    def test_broadcast_token_is_not_resolved_as_one_seat(self):  # noqa: VACUOUS_ASSERTION — recipient_capability is armed to raise, while the persisted broadcast row is the positive control
        with mock.patch("helm.seats.recipient_capability",
                        side_effect=AssertionError("broadcast is not a seat")):
            rc, out, err = self.run_cmd(
                ["post", "--measured", "@all", "status", "update"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("addressee", out)
        self.assertEqual(chat.read("main")[0][0]["text"],
                         "[MEASURED] @all status update")


class ABroadcastStatesItsBasisTest(ChatBase):
    """A post that addresses every seat says how its sender knows. The three
    words are the ones a verdict already owes, asked at the sender's door."""

    def texts(self):
        return [row["text"] for row in chat.read("main")[0]]

    def test_a_broadcast_with_no_basis_is_refused_and_nothing_is_sent(self):
        for token in ("@all", "@fleet", "@everyone"):
            with self.subTest(token=token):
                rc, _out, err = self.run_cmd(["post", token, "pull", "now"])
                self.assertEqual(rc, 2, err)
                for flag in ("--measured", "--inferred", "--unverified"):
                    self.assertIn(flag, err)
                self.assertIn("Nothing was sent", err)
        self.assertEqual(self.texts(), [], "a refused broadcast reached the room")
        # THE CONTROL, same door and same body: one basis admits it.
        rc, _out, err = self.run_cmd(["post", "--inferred", "@all", "pull", "now"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.texts(), ["[INFERRED] @all pull now"])

    def test_each_basis_heads_the_row_it_was_given_for(self):
        for basis in ("measured", "inferred", "unverified"):
            rc, _out, err = self.run_cmd(
                ["post", "--" + basis, "@all", "about", basis])
            self.assertEqual(rc, 0, err)
        self.assertEqual(self.texts(),
                         ["[MEASURED] @all about measured",
                          "[INFERRED] @all about inferred",
                          "[UNVERIFIED] @all about unverified"])

    def test_two_bases_are_refused(self):
        rc, _out, err = self.run_cmd(
            ["post", "--measured", "--inferred", "@all", "which", "is", "it"])
        self.assertEqual(rc, 2, err)
        self.assertIn("ONE basis", err)
        self.assertEqual(self.texts(), [])

    def test_a_post_that_is_not_a_broadcast_is_never_asked(self):
        rc, _out, err = self.run_cmd(["post", "plain", "room", "talk"])
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.run_cmd(["post", "mail", "x@all.example", "bounced"])
        self.assertEqual(rc, 0, err)
        # ...and it MAY state one, which heads the row the same way.
        rc, _out, err = self.run_cmd(["post", "--unverified", "heard", "this"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.texts(),
                         ["plain room talk", "mail x@all.example bounced",
                          "[UNVERIFIED] heard this"])

    def test_the_word_inside_a_body_is_prose_and_never_a_basis(self):
        rc, _out, err = self.run_cmd(
            ["post", "@all", "the", "flag", "--measured", "is", "new"])
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.texts(), [])
        rc, _out, err = self.run_cmd(
            ["post", "--inferred", "@all", "the", "flag", "--measured", "is", "new"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.texts(),
                         ["[INFERRED] @all the flag --measured is new"])

    def test_a_body_on_stdin_passes_the_same_door(self):
        rc, _out, err = self.run_cmd(["post"], stdin_text="@all from a heredoc\n")
        self.assertEqual(rc, 2, err)
        rc, _out, err = self.run_cmd(["post", "--measured"],
                                     stdin_text="@all from a heredoc\n")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.texts(), ["[MEASURED] @all from a heredoc"])

    def test_the_usage_names_the_three_words(self):
        for flag in ("--measured", "--inferred", "--unverified"):
            self.assertIn(flag, chat.HELP["post"])


class RowIntegrityTest(ChatBase):
    def test_unicode_line_separator_never_tears_the_row(self):
        """U+2028/U+2029 inside a message (a voice paste can carry them) must
        not split the JSON row for readers — read() splits on exactly \\n,
        never str.splitlines() (found by the delivery lane 2026-07-20)."""
        chat.post("voice paste second visual line third", who="bob")
        rows, total = chat.read("main")
        self.assertEqual(total, 1)
        self.assertIn(" ", rows[0]["text"])
        chat.post("padding", who="bob")
        p = chat.room_path("main")
        chat._rotate(p, cap=1)   # force rotation through the same split law
        rows, total = chat.read("main")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["text"], "padding")


class PostUnknownFlagTest(ChatBase):
    """post REFUSES an unrecognised LEADING flag instead of publishing it —
    and ONLY leading flags: the body is prose and may talk about flags freely.
    All three xrev findings on the first cut are pinned here: whole-body
    scanning made flag-prose unsendable, single-dash flags still broadcast,
    and the tests sat after the __main__ guard where direct unittest
    execution never discovered them (this class now precedes it)."""

    def setUp(self):
        super().setUp()
        # Ambient actor for this class: the `--seat tester` calls below ASSERT
        # this session identity (the post-actor-binding contract) — they do not
        # SELECT another seat. Identity is incidental here: the class tests
        # leading-flag PARSING, not who signs.
        os.environ["HELM_CHAT_NAME"] = "tester"

    def _post(self, *args):
        import contextlib
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = chat.cmd_chat(["post"] + list(args))
        return rc, err.getvalue()

    def _rows(self, room="main"):
        return chat.read(room)[0]

    def test_a_misremembered_addressing_flag_is_refused_not_posted(self):
        rc, err = self._post("--to", "codex-orch", "hello")
        self.assertEqual(rc, 2)
        self.assertIn("--to", err)
        self.assertEqual(self._rows(), [])

    def test_single_dash_flags_are_refused_too(self):
        # xrev: startswith("--") left `-x` broadcasting (-h is now a help
        # ask, answered rc 0 by the dispatcher gate — see HelpBeforeWorkTest)
        rc, _ = self._post("-x")
        self.assertEqual(rc, 2)
        self.assertEqual(self._rows(), [])

    def test_help_gets_usage_not_a_broadcast(self):
        # upgraded from refusal (rc 2) to an ANSWER (rc 0, usage on stdout)
        # by the dispatcher help gate; still never a broadcast
        for flag in ("--help", "-h"):
            err, out = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                rc = chat.cmd_chat(["post", flag])
            self.assertEqual(rc, 0, flag)
            self.assertIn("usage:", out.getvalue())
        self.assertEqual(self._rows(), [])

    def test_prose_about_flags_is_sendable(self):
        # xrev: the first cut scanned the WHOLE body, so ordinary dev chat
        # about CLI flags was unsendable outside stdin
        rc, _ = self._post("--seat", "tester", "please", "use", "--force", "carefully")
        self.assertEqual(rc, 0)
        self.assertIn("--force", self._rows()[0]["text"])

    def test_double_dash_delimiter_sends_a_flag_shaped_body(self):
        rc, _ = self._post("--seat", "tester", "--", "--to", "is", "not", "a", "flag")
        self.assertEqual(rc, 0)
        self.assertTrue(self._rows()[0]["text"].startswith("--to"))

    def test_prose_dash_starters_are_body_not_flags(self):
        # "-" bullets and "->" arrows are not flag-shaped
        rc, _ = self._post("--seat", "tester", "->", "see", "the", "board")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._rows()), 1)

    def test_the_refusal_names_the_real_addressing_flag(self):
        _, err = self._post("--to", "someone", "hi")
        self.assertIn("--dm", err)

    def test_a_plain_message_still_posts(self):
        rc, _ = self._post("--seat", "tester", "an ordinary message")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._rows()), 1)


class DeletedCwdTest(ChatBase):
    """A session whose process cwd was DELETED (a pruned lane worktree — a
    ROUTINE lifecycle state here) must keep chatting. The homing prologue's
    eager os.getcwd() crashed every default chat verb AND all three delivery
    hooks BEFORE their fail-open guards could catch it (fable composition
    HIGH @ 8313d9f; main handled this, the lane regressed it). seats.safe_cwd
    fails open to None -> un-homed -> #main; the session lives."""

    def _delete_cwd(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        os.chdir(d)
        os.rmdir(d)
        self.assertRaises(OSError, os.getcwd)   # the probe's precondition

    def _hook(self, args, payload=b"{}"):
        """chat.cmd_chat with hook-JSON stdin + FD-1 capture (the hook emit
        writes fd 1 directly — invisible to redirect_stdout)."""
        import types
        fake = types.SimpleNamespace(buffer=io.BytesIO(payload))
        r, w = os.pipe()
        saved = os.dup(1)
        os.dup2(w, 1)
        os.close(w)
        try:
            with mock.patch.object(sys, "stdin", fake), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = chat.cmd_chat(list(args))
            sys.stdout.flush()
        finally:
            os.dup2(saved, 1)
            os.close(saved)
        chunks = []
        while True:
            b = os.read(r, 65536)
            if not b:
                break
            chunks.append(b)
        os.close(r)
        return rc, b"".join(chunks).decode("utf-8")

    def test_default_chat_io_survives_a_deleted_cwd(self):
        self._delete_cwd()
        rc, _, err = self.run_cmd(["post", "still", "alive"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cmd(["read"])
        self.assertEqual(rc, 0, err)
        self.assertIn("still alive", out)

    def test_all_three_delivery_hooks_survive_a_deleted_cwd(self):
        payload = json.dumps({"session_id": "s-del-cwd"}).encode("utf-8")
        self._delete_cwd()
        rc, out = self._hook(["join", "--hook-json"], payload)
        self.assertEqual(rc, 0)
        # the join RAN and emitted its identity line — a crash swallowed by
        # a fail-open guard would also rc 0, but emit nothing
        self.assertIn("SessionStart", out)
        rc, _ = self._hook(["deliver", "--hook-json"], payload)
        self.assertEqual(rc, 0)
        rc, _ = self._hook(["stop-guard", "--hook-json"], payload)
        self.assertEqual(rc, 0)

    def test_seat_add_homing_resolves_from_a_deleted_cwd(self):
        """The same eager-getcwd class at seat.py's _resolve_homing: `helm
        seat add --room X` from a deleted cwd must resolve, not crash."""
        from helm import seat as seat_mod
        from helm import seats
        self._delete_cwd()
        self.assertIsNone(seats.safe_cwd())
        self.assertEqual(seat_mod._resolve_homing("ops"), ("ops", None))
        self.assertEqual(seat_mod._resolve_homing(None), (None, None))

class HelpBeforeWorkTest(ChatBase):
    """--help is answered at the dispatcher, BEFORE any verb runs (the
    block-before-help class, live-probed 2026-07-22): `wait --help` entered
    the wait loop and blocked forever — the mandatory-first-action verb every
    new seat probes — and join/deliver/claim/log-flush DID WORK under --help
    (`claim --help` leased a resource named "--help"). The seats/node/meld
    legs are patched to raise, so a regression fails FAST instead of hanging
    the suite."""

    def setUp(self):
        super().setUp()
        # `--seat t` in the body tests below ASSERTS this ambient identity
        # (post-actor-binding contract); the class tests the help gate, not
        # signing, so the actor is incidental.
        os.environ["HELM_CHAT_NAME"] = "t"

    def _no_dispatch(self, args):
        """cmd_chat(args) with every downstream leg booby-trapped: reaching
        one means the help gate did not fire. chat._follow is trapped too —
        the read --follow leg dispatches inline in cmd_chat, and without the
        trap a gate regression HANGS the suite instead of failing fast."""
        from helm import seats, chatnode, meld
        boom = mock.Mock(side_effect=AssertionError(
            "dispatched past the --help gate"))
        with mock.patch.object(seats, "cmd", boom), \
                mock.patch.object(chatnode, "cmd_node", boom), \
                mock.patch.object(meld, "cmd", boom), \
                mock.patch.object(chat, "_follow", boom):
            return self.run_cmd(args)

    def test_wait_help_answers_before_the_loop(self):
        # THE bug: rc 124/143, zero output, harness timeout. Pin: rc 0 +
        # usage naming every flag's semantics, loop never entered.
        for argv in (["wait", "--help"], ["wait", "-h"],
                     ["wait", "--seat", "s", "--help"]):
            rc, out, err = self._no_dispatch(argv)
            self.assertEqual(rc, 0, argv)
            for token in ("--seat", "--room", "--follow", "--replace",
                          "--timeout", "--any", "timeout"):
                self.assertIn(token, out, argv)
            self.assertEqual(err, "")

    def test_every_seat_verb_answers_help_without_running(self):
        for verb in ("join", "deliver", "delegation-stop", "stop-guard",
                     "seats", "seat", "dm", "claim", "release", "claims"):
            rc, out, _ = self._no_dispatch([verb, "--help"])
            self.assertEqual(rc, 0, verb)
            self.assertIn("usage: helm chat %s" % verb, out)

    def test_node_and_meld_answer_help_without_dispatch(self):
        # meld's species spellings (council/standup) answer in their own
        # voice — each usage line names the typed verb, not the genus
        for verb in ("node", "meld", "council", "standup"):
            rc, out, _ = self._no_dispatch([verb, "--help"])
            self.assertEqual(rc, 0, verb)
            self.assertIn("usage: helm chat %s" % verb, out)

    def test_a_BROKEN_chatnode_IMPORT_still_leaves_EVERY_OTHER_chat_verb(self):
        """THE BLAST RADIUS BELONGS TO THE MODULE, NOT TO THE FUNCTION.

        A review's first finding was that one `try` covered two worlds: an
        unimportable chatnode reached a fallback whose comment says it fires
        "exactly when rendering RAISED". I cured it by hoisting the import
        clear of the catch so the sentence became true — and a review
        measured what that did. `_node_usage()` IS CALLED AT MODULE IMPORT, in
        the HELP dict literal, so letting it raise stopped `helm.chat` from
        importing at all and took EVERY chat verb down with it. A graceful
        degradation traded for a total outage, to fix a comment.

        MY OWN ARM COULD NOT SEE IT because it asserted a property of the
        FUNCTION — that `_node_usage` raises — while the damage was to the
        MODULE.

        THIS RUNS IN A SUBPROCESS, and that is not squeamishness. Breaking an
        import in-process means re-importing `helm.chat` under a poisoned
        `sys.modules`, which leaves a second module object bound as a package
        ATTRIBUTE that no `finally` over `sys.modules` restores — measured,
        by reddening a sibling arm in this very class. A
        fresh interpreter is the only place an import can be observed without
        the observation outliving it.
        """
        import subprocess
        probe = (
            "import sys\n"
            "sys.modules['helm.chatnode'] = None\n"
            "import helm as pkg\n"
            "pkg.__dict__.pop('chatnode', None)\n"
            "try:\n"
            "    import helm.chatnode\n"
            "except ImportError:\n"
            "    pass\n"
            "else:\n"
            "    print('MUSTHIT-FAILED'); raise SystemExit(0)\n"
            "from helm import chat\n"
            "print('IMPORTED')\n"
            "print(chat.HELP['node'])\n"
            "print('VERBS:' + ','.join(sorted(chat.HELP)))\n")
        out = subprocess.run([sys.executable, "-c", probe],
                             capture_output=True, text=True,
                             cwd=os.path.dirname(os.path.dirname(
                                 os.path.abspath(__file__))))
        self.assertNotIn("MUSTHIT-FAILED", out.stdout,
                         "MUST-HIT: the sentinel did not break the import, so "
                         "this arm ran against a healthy tree")
        self.assertIn(
            "IMPORTED", out.stdout,
            "helm.chat did not import with chatnode broken, so EVERY chat "
            "verb is dead and not just node:\n%s" % (out.stderr or out.stdout))
        self.assertIn("IMPORTED:", out.stdout.replace("could not be IMPORTED",
                                                      "IMPORTED:"),
                      "the reader is looking at a broken INSTALLATION and must "
                      "not be told the verb list failed to RENDER — two "
                      "different repairs:\n%s" % out.stdout)
        for verb in ("wait", "post", "read"):
            self.assertIn(
                verb, out.stdout,
                "chatnode being unimportable removed unrelated verb %r from "
                "the help table:\n%s" % (verb, out.stdout))

    def test_the_node_help_fallback_is_exercised_ON_the_failure_path(self):
        """A REMEDY PRINTED ONLY ON A FAILURE PATH CAN ONLY BE TESTED ON THAT
        FAILURE PATH, and testing it on the happy path is not a weaker check
        — it is a different one that cannot fail.

        Three revisions of this one sentence each shipped a remedy that was
        false in the only world where it prints. It named a command that does
        something else; then a command that works ONLY WHEN THE FALLBACK IS
        NOT NEEDED; and the branch is reached exactly when rendering the verb
        list RAISED, so every command that would print that list re-enters the
        renderer that just failed. The author ran the middle one and reported
        it verified — on a healthy tree, where this branch never executes.

        So the fallback now names WHERE the list lives and prescribes nothing,
        and this arm breaks the renderer first and reads what comes out."""
        import re as _re
        from unittest import mock
        from helm import chat, chatnode

        def commands_in(text):
            return _re.findall(r"helm chat node ([a-z][a-z-]*)", text)

        # MUST-HIT ON THE EXTRACTOR: fed prose that DOES name a command it
        # finds one, so the emptiness asserted below is a property of the
        # fallback rather than of a regex that matches nothing.
        self.assertEqual(
            commands_in("... e.g. `helm chat node help`, prints the list"),
            ["help"], "MUST-HIT: the extractor cannot see a named command, so "
                      "this arm would pass against any text at all")

        with mock.patch.object(chatnode, "_VERBS", "malformed"):
            with self.assertRaises(Exception):
                chatnode._usage()      # the renderer really is broken now
            text = chat._node_usage()

            # IT STILL RENDERS, and it still points somewhere real.
            self.assertIn("helm chat node", text)
            self.assertIn("_VERBS", text)

            # AND EVERY COMMAND IT NAMES MUST SURVIVE THE BROKEN RENDERER.
            # Today it names none; if a future edit adds one, this runs it
            # under exactly the failure that produced the text.
            for verb in commands_in(text):
                if verb == "verb":         # the <verb> placeholder, not a call
                    continue
                err = io.StringIO()
                try:
                    with contextlib.redirect_stderr(err):
                        chatnode.cmd_node([verb])
                except Exception as exc:   # noqa: BLE001 — the whole point
                    self.fail(
                        "the fallback prescribes `helm chat node %s`, and "
                        "running it under the very failure that produced "
                        "this text raises %s: %s — a remedy that is false "
                        "in the only world where it prints"
                        % (verb, exc.__class__.__name__, exc))

    def test_node_help_names_EVERY_verb_the_subcommand_dispatches(self):  # noqa: VACUOUS_ASSERTION — the spy controls are inside a loop, but the loop CANNOT be skipped: assertGreaterEqual(len(advertised), 5) runs unconditionally before it, and the refused-verb pole after it is unconditional too
        """task/2600. `node --help` and `node refuel --help` are both served
        from HELP["node"], and that string listed up|down|status while the
        subcommand had shipped `prepare` and `refuel`. So an operator
        following the dry-faucet degrade — whose own cure line is `helm chat
        node refuel` — checked the help and was told by helm that the verb it
        had just recommended does not exist. Hit twice from opposite
        directions: a fresh seat following a doctor cure, and me running the
        faucet cure while the faucet was actually dry.

        THE AUTHORITY IS THE DISPATCHER, NOT THE PROSE. Reading the verb set
        out of the usage string and checking it against the help compares a
        derivation with its own source: since `HELP["node"]` is rendered from
        that same usage, the two agree by construction and a verb invented in
        the prose passes. So every advertised verb is put THROUGH `cmd_node`
        here, with its handler replaced by a spy — proving the routing without
        running the real thing, since `up` would start a systemd unit."""
        import contextlib
        import io as _io
        import re
        from unittest import mock
        from helm import chat, chatnode

        rendered = chatnode._usage()
        verbs = re.search(r"helm chat node <([^>]+)>", rendered)
        self.assertIsNotNone(
            verbs, "MUST-HIT: the rendered usage no longer carries a <a|b|c> "
                   "verb list, so this arm is reading nothing")
        advertised = [v for v in verbs.group(1).split("|") if v]
        self.assertGreaterEqual(len(advertised), 5, advertised)

        # THE INVERSE DIRECTION: the loop below proves every ADVERTISED verb
        # is served; this proves every SERVED verb is advertised, so a verb
        # added to `_VERBS` and skipped by `_usage()` cannot pass.
        for name, _fn, _doc in chatnode._VERBS:
            self.assertIn(name, advertised,
                          "%r is SERVED by the dispatcher but the rendered "
                          "usage does not advertise it" % name)

        for verb in advertised:
            spy = mock.Mock(return_value=0)
            table = tuple((n, (spy if n == verb else f), d)
                          for n, f, d in chatnode._VERBS)
            with mock.patch.object(chatnode, "_VERBS", table):
                rc = chatnode.cmd_node([verb])
            self.assertEqual(rc, 0, verb)
            self.assertEqual(
                spy.call_count, 1,
                "%r is ADVERTISED but does not reach a handler" % verb)

        # THE PAIRED POLE, so the loop above is not satisfied by a dispatcher
        # that accepts anything: a verb nobody serves is refused.
        err = _io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(chatnode.cmd_node(["invented"]), 2)
        self.assertIn("usage: helm chat node", err.getvalue())

        # AND THE HELP STILL SERVES THEM, which is task/2600's own property.
        served = chat.HELP["node"]
        for verb in advertised:
            self.assertIn(verb, served,
                          "`helm chat node %s --help` denies its own verb: "
                          "%r" % (verb, served))
        self.assertNotIn("invented", served)

    def test_chat_help_names_meld_verb_in_usage_line(self):
        """#169: `helm chat` usage line must explicitly name `meld|council|standup`."""
        from helm import cli
        usage = cli._VERB_HELP["chat"]
        self.assertIn("meld", usage)
        self.assertIn("meld|council|standup", usage)

    def test_read_follow_help_returns(self):
        # read --follow --help blocked forever too (same class, chat-local);
        # _no_dispatch traps _follow so a regression fails, never hangs
        rc, out, _ = self._no_dispatch(["read", "--follow", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("usage: helm chat read", out)

    def test_council_verbs_answer_help_with_what_they_now_DO(self):
        # These four answered "DEFERRED to 0.3" — and kept answering it for a
        # full release AFTER the council shipped (56bd498), until a reviewer
        # grepping chat.py read the stale help as proof the feature had never
        # landed and started rebuilding it (live 2026-07-24). --help must
        # describe the SHIPPED behaviour; a deferral notice that outlives its
        # deferral is a surface that lies.
        for verb in ("verdict", "reveal", "council-status", "council-abort"):
            rc, out, _ = self._no_dispatch([verb, "--help"])
            self.assertEqual(rc, 0, verb)
            self.assertIn("usage: helm chat %s" % verb, out)
            self.assertNotIn("DEFERRED", out.upper(), verb)

    def test_room_flag_refuses_a_flag_shaped_value(self):
        # THE residual: the --room pop ran BEFORE the help gate and consumed
        # '--help' as the room name — `wait --room --help` blocked forever
        # (rc 124, zero output) and `post --room --help x` posted into a
        # room literally named "--help". A flag-shaped value is never a
        # room name: refuse fast, before any dispatch.
        for argv in (["wait", "--room", "--help"],
                     ["read", "--room", "--help", "--follow"],
                     ["post", "--room", "--help", "hello"],
                     ["read", "--room"]):
            rc, _, err = self._no_dispatch(argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("--room wants a room name", err, argv)
        self.assertEqual(chat.read("--help")[1], 0)   # no room "--help"

    def test_seat_flag_refuses_a_flag_shaped_value(self):
        # same class, the identity flag: `post --seat --help hi` posted AS a
        # seat named "--help". Non-text verbs never get here — the gate's
        # whole-tail scan answers `wait --seat --help` as help first.
        for argv in (["post", "--seat", "--help", "hi"],
                     ["dm", "--seat", "--help", "codex", "hi"],
                     ["post", "--seat"]):
            rc, _, err = self._no_dispatch(argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("--seat wants a seat name", err, argv)
        self.assertEqual(chat.read()[1], 0)
        rc, out, _ = self._no_dispatch(["wait", "--seat", "--help"])
        self.assertEqual(rc, 0)                       # help wins at the gate
        self.assertIn("usage: helm chat wait", out)

    def test_dm_flag_refuses_a_flag_shaped_value(self):
        rc, _, err = self._no_dispatch(["post", "--dm", "--help", "hi"])
        self.assertEqual(rc, 2)
        self.assertIn("--dm wants a seat name", err)
        self.assertEqual(chat.read()[1], 0)

    def test_leading_help_with_a_body_refuses_loudly(self):
        # the judged contract: bare leading --help = usage rc 0 (pinned in
        # test_inert_verbs...); leading --help WITH a body = rc 2, usage on
        # stderr, NOTHING sent — never success-code a dropped message
        for argv in (["post", "--help", "me", "with", "this"],
                     ["reply", "--help", "1", "still", "broken"],
                     ["dm", "--help", "codex", "hi"]):
            rc, out, err = self._no_dispatch(argv)
            self.assertEqual(rc, 2, argv)
            self.assertEqual(out, "", argv)
            self.assertIn("usage: helm chat %s" % argv[0], err, argv)
            self.assertIn("NOTHING was sent", err, argv)
        self.assertEqual(chat.read()[1], 0)

    def test_dashdash_sends_a_body_that_starts_with_help(self):
        # the deliberate path the refusal points at stays open
        rc, _, _ = self.run_cmd(["post", "--seat", "t", "--",
                                 "--help", "me", "with", "this"])
        self.assertEqual(rc, 0)
        self.assertEqual(chat.read()[0][-1]["text"], "--help me with this")

    def test_help_asks_never_block_end_to_end(self):
        # the class's live symptom was rc 124 under harness timeout: pin the
        # never-blocks property through the real entrypoint, hard timeout
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for argv, want in ((["wait", "--help"], 0),
                           (["wait", "--room", "--help"], 2),
                           (["read", "--room", "--help", "--follow"], 2)):
            p = subprocess.run(
                [sys.executable, "-m", "helm", "chat"] + argv, cwd=root,
                capture_output=True, text=True, timeout=15)
            self.assertEqual(p.returncode, want, (argv, p.stderr))

    def test_inert_verbs_answer_help_without_side_effects(self):
        for verb in ("react", "rooms", "verify", "log-flush", "post",
                     "reply"):
            rc, out, _ = self.run_cmd([verb, "--help"])
            self.assertEqual(rc, 0, verb)
            self.assertIn("usage: helm chat %s" % verb, out)
        self.assertEqual(chat.read()[1], 0)   # nothing posted, nothing leased

    def test_claim_help_creates_no_lease(self):
        rc, out, _ = self._no_dispatch(["claim", "--help"])
        self.assertEqual(rc, 0)
        from helm import seats
        self.assertEqual(seats.claims_list(), [])

    def test_prose_about_help_still_sends(self):
        # free-text verbs scan only the LEADING position: a body ABOUT
        # --help must stay sendable (post's flag-refusal scope law)
        rc, out, _ = self.run_cmd(["post", "--seat", "t", "run", "it",
                                   "with", "--help", "first"])
        self.assertEqual(rc, 0)
        self.assertEqual(chat.read()[0][-1]["text"],
                         "run it with --help first")

    def test_roster_alias_shares_the_seats_help(self):
        rc, out, _ = self._no_dispatch(["roster", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("usage: helm chat seats", out)


class SeatActorBindingTest(ChatBase):
    """Xrev outcome controls: --seat is an ASSERTION, not
    a signer selector. An actor (ambient HELM_CHAT_NAME) that ASSERTS a
    DIFFERENT --seat produces NO effect at all — no row, no DM spool change,
    no ACK transition, no signer call — refused BEFORE any of them. Actor
    with omitted/equal --seat succeeds, and the signer sees the AMBIENT actor,
    never the claim. Real dm/ack verbs, not `post --dm`.
    (Bug it closes: `helm chat dm X --seat kimi` used to sign the DM AS kimi;
    `helm chat ack --seat kimi` used to forge kimi's signed ACK.)"""

    def setUp(self):
        super().setUp()
        # THE AMBIENT ACTOR — declared AND corroborated, because signing a DM
        # is an act and an act door needs the roster to know this seat.
        _tmp_declare(self, "seat-a")
        self._ss = mock.patch.object(chat, "_sign_send",
                                     return_value=(None, chat._diag(
                                         "send_failed", "off")))
        self.ss = self._ss.start()
        self.addCleanup(self._ss.stop)

    def _rows(self, room="main"):
        return chat.read(room)[0]

    @contextlib.contextmanager
    def _signer_configured(self):
        """Genuinely turn the signed transport ON so _sign_send is REACHABLE on
        a success path. ChatBase runs transport-off, where _sign_send is never
        called on ANY path — which alone makes a mismatch's assert_not_called
        VACUOUS (it passes whether or not the refusal fired). Under this, the
        equal/omitted path DOES call _sign_send, so a mismatch's no-call is a
        real, discriminating control (an xrev note)."""
        from helm import cell as cellmod
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        ready = {"configured": True, "usable": True, "state": "ready",
                 "reason": None}
        with mock.patch.object(cellmod, "bin_status", return_value=ready), \
                mock.patch.object(chat, "node_head",
                                  return_value={"chain_index": 1}):
            yield

    # ---- POST ------------------------------------------------------------
    def test_post_seat_mismatch_refuses_before_any_row(self):
        rc, _out, err = self.run_cmd(["post", "hi", "--seat", "seat-b"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot act as another seat", err)
        self.assertEqual(self._rows(), [])          # NO row appended
        self.ss.assert_not_called()                 # NO signer call

    def test_post_ambient_signs_as_the_actor_not_the_claim(self):
        # This lane's exact contract: the CLI threads NO claimed profile into
        # the signing owner. post() passes profile=None -> _signed_row resolves
        # the signer through the identity gate (chat._post_identity), never the
        # --seat claim. Re-adding profile=seat (the bug this closes) makes the
        # profile arg non-None on the equal path and fails here. Mismatch never
        # reaches _signed_row at all — the refusal tests prove that.
        with mock.patch.object(chat, "_signed_row",
                               wraps=chat._signed_row) as sr:
            rc, _o, _e = self.run_cmd(["post", "hi", "--seat", "seat-a"])  # equal
        self.assertEqual(rc, 0)
        self.assertEqual(self._rows()[-1]["from"], "seat-a")   # ambient identity
        self.assertIsNone(sr.call_args.args[2])                # profile: no claim

    def test_post_no_seat_uses_ambient(self):
        rc, _o, _e = self.run_cmd(["post", "hi"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._rows()[-1]["from"], "seat-a")

    # ---- REPLY (post --reply-to) ----------------------------------------
    def test_reply_seat_mismatch_refuses(self):
        self.run_cmd(["post", "parent"])
        before = len(self._rows())
        rc, _o, err = self.run_cmd(
            ["post", "child", "--reply-to", "1", "--seat", "seat-b"])
        self.assertEqual(rc, 2)
        self.assertEqual(len(self._rows()), before)   # no child row
        self.assertIn("cannot act as another seat", err)

    # ---- REACT -----------------------------------------------------------
    def test_react_seat_mismatch_refuses_the_toggle(self):
        self.run_cmd(["post", "target"])
        self.ss.reset_mock()
        rc, _o, err = self.run_cmd(["react", "1", ":fire:", "--seat", "seat-b"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot act as another seat", err)
        self.ss.assert_not_called()
        # no reaction landed on the row
        self.assertFalse(any(r.get("react") for r in self._rows()))

    # ---- DM verb (the confirmed forgery) --------------------------------
    def test_dm_verb_seat_mismatch_signs_nothing_as_another(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = chat.cmd_chat(["dm", "codex", "secret", "--seat", "seat-b"])
        self.assertEqual(code, 2)
        self.assertIn("cannot act as another seat", err.getvalue())
        self.ss.assert_not_called()                 # no DM signed as seat-b
        # DIRECT spool control (xrev): the recipient's private lane
        # never grew — the refused DM produced no row anywhere, not just no sig
        self.assertEqual(chat.read(chat.dm_room("codex"))[1], 0)

    def test_dm_verb_ambient_delivers_as_the_actor(self):
        rc, _o, _e = self.run_cmd(["dm", "codex", "hello"])
        self.assertEqual(rc, 0)
        lane = chat.read(chat.dm_room("codex"))[0]
        self.assertTrue(lane and lane[-1]["from"] == "seat-a")

    # ---- ACK verb (the forgeable obligation clear) ----------------------
    def test_ack_verb_seat_mismatch_never_transitions(self):
        from helm import seats
        # seat-a posts a row addressed to seat-c so there IS an ackable row
        self.run_cmd(["post", "@seat-c please ack"])
        rid = self._rows()[-1]["id"]
        self.ss.reset_mock()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = chat.cmd_chat(["ack", rid, "done", "--seat", "seat-c"])
        self.assertEqual(code, 2)
        self.assertIn("cannot act as another seat", err.getvalue())
        self.ss.assert_not_called()                 # no forged signed ACK
        self.assertFalse(any(r.get("ack") == rid for r in self._rows()))

    # ---- NON-VACUOUS controls: prove the no-signer assertions discriminate --
    # Xrev note: the mismatch tests above run transport-off,
    # where _sign_send is never called on ANY path, so their assert_not_called
    # alone is vacuous. These run with the signer GENUINELY configured, so the
    # signer IS reached on success and the refusal's no-call is a real control.
    def test_post_mismatch_reaches_no_signer_with_transport_on(self):
        with self._signer_configured():
            # positive control: the equal-actor path DOES reach _sign_send
            rc, _o, _e = self.run_cmd(["post", "one", "--seat", "seat-a"])
            self.assertEqual(rc, 0)
            self.assertGreaterEqual(self.ss.call_count, 1)   # signer WAS reached
            self.ss.reset_mock()
            # the refusal: a mismatch reaches the (now-live) signer ZERO times
            rc, _o, err = self.run_cmd(["post", "two", "--seat", "seat-b"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot act as another seat", err)
        self.ss.assert_not_called()                          # NOW discriminating
        self.assertFalse(any(r["text"] == "two" for r in self._rows()))

    def test_dm_and_ack_mismatch_reach_no_signer_with_transport_on(self):
        # dm + ack are THE confirmed forgeries — prove the refusal stops the
        # signer even when signing is genuinely reachable (non-vacuous).
        with self._signer_configured():
            self.ss.reset_mock()
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                dm_rc = chat.cmd_chat(["dm", "codex", "x", "--seat", "seat-b"])
            self.assertEqual(dm_rc, 2)
            self.ss.assert_not_called()                      # no DM signed
            self.assertEqual(chat.read(chat.dm_room("codex"))[1], 0)  # no spool
            # a real ackable row (seat-a's own post signs; reset before the ack)
            self.run_cmd(["post", "@seat-c please ack"])
            rid = self._rows()[-1]["id"]
            self.ss.reset_mock()
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                ack_rc = chat.cmd_chat(["ack", rid, "done", "--seat", "seat-c"])
            self.assertEqual(ack_rc, 2)
            self.ss.assert_not_called()                      # no forged signed ACK
            self.assertFalse(any(r.get("ack") == rid for r in self._rows()))


if __name__ == "__main__":
    unittest.main()


class PostStdinIsTheShellSafePathTest(ChatBase):
    """`helm chat post` has always read stdin when given no body argument —
    and its usage never said so, so the whole fleet composed a2a text as
    double-quoted shell arguments instead.

    That is not a cosmetic gap. In double quotes, backticks are COMMAND
    SUBSTITUTION: bash runs the quoted content and splices its stdout into
    the message before helm is ever invoked, so the payload arrives with a
    hole in it and delivery reports OK. It ate an opus-integrator gate
    request on 2026-07-25 at the exact token the sentence was about.

    helm cannot guard that — the substitution happens before argv exists,
    and a hole is indistinguishable from typed text. The only real remedy is
    a path where the body never becomes a shell word, which already shipped.
    So these tests hold the DOCUMENTATION down, because an undiscoverable
    safe path is the same as no safe path.
    """

    def setUp(self):
        super().setUp()
        os.environ["HELM_CHAT_NAME"] = "tester"

    def test_the_help_surface_NAMES_stdin_and_why(self):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = chat.cmd_chat(["post", "--help"])
        self.assertEqual(rc, 0)
        text = out.getvalue() + err.getvalue()
        self.assertIn("stdin", text.lower())
        self.assertIn("backtick", text.lower())   # the hazard, not just the flag

    def test_a_body_on_stdin_actually_POSTS(self):
        """The doc is only true if the path works — pin both, or the help
        surface becomes the lie."""
        body = "gate at `git cherry main lane` with 100% | pipes | and $HOME"
        with mock.patch.object(sys, "stdin", io.StringIO(body)):
            err, out = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                rc = chat.cmd_chat(["post", "--room", "main"])
        self.assertEqual(rc, 0)
        rows = chat.read("main")[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], body)   # byte-identical, nothing eaten

    def test_an_EMPTY_stdin_still_refuses_rather_than_posting_blank(self):
        with mock.patch.object(sys, "stdin", io.StringIO("   \n")):
            err, out = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                rc = chat.cmd_chat(["post", "--room", "main"])
        self.assertEqual(rc, 2)
        self.assertEqual(chat.read("main")[0], [])


class TimestampCarriesItsZoneTest(ChatBase):
    """A bare HH:MM on a coordination surface is a seven-hour trap.

    Rows are stamped UTC (`pk.now_ts` uses gmtime), but the render sliced
    [11:16] and dropped the Z — so chat printed "09:27" while `stat`, `ps`
    and `date` on the same box all printed 02:27. Every timestamp an agent
    compares a chat row against is in the OTHER clock.

    Measured 2026-07-25: a seat read a config mtime of 02:25 against a chat
    row at 09:27, concluded the file had been untouched for hours when it had
    been written ninety seconds earlier, and publicly told the integrator to
    stop doing the correct thing. The marker costs one character and makes
    the two clocks distinguishable on sight.
    """

    def test_the_stamp_names_its_clock(self):
        import time
        today = time.strftime("%Y-%m-%d", time.gmtime())
        row = {"ts": "%sT09:27:31Z" % today, "from": "someone", "text": "hi"}
        self.assertTrue(chat._fmt(row).startswith("09:27Z "))

    def test_it_is_the_UTC_slice_and_NOT_local_time(self):
        """The binding assertion. If someone later renders local time and
        leaves the Z, this fails — which is the point: a marker that lies is
        worse than no marker, because it ends the argument."""
        import time
        from helm import pk
        stamp = pk.now_ts()
        rendered = chat._fmt({"ts": stamp, "from": "s", "text": "t"}).split()[0]
        self.assertEqual(rendered, time.strftime("%H:%M", time.gmtime()) + "Z")

    def test_an_unknown_time_reads_unknown_not_a_bare_Z(self):
        """Missing is not midnight and not a zone with no time in it."""
        for bad in ("", None, "garbage"):
            out = chat._fmt({"ts": bad, "from": "s", "text": "t"})
            self.assertTrue(out.startswith("--:-- "), repr(bad))
            self.assertNotIn("Z ", out.split("s")[0])

    def test_a_reaction_and_its_parent_agree_on_the_clock(self):
        """Both sides of the comparison go through one helper, because the
        bug this pins WAS a comparison between two differently-read stamps."""
        out = chat._fmt({"ts": "2026-07-25T09:27:00Z", "from": "a",
                         "react": "heart", "tfrom": "b",
                         "tts": "2026-07-25T08:15:00Z"})
        self.assertIn("09:27Z", out)
        self.assertIn("08:15Z", out)

    def test_the_helper_is_total(self):
        """It runs in front of every rendered row, so it may never raise —
        and it must never emit a bare zone with no time behind it. Written
        without restating the implementation, which would assert nothing."""
        for bad in (None, "", "x", 12345, "2026-07-25", "T::Z", object()):
            out = chat._hhmmz(bad)
            self.assertIsInstance(out, str)
            self.assertTrue(out == "--:--" or out.endswith("Z"), repr(bad))
            self.assertNotEqual(out, "Z")

    def test_a_row_from_another_day_carries_its_date(self):
        """A row from a non-today UTC day renders month and day before the
        time, so a room spanning days is never blind."""
        row = {"ts": "2020-01-15T03:45:00Z", "from": "s", "text": "t"}
        self.assertTrue(chat._fmt(row).startswith("Jan 15 03:45Z "))

    def test_reaction_target_from_another_day_carries_its_date(self):
        """The target timestamp in a reaction row obeys the same day rule,
        so the comparison the row invites stays consistent."""
        import time
        today = time.strftime("%Y-%m-%d", time.gmtime())
        out = chat._fmt({"ts": "%sT10:00:00Z" % today, "from": "a",
                         "react": "heart", "tfrom": "b",
                         "tts": "2020-02-20T08:30:00Z"})
        self.assertIn("Feb 20 08:30Z", out)
        self.assertIn("10:00Z", out)       # today's row stays compact

    def test_a_today_row_is_byte_identical_to_the_current_format(self):
        """No regression: a freshly-minted row renders the same compact form."""
        import time
        from helm import pk
        stamp = pk.now_ts()
        hhmm = time.strftime("%H:%M", time.gmtime()) + "Z"
        rendered = chat._fmt({"ts": stamp, "from": "s", "text": "t"})
        self.assertTrue(rendered.startswith(hhmm + " "))

    def test_the_day_aware_helper_never_raises(self):
        """Same contract as _hhmmz: runs on every rendered row, must never
        crash, must never emit a bare Z with no time behind it."""
        for bad in (None, "", "x", 12345, "2026-07-25", "T::Z", object()):
            out = chat._day_stamp(bad)
            self.assertIsInstance(out, str)
            self.assertTrue(out == "--:--" or out.endswith("Z"), repr(bad))
            self.assertNotEqual(out, "Z")


class RamRootFallbackTest(unittest.TestCase):
    """The no-/dev/shm arm derives a PER-USER root, never bare /tmp.

    codex's port review measured the first cut returning /tmp/helm-ram with
    /dev/shm mocked absent and TMPDIR unset — a predictable machine-global
    root, the shared-directory race Apple's secure-coding guide names. These
    arms pin the cured ladder: owned TMPDIR, then confstr 65537, then a
    uid-suffixed 0700 root that refuses loudly under a foreign owner."""

    def _no_shm(self):
        real = os.path.isdir
        return mock.patch.object(
            home.os.path, "isdir",
            side_effect=lambda p: False if p == "/dev/shm" else real(p))

    def test_linux_shm_short_circuits_regardless_of_tmpdir(self):
        with mock.patch.object(home.os.path, "isdir", return_value=True), \
                mock.patch.dict(os.environ, {"TMPDIR": "/somewhere/else"}):
            self.assertEqual(home.ram_root(), "/dev/shm")

    def test_owned_tmpdir_is_the_first_fallback(self):  # noqa: VACUOUS_ASSERTION — the assertEqual on the derived path is the unconditional positive
        d = tempfile.mkdtemp(prefix="helm-ramroot-")
        self.addCleanup(shutil.rmtree, d, True)
        with self._no_shm(), mock.patch.dict(os.environ, {"TMPDIR": d}):
            got = home.ram_root()
        self.assertEqual(got, os.path.join(os.path.realpath(d), "helm-ram"))

    def test_unowned_or_missing_tmpdir_is_not_trusted(self):  # noqa: VACUOUS_ASSERTION — the uid-root assertEqual is the unconditional positive
        with self._no_shm(), \
                mock.patch.dict(os.environ,
                                {"TMPDIR": "/does/not/exist/anywhere"}), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                self._private_fallback() as priv:
            got = home.ram_root()
        self.assertEqual(got, priv)

    def test_no_tmpdir_no_confstr_yields_uid_root_not_bare_tmp(self):  # noqa: VACUOUS_ASSERTION — path equality + stat ownership/mode are unconditional positives
        self.assertEqual(home._ram_fallback_path(),
                         "/tmp/helm-ram-%d" % os.getuid())
        with self._no_shm(), mock.patch.dict(os.environ), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None):
            os.environ.pop("TMPDIR", None)
            with self._private_fallback() as priv:
                got = home.ram_root()
                st = os.stat(got)   # inside the scope: exit removes it
        self.assertNotEqual(got, "/tmp/helm-ram")   # the probed defect
        self.assertEqual(got, priv)
        self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(st.st_mode) & 0o077, 0,
                         "group/other bits open on the per-user RAM root")

    def test_a_fallback_root_owned_by_another_uid_refuses_loudly(self):  # noqa: VACUOUS_ASSERTION — assertRaisesRegex on the exact message is the positive; the sibling arms prove the same call succeeds when owned
        foreign = mock.Mock(st_mode=stat.S_IFDIR | 0o700,
                            st_uid=os.getuid() + 1)

        with self._no_shm(), mock.patch.dict(os.environ), \
                self._private_fallback() as priv:
            # the uid gate lives on the EXISTING-entry branch, so the
            # fixture pre-creates the entry
            os.makedirs(priv, mode=0o700, exist_ok=True)
            os.environ.pop("TMPDIR", None)
            with mock.patch.object(home, "_darwin_user_tmp",
                                   return_value=None), \
                    mock.patch.object(home.os, "fstat",
                                      return_value=foreign):
                with self.assertRaisesRegex(RuntimeError, "another uid"):
                    home.ram_root()

    def test_an_owned_but_shared_writable_tmpdir_is_not_territory(self):  # noqa: VACUOUS_ASSERTION — the uid-root assertEqual is the unconditional positive on the same call
        d = tempfile.mkdtemp(prefix="helm-ramroot-")
        self.addCleanup(shutil.rmtree, d, True)
        os.chmod(d, 0o777)
        with self._no_shm(), mock.patch.dict(os.environ, {"TMPDIR": d}), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                self._private_fallback() as priv:
            got = home.ram_root()
        self.assertNotEqual(got, os.path.join(os.path.realpath(d),
                                              "helm-ram"))
        self.assertEqual(got, priv)

    def test_a_preexisting_permissive_uid_root_is_repinned_to_0700(self):  # noqa: VACUOUS_ASSERTION — the mode assertEqual after the call is the unconditional positive
        with self._private_fallback() as target:
            os.makedirs(target, mode=0o700, exist_ok=True)
            os.chmod(target, 0o755)
            with self._no_shm(), mock.patch.dict(os.environ), \
                    mock.patch.object(home, "_darwin_user_tmp",
                                      return_value=None):
                os.environ.pop("TMPDIR", None)
                got = home.ram_root()
            self.assertEqual(got, target)
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o700)


    def test_a_preplanted_symlink_at_the_fallback_name_refuses_untouched(self):  # noqa: VACUOUS_ASSERTION — the victim-mode assertEqual after the refusal is the unconditional positive that nothing was dereferenced
        victim = tempfile.mkdtemp(prefix="helm-ramroot-victim-")
        self.addCleanup(shutil.rmtree, victim, True)
        os.chmod(victim, 0o755)
        ctx = self._private_fallback()
        target = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        os.symlink(victim, target)
        with self._no_shm(), mock.patch.dict(os.environ), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None):
            os.environ.pop("TMPDIR", None)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                home.ram_root()
        self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o755,
                         "the victim directory was mutated through the link")


    def _private_fallback(self):
        """Every arm owns a PRIVATE fallback target — the first arms
        rmtree'd/chmod'd the REAL /tmp/helm-ram-<uid>, which on a no-shm
        host is the live fleet's chat root (codex round-4 D1 overrule)."""
        import contextlib

        @contextlib.contextmanager
        def scoped():
            base = tempfile.mkdtemp(prefix="helm-ramroot-priv-")
            priv = os.path.join(base, "helm-ram-%d" % os.getuid())
            with mock.patch.object(home, "_ram_fallback_path",
                                   return_value=priv):
                yield priv
            shutil.rmtree(base, ignore_errors=True)
        return scoped()

    def test_a_writable_nonsticky_ancestor_disqualifies_a_candidate(self):  # noqa: VACUOUS_ASSERTION — the private-fallback assertEqual is the unconditional positive on the same call
        base = tempfile.mkdtemp(prefix="helm-ramroot-anc-")
        self.addCleanup(shutil.rmtree, base, True)
        os.chmod(base, 0o777)               # writable, NOT sticky
        leaf = os.path.join(base, "leaf")
        os.mkdir(leaf, 0o700)
        with self._no_shm(), mock.patch.dict(os.environ, {"TMPDIR": leaf}), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                self._private_fallback() as priv:
            got = home.ram_root()
        self.assertEqual(got, priv,
                         "an owned 0700 leaf under a swappable ancestor was "
                         "trusted as territory")

    def test_a_sticky_or_unwritable_ancestry_is_accepted(self):
        base = tempfile.mkdtemp(prefix="helm-ramroot-anc-")
        self.addCleanup(shutil.rmtree, base, True)
        os.chmod(base, 0o1777)              # writable BUT sticky, like /tmp
        leaf = os.path.join(base, "leaf")
        os.mkdir(leaf, 0o700)
        with self._no_shm(), mock.patch.dict(os.environ, {"TMPDIR": leaf}), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None):
            got = home.ram_root()
        self.assertEqual(got, os.path.join(os.path.realpath(leaf),
                                           "helm-ram"))

    def test_a_preexisting_child_symlink_in_a_valid_candidate_refuses(self):  # noqa: VACUOUS_ASSERTION — the victim-mode assertEqual after the refusal is the unconditional positive that nothing was dereferenced
        victim = tempfile.mkdtemp(prefix="helm-ramroot-victim-")
        self.addCleanup(shutil.rmtree, victim, True)
        os.chmod(victim, 0o755)
        d = tempfile.mkdtemp(prefix="helm-ramroot-")
        self.addCleanup(shutil.rmtree, d, True)
        os.symlink(victim, os.path.join(d, "helm-ram"))
        with self._no_shm(), mock.patch.dict(os.environ, {"TMPDIR": d}), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None):
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                home.ram_root()
        self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o755,
                         "the victim was mutated through the child link")

    def test_a_foreign_owned_ancestor_disqualifies_a_candidate(self):  # noqa: VACUOUS_ASSERTION — the private-fallback assertEqual is the unconditional positive on the same call
        base = tempfile.mkdtemp(prefix="helm-ramroot-anc-")
        self.addCleanup(shutil.rmtree, base, True)
        leaf = os.path.join(base, "leaf")
        os.mkdir(leaf, 0o700)
        real_stat = os.stat
        base_res = os.path.realpath(base)
        foreign = os.stat(base)

        def stat_sub(p, *a, **kw):
            if isinstance(p, str) and os.path.realpath(p) == base_res:
                m = mock.Mock(st_mode=foreign.st_mode,
                              st_uid=os.getuid() + 1)
                return m
            return real_stat(p, *a, **kw)

        with self._no_shm(), mock.patch.dict(os.environ, {"TMPDIR": leaf}), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                mock.patch.object(home.os, "stat", side_effect=stat_sub), \
                self._private_fallback() as priv:
            got = home.ram_root()
        self.assertEqual(got, priv,
                         "a leaf under a foreign-owned ancestor was trusted")

    def test_a_restrictive_umask_cannot_poison_the_fallback(self):
        """umask 0777 filters mkdir(0700) to 0000, and the no-follow open
        then failed BEFORE fchmod could repair the inode (codex round-7) —
        the establishment now repairs a just-created entry's mode first."""
        with self._no_shm(), mock.patch.dict(os.environ), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                self._private_fallback() as priv:
            os.environ.pop("TMPDIR", None)
            old = os.umask(0o777)   # AFTER the fixture base exists — the
            try:                    # umask must poison only the establish
                got = home.ram_root()
                st = os.stat(got)
            finally:
                os.umask(old)
        self.assertEqual(got, priv)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o700)

    def test_an_unusable_owned_tmpdir_falls_through_not_aborts(self):
        """An owned 0500 TMPDIR passed the ownership bar then died EACCES at
        the child mkdir, aborting the whole ladder (codex round-7) — an
        environmental failure now falls through to the fixed fallback while
        adversarial refusals (symlink, foreign uid) stay loud."""
        d = tempfile.mkdtemp(prefix="helm-ramroot-")
        self.addCleanup(shutil.rmtree, d, True)
        self.addCleanup(os.chmod, d, 0o700)
        os.chmod(d, 0o500)
        with self._no_shm(), mock.patch.dict(os.environ, {"TMPDIR": d}), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                self._private_fallback() as priv:
            got = home.ram_root()
        self.assertEqual(got, priv)

    def test_posix_acls_on_the_established_dir_refuse_loudly(self):  # noqa: VACUOUS_ASSERTION — assertRaisesRegex on the exact message is the positive; the fourteen sibling arms prove the same call succeeds without ACLs
        """ACLs resolve before mode bits, so an ACL-bearing 'private' dir is
        not private — and the stdlib cannot remove POSIX ACLs, so Linux
        refuses (codex round-8, from Apple's inheritable-ACE semantics)."""
        with self._no_shm(), mock.patch.dict(os.environ), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                mock.patch.object(home.os, "listxattr",
                                  return_value=["system.posix_acl_access"]), \
                self._private_fallback():
            os.environ.pop("TMPDIR", None)
            with self.assertRaisesRegex(RuntimeError, "POSIX ACLs"):
                home.ram_root()

    def test_darwin_strips_acls_with_apples_own_verb(self):
        """On darwin the establish discipline runs /bin/chmod -N on the
        established path — Apple's documented ACL-removal verb — and a
        failed strip refuses rather than promising false exclusivity."""
        import subprocess as sp
        import sys as sysmod
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0)

        with self._no_shm(), mock.patch.dict(os.environ), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                mock.patch.object(sysmod, "platform", "darwin"), \
                mock.patch.object(sp, "run", side_effect=fake_run), \
                self._private_fallback() as priv:
            os.environ.pop("TMPDIR", None)
            got = home.ram_root()
        self.assertEqual(got, priv)
        # the strip runs on the PRIVATE build name and the hardened inode
        # is published by rename — the final name never carries ACEs
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["/bin/chmod", "-N"])
        self.assertTrue(calls[0][2].startswith(priv + ".estab-"),
                        "strip ran on %s, not the private build" % calls[0][2])

    def test_a_failed_darwin_strip_refuses_not_promises(self):
        """The strip's own failure must not fall through to a false
        exclusivity promise — chmod -N returning nonzero refuses loudly."""
        import subprocess as sp
        import sys as sysmod
        with self._no_shm(), mock.patch.dict(os.environ), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                mock.patch.object(sysmod, "platform", "darwin"), \
                mock.patch.object(sp, "run",
                                  return_value=mock.Mock(returncode=1)), \
                self._private_fallback():
            os.environ.pop("TMPDIR", None)
            with self.assertRaisesRegex(RuntimeError, "strip ACLs"):
                home.ram_root()

    def test_an_unreadable_acl_state_refuses_not_promises(self):  # noqa: VACUOUS_ASSERTION — assertRaisesRegex on the exact not-measured message is the positive; the ENOTSUP control below proves the same call passes when absence is provable
        """Could-not-look must never answer as verified-absent: a failing
        listxattr refused exclusivity it did not measure (codex round-10),
        while a filesystem that CANNOT hold xattrs provably has no ACLs."""
        import errno as errno_mod
        with self._no_shm(), mock.patch.dict(os.environ), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                mock.patch.object(home.os, "listxattr",
                                  side_effect=OSError(errno_mod.EACCES,
                                                      "denied")), \
                self._private_fallback():
            os.environ.pop("TMPDIR", None)
            with self.assertRaisesRegex(RuntimeError, "not measured"):
                home.ram_root()
        with self._no_shm(), mock.patch.dict(os.environ), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                mock.patch.object(home.os, "listxattr",
                                  side_effect=OSError(errno_mod.ENOTSUP,
                                                      "no xattr")), \
                self._private_fallback() as priv:
            os.environ.pop("TMPDIR", None)
            self.assertEqual(home.ram_root(), priv)

    def test_content_planted_before_publication_refuses_unpublished(self):  # noqa: VACUOUS_ASSERTION — the not-lexists assert on the final name is the unconditional positive that nothing was published
        """codex's round-11 seam probe, frozen: a writer that reaches the
        directory before publication (an inherited ACE on darwin, any race
        elsewhere) is caught by the verified-empty step, the establishment
        refuses, and the final name is never published."""
        planted = {}
        real_strip = home._strip_or_refuse_acls

        def plant_then_strip(path, fd):
            with open(os.path.join(path, "intruder"), "w") as f:
                f.write("x")
            planted["at"] = path
            return real_strip(path, fd)

        with self._no_shm(), mock.patch.dict(os.environ), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                mock.patch.object(home, "_strip_or_refuse_acls",
                                  side_effect=plant_then_strip), \
                self._private_fallback() as priv:
            os.environ.pop("TMPDIR", None)
            with self.assertRaisesRegex(RuntimeError, "before it was "
                                                      "published"):
                home.ram_root()
            self.assertFalse(os.path.lexists(priv),
                             "a poisoned build was published anyway")
        self.assertTrue(planted, "the seam probe never ran")

    def test_a_name_swapped_after_hardening_is_never_published(self):  # noqa: VACUOUS_ASSERTION — the not-lexists positive on the final name plus the exact-message raise are the unconditional controls
        """codex's round-12 probe, frozen: every hardening check examines the
        held fd, but rename publishes whatever the NAME says — a temp dir
        moved aside post-open with a symlink planted at its name shipped the
        attacker's link. The lstat/fstat identity proof at publish refuses."""
        victim = tempfile.mkdtemp(prefix="helm-ramroot-victim-")
        self.addCleanup(shutil.rmtree, victim, True)
        real_strip = home._strip_or_refuse_acls
        swapped = {}

        def strip_then_swap(path, fd):
            real_strip(path, fd)
            moved = path + ".moved"
            os.rename(path, moved)
            swapped["moved"] = moved
            os.symlink(victim, path)
            return None

        with self._no_shm(), mock.patch.dict(os.environ), \
                mock.patch.object(home, "_darwin_user_tmp",
                                  return_value=None), \
                mock.patch.object(home, "_strip_or_refuse_acls",
                                  side_effect=strip_then_swap), \
                self._private_fallback() as priv:
            os.environ.pop("TMPDIR", None)
            with self.assertRaisesRegex(RuntimeError,
                                        "no longer names the hardened"):
                home.ram_root()
            self.assertFalse(os.path.lexists(priv),
                             "a substituted entry was published")
        if "moved" in swapped:
            shutil.rmtree(swapped["moved"], ignore_errors=True)
