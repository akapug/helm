#!/usr/bin/env python3
"""Hermetic contract tests for local blind relay + decoupled presence."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from helm import multiplayer


class MultiplayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-multiplayer-")
        self.prior = {k: os.environ.get(k) for k in (
            "HELM_MULTIPLAYER_DIR", "HELM_MULTIPLAYER_CAVE",
            "HELM_MULTIPLAYER_ACTOR", "HELM_MULTIPLAYER_CONNECTION",
            "HELM_MULTIPLAYER_BACKEND", "HELM_CHAT_ROOM", "HELM_CHAT_NAME")}
        os.environ["HELM_MULTIPLAYER_DIR"] = self.tmp
        for k in self.prior:
            if k != "HELM_MULTIPLAYER_DIR":
                os.environ.pop(k, None)
        self.relay, self.presence = multiplayer.adapters()

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_relay_preserves_opaque_update_and_generation_cursor(self):
        opaque = "AQAABHsic2VjcmV0IjoidGhlIHJlbGF5IG11c3Qgbm90IHJlYWQifQ=="
        first = self.relay.publish("Cave One", "Plan", "Agent A", opaque)
        self.assertNotIn("update", first)  # acknowledgements never echo content
        self.assertEqual(first["bytes"], len(opaque))
        out = self.relay.updates("Cave One", "Plan")
        self.assertEqual(out["updates"][0]["update"], opaque)
        self.assertEqual(out["cursor"], first["cursor"])
        second = self.relay.publish("Cave One", "Plan", "Agent B", "crdt:delta:2")
        tail = self.relay.updates("Cave One", "Plan", first["cursor"])
        self.assertEqual([r["id"] for r in tail["updates"]], [second["id"]])
        self.assertEqual(tail["cursor"], second["cursor"])

    def test_relay_serializes_concurrent_publishers(self):
        barrier = threading.Barrier(12)
        errors = []

        def publish(n):
            try:
                barrier.wait()
                self.relay.publish("one", "shared", "agent-%d" % n, "u%d" % n)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=publish, args=(n,)) for n in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        rows = self.relay.updates("one", "shared")["updates"]
        self.assertEqual(len(rows), 12)
        self.assertEqual({r["update"] for r in rows}, {"u%d" % n for n in range(12)})

    def test_first_publish_blocks_reader_until_header_and_update_land(self):
        entered, reading, release = threading.Event(), threading.Event(), threading.Event()
        original = self.relay._prepare
        seen, errors = [], []

        def slow_prepare(f, cave, doc):
            entered.set()
            release.wait(2)
            return original(f, cave, doc)

        def publish():
            try:
                self.relay.publish("one", "new", "writer", "u1")
            except Exception as e:
                errors.append(e)

        def read():
            try:
                reading.set()
                seen.append(self.relay.updates("one", "new"))
            except Exception as e:
                errors.append(e)

        with mock.patch.object(self.relay, "_prepare", side_effect=slow_prepare):
            writer = threading.Thread(target=publish)
            reader = threading.Thread(target=read)
            writer.start()
            self.assertTrue(entered.wait(1))
            reader.start()
            self.assertTrue(reading.wait(1))
            self.assertTrue(reader.is_alive())  # shared lock waits on first publish
            release.set()
            writer.join()
            reader.join()
        self.assertEqual(errors, [])
        self.assertEqual([r["update"] for r in seen[0]["updates"]], ["u1"])

    def test_partial_tail_never_advances_cursor_and_next_publish_repairs(self):
        first = self.relay.publish("one", "doc", "a", "u1")
        path = self.relay.path("one", "doc")
        with open(path, "ab") as f:
            f.write(b'{"v":1,"update":"crashed')
        out = self.relay.updates("one", "doc", first["cursor"])
        self.assertEqual(out["updates"], [])
        self.assertEqual(out["cursor"], first["cursor"])
        second = self.relay.publish("one", "doc", "b", "u2")
        out = self.relay.updates("one", "doc", first["cursor"])
        self.assertEqual([r["update"] for r in out["updates"]], ["u2"])
        self.assertEqual(out["cursor"], second["cursor"])

    def test_cursor_rejects_mid_record_and_recreated_generation(self):
        first = self.relay.publish("one", "doc", "a", "u1")
        generation, offset = first["cursor"].split(":")
        with self.assertRaisesRegex(ValueError, "boundary"):
            self.relay.updates("one", "doc", "%s:%d" % (generation, int(offset) - 1))
        os.remove(self.relay.path("one", "doc"))
        self.relay.publish("one", "doc", "b", "u2" * 100)
        with self.assertRaisesRegex(ValueError, "stale cursor generation"):
            self.relay.updates("one", "doc", first["cursor"])

    def test_relay_rejects_bad_cursor_and_oversized_update(self):
        with self.assertRaisesRegex(ValueError, "stale cursor"):
            self.relay.updates("one", "doc", "old:4")
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.relay.publish("one", "doc", "a",
                               "x" * (multiplayer.MAX_UPDATE_BYTES + 1))

    def test_lossless_path_keys_prevent_slug_collisions(self):
        self.assertNotEqual(self.relay.path("team.a", "plan"),
                            self.relay.path("team-a", "plan"))
        self.assertNotEqual(self.relay.path("one", "doc.a"),
                            self.relay.path("one", "doc-a"))
        self.relay.publish("team.a", "plan", "agent.a", "u1")
        self.relay.publish("team-a", "plan", "agent-a", "u2")
        self.assertEqual(self.relay.updates("team.a", "plan")["updates"][0]["update"],
                         "u1")
        self.assertEqual(self.relay.updates("team-a", "plan")["updates"][0]["update"],
                         "u2")

    def test_presence_never_touches_update_log(self):
        self.presence.heartbeat("one", "alice", "editing", ttl=30, connection="tab-a")
        self.assertFalse(os.path.exists(self.relay.path("one", "shared")))
        self.relay.publish("one", "shared", "alice", "u1")
        with open(self.relay.path("one", "shared"), "rb") as f:
            before = f.read()
        self.presence.heartbeat("one", "bob", "watching", ttl=30,
                                connection="session-b")
        with open(self.relay.path("one", "shared"), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual([p["actor"] for p in self.presence.peers("one")],
                         ["alice", "bob"])

    def test_presence_is_connection_scoped_and_leave_is_bound(self):
        self.presence.heartbeat("one", "alice", "editing", connection="tab-a")
        self.presence.heartbeat("one", "alice", "watching", connection="tab-b")
        peers = self.presence.peers("one")
        self.assertEqual([(p["actor"], p["connection"]) for p in peers],
                         [("alice", "tab-a"), ("alice", "tab-b")])
        self.assertTrue(self.presence.leave("one", "alice", "tab-a"))
        self.assertEqual([p["connection"] for p in self.presence.peers("one")],
                         ["tab-b"])
        self.assertFalse(self.presence.leave("one", "alice", "tab-a"))

    def test_presence_expires(self):
        with mock.patch.object(multiplayer.time, "time", return_value=100.0):
            self.presence.heartbeat("one", "alice", ttl=2, connection="a")
            self.presence.heartbeat("one", "bob", ttl=20, connection="b")
        with mock.patch.object(multiplayer.time, "time", return_value=103.0):
            self.assertEqual([p["actor"] for p in self.presence.peers("one")],
                             ["bob"])

    def test_defaults_follow_room_seat_and_session_not_metaharness(self):
        os.environ["HELM_CHAT_ROOM"] = "Project Alpha"
        os.environ["HELM_CHAT_NAME"] = "codex-1"
        self.assertEqual(multiplayer.default_cave(), "Project Alpha")
        self.assertEqual(multiplayer.actor_name(), "codex-1")
        os.environ["HELM_MULTIPLAYER_CAVE"] = "Explicit Cave"
        os.environ["HELM_MULTIPLAYER_ACTOR"] = "human"
        os.environ["HELM_MULTIPLAYER_CONNECTION"] = "phone"
        self.assertEqual(multiplayer.default_cave(), "Explicit Cave")
        self.assertEqual(multiplayer.actor_name(), "human")
        self.assertEqual(multiplayer.connection_name(), "phone")

    def _run(self, args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             mock.patch.object(multiplayer.sys, "stdin", io.StringIO(stdin)):
            rc = multiplayer.cmd_multiplayer(args)
        return rc, out.getvalue(), err.getvalue()

    def test_cli_two_actor_dogfood_contract(self):
        rc, out, _ = self._run(["presence", "--cave", "demo", "--actor", "david",
                                "--connection", "phone", "--state", "editing"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["actor"], "david")
        self.assertEqual(self._run(["publish", "board", "--stdin", "--cave", "demo",
                                    "--actor", "david"], stdin="opaque-a")[0], 0)
        self.assertEqual(self._run(["publish", "board", "--stdin", "--cave", "demo",
                                    "--actor", "codex"], stdin="opaque-b")[0], 0)
        rc, out, _ = self._run(["read", "board", "--cave", "demo", "--json"])
        self.assertEqual(rc, 0)
        rows = json.loads(out)["updates"]
        self.assertEqual([(r["actor"], r["update"]) for r in rows],
                         [("david", "opaque-a"), ("codex", "opaque-b")])
        self.assertEqual(self._run(["peers", "--cave", "demo", "--json"])[0], 0)

    def test_cli_set_and_status_drive_the_lww_demo_board(self):
        # a monotonic clock so "later write wins" is deterministic (real ts are
        # monotonic across sequential publishes; pinning them removes any doubt)
        counter = [100.0]

        def clock():
            counter[0] += 1.0
            return counter[0]

        with mock.patch.object(multiplayer.time, "time", clock):
            for args in (["set", "board", "greeting", "hello", "--cave", "demo",
                          "--actor", "david"],
                         ["set", "board", "greeting", "hi", "there", "--cave",
                          "demo", "--actor", "codex"],  # later + multi-word value
                         ["set", "board", "status", "building", "--cave", "demo",
                          "--actor", "codex"]):
                self.assertEqual(self._run(args)[0], 0, args)
            rc, out, _ = self._run(["status", "board", "--cave", "demo", "--json"])
        self.assertEqual(rc, 0)
        got = json.loads(out)
        board = {c["key"]: c for c in got["board"]}
        self.assertEqual(board["greeting"]["value"], "hi there")  # LWW: codex later
        self.assertEqual(board["greeting"]["actor"], "codex")
        self.assertEqual(board["status"]["value"], "building")
        self.assertEqual(got["foreign"], 0)
        # the human render materializes the same board (and launders — see below)
        rc, out, _ = self._run(["status", "board", "--cave", "demo"])
        self.assertEqual(rc, 0)
        self.assertIn("greeting = hi there", out)
        self.assertIn("status = building", out)

    def test_cli_status_launders_relay_supplied_values(self):
        # cell values ride the BLIND relay from any actor — a hostile ESC/bidi
        # payload must never reach the terminal through the status view.
        from helm import multiplayer_demo
        self.relay.publish("demo", "board", "attacker",
                           multiplayer_demo.encode("k", chr(0x9b) + "31mred",
                                                   "attacker"))
        rc, out, _ = self._run(["status", "board", "--cave", "demo"])
        self.assertEqual(rc, 0)
        self.assertNotIn(chr(0x9b), out)
        self.assertIn("\\u009b31mred", out)

    def test_cli_json_launders_relay_supplied_values(self):
        # the --json sinks are ALSO a terminal seam (TESTDRIVE points the owner
        # at --json). The presence API guards actor/connection via _identity, so
        # the real unguarded surface is the BLIND relay: a planted cell value +
        # envelope actor carry C1 (U+009B) / bidi RLO (U+202E) undecoded, and
        # they must escape in --json, never reach the terminal raw.
        from helm import multiplayer_demo
        payload = chr(0x9b) + "31m" + chr(0x202e) + "evil"
        self.relay.publish("demo", "board", chr(0x202e) + "attacker",
                           multiplayer_demo.encode("k", payload, "attacker"))
        for args in (["status", "board", "--cave", "demo", "--json"],
                     ["read", "board", "--cave", "demo", "--json"]):
            rc, out, _ = self._run(args)
            self.assertEqual(rc, 0, args)
            self.assertNotIn(chr(0x9b), out, args)     # C1 CSI never raw
            self.assertNotIn(chr(0x202e), out, args)   # bidi RLO never raw
            self.assertIn("\\u009b", out, args)        # escaped instead

    def test_cli_stdin_only_and_safe_human_output(self):
        self.assertEqual(self._run(["publish", "doc", "--stdin"], stdin="--cave")[0], 0)
        self.assertEqual(self._run(["publish", "doc", "raw-argv"])[0], 2)
        self.relay.publish("main", "doc", "attacker", "\x1b]8;;bad\x07\nforged")
        self.presence.heartbeat("main", "attacker", chr(0x9b) + "31mred",
                                connection="tab")
        rc, out, _ = self._run(["read", "doc"])
        self.assertEqual(rc, 0)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("forged", out)
        self.assertIn("opaque bytes", out)
        rc, out, _ = self._run(["peers"])
        self.assertEqual(rc, 0)
        self.assertNotIn(chr(0x9b), out)
        self.assertIn("\\u009b31mred", out)

    def test_cli_stdin_is_bounded_before_publish(self):
        rc, _out, err = self._run(["publish", "doc", "--stdin"],
                                  stdin="x" * (multiplayer.MAX_UPDATE_BYTES + 100))
        self.assertEqual(rc, 2)
        self.assertIn("exceeds", err)

    def test_cli_adapter_factory_is_injectable(self):
        calls = []

        class Relay:
            def updates(self, cave, doc, cursor=0):
                calls.append((cave, doc, cursor))
                return {"cave": cave, "doc": doc, "cursor": "fake:1", "updates": []}

        class Presence:
            pass

        factory = lambda name=None: (Relay(), Presence())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = multiplayer.cmd_multiplayer(["read", "d", "--cave", "c"], factory)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("c", "d", 0)])

    def test_cli_filesystem_errors_are_clean(self):
        bad = os.path.join(self.tmp, "not-a-directory")
        with open(bad, "w") as f:
            f.write("occupied")
        os.environ["HELM_MULTIPLAYER_DIR"] = bad
        rc, _out, err = self._run(["presence"])
        self.assertEqual(rc, 2)
        self.assertIn("helm multiplayer:", err)
        self.assertNotIn("Traceback", err)

    def test_cli_errors_are_clean(self):
        rc, _out, err = self._run(["read"])
        self.assertEqual(rc, 2)
        self.assertIn("invalid multiplayer command", err)


if __name__ == "__main__":
    unittest.main()
