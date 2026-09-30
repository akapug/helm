#!/usr/bin/env python3
"""The per-room index the two /api/ledger walks read (task/3715).

THE COST. Both walks behind /api/ledger (the turn-label join) and
/api/ledger/native (the chat pulse) sat behind a 15 s TTL, and every run of
either re-read EVERY chat room: about 30 MB of raw bytes, or about 93 MB
parsed, every 15 s while a ledger page was open. The owner's console logged
741 client hang-ups on /api/ledger in one day, and that steady multi-MB churn
on handler threads is what glibc's per-thread arenas keep.

THE STATES, one arm each, every arm read through the room files the walk
actually OPENS (a spy on `open`, filtered to this world's room files):
  * an unchanged room set  -> no room file is opened again;
  * one room appended      -> only that room is opened, and the new row is in
                              the answer;
  * a room removed         -> its rows are gone from the answer and its index
                              entry is dropped;
  * a room rotated         -> (a new file put in place) its rotated-out rows
                              are gone, never served from the old fold;
  * a rolled-back append   -> (truncated in place, then a different row
                              appended) the rolled-back row is gone;
  * an unreadable room     -> the answer NAMES it, with the reason, and a room
                              that was read before serves none of its old rows;
  * a row part-written     -> not folded until its newline lands, then once;
  * a rollback under one   -> (the stat stamp the same before the append and
    stat stamp                after its rollback) the rolled-back row is gone.
Each arm that asserts an absence first proves the spy saw the same room when it
WAS read, so an empty list below is about the walk and not about the spy.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, web, web_ledger  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR", "HELM_NODE_URL",
            "MELD_NODE_URL", "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CELL_BIN", "MELD_CELL_BIN")

TURN_A = "a1" * 32
TURN_B = "b2" * 32
TURN_C = "c3" * 32
TURN_D = "d4" * 32
WANTED = {TURN_A, TURN_B, TURN_C, TURN_D}
_REAL_OPEN = open


def _line(**row):
    return json.dumps(row) + "\n"


SEED = {
    "alpha": [_line(ts="2026-09-29T10:00:00Z", **{"from": "ann"},
                    text="first", turn=TURN_A),
              _line(ts="2026-09-29T10:00:01Z", **{"from": "ann"}, text="second")],
    "beta": [_line(ts="2026-09-29T10:00:02Z", **{"from": "bob"},
                   text="signed", turn=TURN_B),
             _line(ts="2026-09-29T10:00:03Z", **{"from": "cy"}, react=":tada:",
                   tts="2026-09-29T10:00:02Z", tfrom="bob")],
    "gamma": [_line(ts="2026-09-29T09:59:00Z", **{"from": "dee"}, text="quiet")],
}


def _index():
    """The module's index, or an empty stand-in on a tree without one, so an
    arm fails on what the walk READ rather than on a missing name."""
    return getattr(web_ledger, "_ROOM_FOLDS", {})


class _World(unittest.TestCase):
    """A tmp HELM_HOME with three seeded rooms, rebuilt for every arm."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ledger-index-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
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
        os.environ["HELM_HOME"] = self.tmp
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat-ram")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.makedirs(chat.chat_dir(), exist_ok=True)
        for room, rows in SEED.items():
            self.write(room, "".join(rows))
        _index().clear()
        self.addCleanup(_index().clear)
        for key in ("ledger-about", "ledger-native-pulse"):
            web._qstate.pop(key, None)
            self.addCleanup(web._qstate.pop, key, None)

    def write(self, room, text, mode="w"):
        with _REAL_OPEN(chat.room_path(room), mode, encoding="utf-8") as f:
            f.write(text)

    def opened(self, fn):
        """(fn(), [room names whose log fn opened, in order])."""
        seen, top = [], chat.chat_dir()

        def spy(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)):
                path = os.path.abspath(os.fspath(file))
                if os.path.dirname(path) == top and path.endswith(".jsonl"):
                    seen.append(os.path.basename(path)[:-len(".jsonl")])
            return _REAL_OPEN(file, *args, **kwargs)
        with mock.patch("builtins.open", spy):
            out = fn()
        return out, seen

    def pulse(self):
        return web_ledger._native_chat_pulse()

    def about(self, faults=None):
        if faults is None:
            return web_ledger._turn_about(WANTED)
        return web_ledger._turn_about(WANTED, faults=faults)


class UnchangedRoomsTest(_World):

    def test_an_unchanged_room_set_reads_no_room_file_again(self):  # noqa: VACUOUS_ASSERTION — the same spy's first call saw all three rooms (asserted unconditionally) before each empty list is asserted
        first, seen = self.opened(self.pulse)
        # UNCONDITIONAL POSITIVE CONTROL: the spy saw every room when the walk
        # read them, so the empty list below is about the walk.
        self.assertEqual(sorted(seen), ["alpha", "beta", "gamma"])
        self.assertEqual((first["rooms"], first["msgs"]), (3, 4))
        again, seen = self.opened(self.pulse)
        self.assertEqual(seen, [], "an unchanged room set was read again")
        self.assertEqual(again, first)

        labels, seen = self.opened(self.about)
        self.assertEqual(sorted(seen), ["alpha", "beta", "gamma"])
        self.assertEqual(set(labels), {TURN_A, TURN_B})
        again, seen = self.opened(self.about)
        self.assertEqual(seen, [], "an unchanged room set was read again")
        self.assertEqual(again, labels)


class AppendedRoomTest(_World):

    def test_only_the_appended_room_is_read_and_its_new_row_is_in_the_answer(self):
        self.pulse()
        self.about()
        self.write("gamma", _line(ts="2026-09-29T11:00:00Z", **{"from": "eve"},
                                  text="late news", turn=TURN_C), mode="a")
        pulse, seen = self.opened(self.pulse)
        self.assertEqual(seen, ["gamma"])
        self.assertEqual((pulse["msgs"], pulse["last_from"], pulse["last_room"],
                          pulse["last_ts"]),
                         (5, "eve", "gamma", "2026-09-29T11:00:00Z"))
        labels, seen = self.opened(self.about)
        self.assertEqual(seen, ["gamma"])
        self.assertEqual((labels[TURN_C]["room"], labels[TURN_C]["from"],
                          labels[TURN_C]["text"]), ("gamma", "eve", "late news"))
        # the rooms that did not move keep their labels
        self.assertEqual(labels[TURN_A]["room"], "alpha")


class RemovedAndRotatedRoomTest(_World):

    def test_a_removed_room_is_dropped_never_a_stale_row(self):  # noqa: VACUOUS_ASSERTION — the removed room's fold and label are both asserted present before the removal, on the same observables
        self.assertIn(TURN_B, self.about())
        self.assertEqual(self.pulse()["rooms"], 3)
        # POSITIVE CONTROL on the observable asserted empty below
        self.assertTrue([k for k in _index() if chat.room_path("beta") in k],
                        "the walk kept no fold of the room to drop")
        os.unlink(chat.room_path("beta"))
        labels = self.about()
        self.assertIn(TURN_A, labels)          # the walk still reads the rest
        self.assertNotIn(TURN_B, labels, "a removed room's label was served")
        pulse = self.pulse()
        self.assertEqual((pulse["rooms"], pulse["msgs"]), (2, 3))
        self.assertFalse([k for k in _index() if chat.room_path("beta") in k],
                         "the removed room's fold is still held")

    def test_a_rotated_room_keeps_none_of_its_rotated_out_rows(self):
        self.assertIn(TURN_A, self.about())
        self.assertEqual(self.pulse()["msgs"], 4)
        # what chat's rotation does: a NEW file put in place over the old one
        path = chat.room_path("alpha")
        fresh = path + ".rotating"
        with _REAL_OPEN(fresh, "w", encoding="utf-8") as f:
            f.write(SEED["alpha"][1])
            f.write(_line(ts="2026-09-29T12:00:00Z", **{"from": "fay"},
                          text="after rotation", turn=TURN_D))
        os.replace(fresh, path)
        labels, seen = self.opened(self.about)
        self.assertEqual(seen, ["alpha"])
        self.assertNotIn(TURN_A, labels, "a rotated-out row's label was served")
        self.assertEqual(labels[TURN_D]["text"], "after rotation")
        pulse = self.pulse()
        self.assertEqual((pulse["msgs"], pulse["last_from"]), (4, "fay"))

    def test_a_rolled_back_append_then_a_different_row_is_refolded(self):
        """chat's keyed append truncates its own row back out in place when its
        receipt fails. The next append lands at the same offset, so a reader
        that only reads PAST the old end would keep the rolled-back row."""
        path = chat.room_path("gamma")
        start = os.path.getsize(path)
        self.write("gamma", _line(ts="2026-09-29T13:00:00Z", **{"from": "gil"},
                                  text="rolled back", turn=TURN_C), mode="a")
        self.assertIn(TURN_C, self.about())
        self.assertEqual(self.pulse()["last_from"], "gil")
        with _REAL_OPEN(path, "r+b") as f:
            f.truncate(start)
        self.write("gamma", _line(ts="2026-09-29T13:00:01Z", **{"from": "hal"},
                                  text="the row that stayed, and it is longer",
                                  turn=TURN_D), mode="a")
        labels = self.about()
        self.assertNotIn(TURN_C, labels, "the rolled-back row's label was served")
        self.assertEqual(labels[TURN_D]["from"], "hal")
        pulse = self.pulse()
        self.assertEqual((pulse["msgs"], pulse["last_from"]), (5, "hal"))


class AppendReadRaceTest(_World):
    """A walk and a writer on the same room at the same moment."""

    def test_a_row_still_being_written_is_folded_once_when_its_newline_lands(self):  # noqa: VACUOUS_ASSERTION — the absent label and the unchanged count are the same observables asserted present, by exact value, once the newline lands
        self.assertEqual(self.pulse()["msgs"], 4)
        self.assertNotIn(TURN_C, self.about())
        row = _line(ts="2026-09-29T16:00:00Z", **{"from": "kim"},
                    text="in flight", turn=TURN_C)
        cut = len(row) // 2
        self.write("gamma", row[:cut], mode="a")
        # half a row is not a row yet, and it is not lost either
        self.assertEqual(self.pulse()["msgs"], 4)
        self.assertNotIn(TURN_C, self.about())
        self.write("gamma", row[cut:], mode="a")
        pulse, seen = self.opened(self.pulse)
        self.assertEqual(seen, ["gamma"])
        self.assertEqual((pulse["msgs"], pulse["last_from"]), (5, "kim"))
        labels, seen = self.opened(self.about)
        self.assertEqual(seen, ["gamma"])
        self.assertEqual((labels[TURN_C]["from"], labels[TURN_C]["text"]),
                         ("kim", "in flight"))

    def test_a_rollback_under_the_same_stat_stamp_serves_no_rolled_back_row(self):  # noqa: VACUOUS_ASSERTION — the same join is asserted to carry the row, unconditionally, before the rollback
        """The walk stats a room before it reads it, so a row appended between
        the two is folded under the stamp from before the row. Where the clock
        is coarser than the writer, the keyed append's rollback puts the file
        back to that same (size, mtime, ctime). The stat below is held at the
        stamp from before the append, which is the coarse clock's answer."""
        path = chat.room_path("gamma")
        before = os.stat(path)
        real_stat = os.stat

        def coarse(p, *args, **kwargs):
            if isinstance(p, (str, os.PathLike)) and \
                    os.path.abspath(os.fspath(p)) == path:
                return before
            return real_stat(p, *args, **kwargs)
        self.write("gamma", _line(ts="2026-09-29T13:00:00Z", **{"from": "gil"},
                                  text="rolled back", turn=TURN_C), mode="a")
        with mock.patch.object(web_ledger.os, "stat", coarse):
            # POSITIVE CONTROL: the walk read the row past the stamped size
            self.assertIn(TURN_C, self.about())
            self.assertEqual(self.pulse()["last_from"], "gil")
            with _REAL_OPEN(path, "r+b") as f:
                f.truncate(before.st_size)
            labels = self.about()
            pulse = self.pulse()
        self.assertNotIn(TURN_C, labels, "the rolled-back row's label was served")
        self.assertEqual((pulse["msgs"], pulse["last_from"]), (4, "cy"))


class UnreadableRoomTest(_World):

    def test_an_unreadable_room_is_named_never_silently_missing(self):
        # a room log that exists and will not open: a directory in its place
        os.makedirs(chat.room_path("locked"))
        self.assertIn("locked", chat.list_rooms())      # the walk sees it
        pulse = self.pulse()
        self.assertEqual(pulse["rooms"], 3)             # the others still read
        self.assertEqual(pulse.get("unreadable"),
                         [{"room": "locked", "reason": "not-a-file"}])
        faults = []
        labels = self.about(faults)
        self.assertEqual(set(labels), {TURN_A, TURN_B})
        self.assertEqual(faults, [{"room": "locked", "reason": "not-a-file"}])

    def test_a_room_read_before_that_turns_unreadable_serves_no_old_row(self):
        self.assertIn(TURN_B, self.about())
        self.assertEqual(self.pulse()["rooms"], 3)
        self.write("beta", _line(ts="2026-09-29T14:00:00Z", **{"from": "ivy"},
                                 text="unseen"), mode="a")
        denied = chat.room_path("beta")

        def refuse(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and \
                    os.path.abspath(os.fspath(file)) == denied:
                raise PermissionError(13, "Permission denied", denied)
            return _REAL_OPEN(file, *args, **kwargs)
        with mock.patch("builtins.open", refuse):
            faults = []
            labels = self.about(faults)
            pulse = self.pulse()
        self.assertIn(TURN_A, labels)
        self.assertNotIn(TURN_B, labels, "an unreadable room's old label was served")
        self.assertEqual(faults, [{"room": "beta", "reason": "denied"}])
        self.assertEqual((pulse["rooms"], pulse["msgs"]), (2, 3))
        self.assertEqual(pulse["unreadable"], [{"room": "beta", "reason": "denied"}])


class _Node(BaseHTTPRequestHandler):
    """The attestation node: four receipts, one per test turn."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = {"/status": {"healthy": True, "dag_height": 4, "latest_height": 4},
                "/api/receipts": [{"chain_index": i, "turn_hash": t,
                                   "agent": "c1" * 32, "timestamp": 1784582956 + i}
                                  for i, t in enumerate(sorted(WANTED))],
                "/api/cells": []}.get(self.path)
        raw = json.dumps(body).encode()
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class LedgerEndpointsTest(_World):
    """The same states through the two endpoints the page polls."""

    @classmethod
    def setUpClass(cls):
        cls.node = ThreadingHTTPServer(("127.0.0.1", 0), _Node)
        cls.node.daemon_threads = True
        threading.Thread(target=cls.node.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        cls.srv = web.make_server(0)
        threading.Thread(target=cls.srv.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for s in (cls.srv, cls.node):
            s.shutdown()
            s.server_close()

    def setUp(self):
        super().setUp()
        os.environ["HELM_NODE_URL"] = "http://127.0.0.1:%d" % \
            self.node.server_address[1]

    def get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.srv.server_address[1], path)
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())

    def next_window(self):
        for key in ("ledger-about", "ledger-native-pulse"):
            web._qstate.pop(key, None)

    def test_the_next_window_reads_only_what_moved(self):  # noqa: VACUOUS_ASSERTION — each endpoint's first request saw all three rooms through the same spy (asserted unconditionally) before the next window's empty list
        d, seen = self.opened(lambda: self.get("/api/ledger"))
        self.assertEqual(sorted(seen), ["alpha", "beta", "gamma"])
        by_hash = {t["turn_hash"]: t for t in d["turns"]}
        self.assertEqual(by_hash[TURN_A]["about"]["from"], "ann")
        n, seen = self.opened(lambda: self.get("/api/ledger/native"))
        self.assertEqual(sorted(seen), ["alpha", "beta", "gamma"])
        self.assertEqual(n["chat"]["msgs"], 4)

        self.next_window()
        d, seen = self.opened(lambda: self.get("/api/ledger"))
        self.assertEqual(seen, [], "an unchanged room set was read again")
        self.assertEqual(by_hash[TURN_A]["about"],
                         {t["turn_hash"]: t for t in d["turns"]}[TURN_A]["about"])
        n, seen = self.opened(lambda: self.get("/api/ledger/native"))
        self.assertEqual(seen, [], "an unchanged room set was read again")

        self.next_window()
        self.write("beta", _line(ts="2026-09-29T15:00:00Z", **{"from": "jo"},
                                 text="new", turn=TURN_C), mode="a")
        d, seen = self.opened(lambda: self.get("/api/ledger"))
        self.assertEqual(seen, ["beta"])
        by_hash = {t["turn_hash"]: t for t in d["turns"]}
        self.assertEqual((by_hash[TURN_C]["about"]["from"],
                          by_hash[TURN_C]["about"]["room"]), ("jo", "beta"))
        n, seen = self.opened(lambda: self.get("/api/ledger/native"))
        self.assertEqual(seen, ["beta"])
        self.assertEqual((n["chat"]["msgs"], n["chat"]["last_from"]), (5, "jo"))

    def test_an_unreadable_room_is_on_the_wire(self):
        os.makedirs(chat.room_path("locked"))
        d = self.get("/api/ledger")
        # POSITIVE CONTROL: the join ran and labelled the readable rooms
        self.assertEqual({t["turn_hash"]: t for t in d["turns"]}[TURN_B]
                         ["about"]["room"], "beta")
        self.assertEqual(d.get("about_unreadable"),
                         [{"room": "locked", "reason": "not-a-file"}])
        n = self.get("/api/ledger/native")
        self.assertEqual(n["chat"]["msgs"], 4)
        self.assertEqual(n["chat"].get("unreadable"),
                         [{"room": "locked", "reason": "not-a-file"}])


if __name__ == "__main__":
    unittest.main()
