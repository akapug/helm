#!/usr/bin/env python3
"""A Stop spawns nothing and reads no large file: the spawn/open spy.

THE BUDGET THAT CANNOT EXPIRE IS THE ONE COUNTED IN OPERATIONS. Every wall
clock budget is a fit to one box's load on one day, and a busier day expires
it (the hook-latency measurement in the journal). So this arm
counts what a stop DOES instead of how long it took: the stop-guard handler is
run exactly as the hook wrapper runs it — `helm chat stop-guard --hook-json`,
the payload on stdin — under a spy on every process spawn and on every open of
a file over 1 MB, in a world whose dispatch ledger is over 1 MB and whose
session holds a lane in gate.

What must be true: ZERO subprocesses from the ladder and ZERO opens of a file
over 1 MB anywhere in the handler, AND the gate exemption still granted — from
the resident's stop facts, which is the positive control that the claims rung
ran and read them.

THE SEAM RUNG IS ON THE LADDER UNDER TEST. Its facts — every worktree of the
repository, the whole gate-receipt ledger, git's answer about every live
pair — are the resident's to compute, and the rung only reads them. So the
world here also carries what the seam rung reads: a LIVE PEER
room (another seat's lease), two seats rostered in the room the stop stands
in, and a gate-receipt ledger over 1 MB. Its positive controls are the
co-tenancy disclosure, which only an EXACT census can produce, and the pair
witnesses it judged EXACT.

WHAT IS STILL SPAWNED IS NAMED HERE, NOT HIDDEN, and each is a follow-up with
its own owner:

  * one rung, switched off in the first arm by its own kill switch: WIRING (an
    import-graph walk of this checkout). It is still computed on the stop;
    moving it onto the resident is the next slice of this lane.
  * the chat CLI's ENTRY, before the ladder runs: the homing read of the cwd's
    project (`seats_identity._git_root_typed`, asked twice — once for the
    process cwd, once for the payload's) and the helm-scope check of the hook
    (`hooks._shared_root`). Every chat hook pays these, not only Stop.

The arms pin those as a CLOSED set: a spawn from anywhere else reddens them.
"""
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.test_seats import SeatsBase
from tests._stopfacts import LaneWorld

from helm import dispatches, gate

_ENV_PRIOR = {}

#: The only rung a stop may still spawn from, until it moves to the resident
#: too. Keyed by the function that runs the rung.
_NOT_YET_RESIDENT = {"_wiring_rung": "wiring"}
#: The chat CLI entry's spawns before the ladder, by the frame that spawns.
_ENTRY_SPAWNERS = ("seats_identity.py:_git_root_typed",
                   "hooks.py:_shared_root")
_RUNG_OF = dict(_NOT_YET_RESIDENT, _seam_gate="seam", claims_rung="claims",
                _beacon_block="beacon", _rearm_rung="beacon",
                _spiral_gate="spiral", _ndp_gate="ndp", _stop_whisper="whisper",
                _pending_all="inbox", _claim_evidence_warning="claim-evidence",
                publish="response", _stop_guard="ladder", main="cli")
BIG = 1 << 20


def setUpModule():
    _ENV_PRIOR["HELM_SCRATCH_GC"] = os.environ.get("HELM_SCRATCH_GC")
    os.environ["HELM_SCRATCH_GC"] = "0"


def tearDownModule():
    for key, was in _ENV_PRIOR.items():
        if was is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = was


def _where():
    """(rung, the helm frames from the call outward) of a spied call."""
    frame = sys._getframe(2)
    rung, chain = None, []
    while frame is not None:
        name = frame.f_code.co_name
        path = frame.f_code.co_filename
        if os.sep + "helm" + os.sep in path \
                and os.sep + "tests" + os.sep not in path:
            chain.append("%s:%s" % (os.path.basename(path), name))
            if rung is None and name in _RUNG_OF:
                rung = _RUNG_OF[name]
        frame = frame.f_back
    return rung or "?", " < ".join(chain[:8]) or "?"


class Spy(object):
    """Records every process spawn and every open of a file over BIG bytes,
    and lets each one proceed unchanged — a spy that refused would steer the
    code under test down a different path than production takes."""

    def __init__(self):
        self.spawns, self.big, self.opened = [], [], []

    def _big(self, file):
        if isinstance(file, int):
            return
        try:
            name = os.fsdecode(file)
            size = os.stat(name).st_size
        except (OSError, TypeError, ValueError):
            return
        self.opened.append(os.path.realpath(name))
        if size > BIG:
            self.big.append((name, size) + _where())

    def patches(self):
        spy = self
        real_popen = subprocess.Popen
        real_open = open
        real_os_open = os.open

        class SpyPopen(real_popen):
            def __init__(self_, args, *a, **k):
                spy.spawns.append((args,) + _where())
                super().__init__(args, *a, **k)

        def spy_open(file, *a, **k):
            spy._big(file)
            return real_open(file, *a, **k)

        def spy_os_open(file, *a, **k):
            spy._big(file)
            return real_os_open(file, *a, **k)

        def spawning(real):
            def wrapped(*a, **k):
                spy.spawns.append((real.__name__,) + _where())
                return real(*a, **k)
            return wrapped

        out = [mock.patch.object(subprocess, "Popen", SpyPopen),
               mock.patch("builtins.open", spy_open),
               mock.patch.object(io, "open", spy_open),
               mock.patch.object(os, "open", spy_os_open)]
        for name in ("fork", "posix_spawn", "posix_spawnp", "system",
                     "execv", "execve", "execvp", "execvpe"):
            if hasattr(os, name):
                out.append(mock.patch.object(os, name,
                                             spawning(getattr(os, name))))
        return out


class StopSpawnsNothingTest(LaneWorld, SeatsBase):

    def setUp(self):
        super().setUp()
        self._seam_prior = os.environ.get("HELM_STOP_GUARD_SEAM")
        self.build_lane_world()
        self.pad_ledger()
        self.seam_world()
        self.resident_refresh()

    def seam_world(self):
        """What the seam rung reads, planted where it would be read from: a
        LIVE peer room under another seat's lease (so the stop has a pair to
        judge), two seats rostered in the room this stop stands in (so an
        EXACT census produces the co-tenancy disclosure), and a gate-receipt
        ledger over 1 MB of real receipts (so a stop that read it would open
        a file over the bound)."""
        from helm import pk, seats
        self.peer = self.root + "-wt/lane-b"
        self.git("worktree", "add", "-q", "-b", "lane-b", self.peer)
        ok, msg, _lease = seats.claim("worktree:proj:lane-b", "bob", ttl=600,
                                      session="s-bob-" + os.urandom(3).hex(),
                                      repo=self.common)
        self.assertTrue(ok, msg)
        rows = pk.read_json(seats.roster_path(), {}) or {}
        for name in ("carol", "dave"):
            rows[name] = dict(rows.get(name) or {}, cwd=self.root)
        os.makedirs(os.path.dirname(seats.roster_path()), exist_ok=True)
        pk.atomic_write(seats.roster_path(), json.dumps(rows))
        self.pad_receipts()

    def pad_receipts(self):
        """Real gate receipts, each with its content id computed by gate's
        own function and serialised as the event ledger writes one, until
        the ledger is over 1 MB. `gate.receipts()` reads them all — the
        resident's control below — so they are not rows a reader skips."""
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines, size, n = [], 0, 0
        while size <= BIG + 4096:
            head = "%040x" % (0xabc0000 + n)
            row = {"v": 5, "event": "gate", "ts": "2026-09-24T00:00:00Z",
                   "head": head, "tree": "%040x" % (0xdef0000 + n),
                   "dirty": False, "head_after": head,
                   "tree_after": "%040x" % (0xdef0000 + n),
                   "dirty_after": False,
                   "interpreter": {"name": "cpython", "version": "3.14",
                                   "language": "3.14", "executable": "/x"},
                   "argv": ["python3"] + list(gate.SUITE), "suite": True,
                   "status": "OK", "ran": 1, "skipped": 0, "rc": 0,
                   "executed": True, "repo_id": self.root, "failures": [],
                   "failures_unreadable": False, "base_check": None,
                   "host": {"node": "n", "system": "Linux", "release": "r",
                            "id": "i"},
                   "label": None, "elapsed": 1.0, "wall": 1.0, "detail": ""}
            row["id"] = gate._receipt_id(row)
            line = json.dumps(row, ensure_ascii=False,
                              separators=(",", ":")) + "\n"
            lines.append(line)
            size += len(line.encode("utf-8"))
            n += 1
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write("".join(lines))
        self.assertGreater(os.path.getsize(path), BIG,
                           "control: the receipt ledger is over the bound")
        got, unavailable, _skipped = gate.receipts()
        self.assertFalse(unavailable, "control: the padded receipts read")
        self.assertGreaterEqual(len(got), n, "control: every pad row reads")
        self.receipts = os.path.realpath(path)

    def tearDown(self):
        if self._seam_prior is None:
            os.environ.pop("HELM_STOP_GUARD_SEAM", None)
        else:
            os.environ["HELM_STOP_GUARD_SEAM"] = self._seam_prior
        super().tearDown()

    def pad_ledger(self):
        """Grow the dispatch ledger past 1 MB with CLOSED rows — each a real
        dispatch event and its cancel, in the writer's own shape — so a stop
        that read the ledger whole would open a file over the bound, while
        the owed frontier (and the facts) stay the size of the fixture."""
        path = dispatches.ledger_path()
        with open(path, "rb") as f:
            first = json.loads(f.readline())
        self.assertEqual(first.get("event"), "dispatch")
        lines = []
        n = 0
        size = os.path.getsize(path)
        while size + sum(len(x) for x in lines) <= BIG + 4096:
            rid = "%032x" % (0xfeed0000 + n)
            row = dict(first, id=rid, chain_root=rid, lane="pad-%05d" % n,
                       operation_key="pad-%05d" % n, kind="build")
            lines.append(json.dumps(row, sort_keys=True) + "\n")
            lines.append(json.dumps({"v": 3, "event": "cancel", "seq": 1,
                                     "id": rid, "ts": first.get("ts"),
                                     "reason": "padding"}) + "\n")
            n += 1
        with open(path, "a") as f:
            f.write("".join(lines))
        self.assertGreater(os.path.getsize(path), BIG,
                           "control: the ledger is over the bound")
        snap, why = dispatches.snapshot()
        self.assertIsNone(why, "control: the padded ledger still folds")
        self.assertIn(self.row["id"], snap)
        self.ledger = os.path.realpath(path)

    def resident_refresh(self):
        """Run the resident's refresh the way `helm web` does, then take the
        stand-in away so the stop reads only what the resident wrote. On a
        tree with no resident there is nothing to run, and the stop does what
        that tree's stop does."""
        try:
            from helm import stopfacts_resident
        except ImportError:
            return
        why = stopfacts_resident.write(stopfacts_resident.compute())
        self.assertIsNone(why, why)
        fresh = getattr(self, "fresh_resident", None)
        if fresh is not None:
            fresh.off()

    def run_stop(self, seam="1", wiring="0"):
        """The stop-guard handler, as the wrapper runs it, under the spy, with
        the seam rung's pair witnesses recorded (the seam positive control)."""
        from helm import cli, stopfacts
        spy = Spy()
        real_pairs = getattr(stopfacts.View, "seam_pairs", None)
        spy.pairs = []

        def pairs(view, facts, rooms):
            got = real_pairs(view, facts, rooms)
            spy.pairs.append((sorted(rooms), got))
            return got
        env = {"HELM_NO_TREE_WARNING": "1", "HELM_STOP_GUARD_SEAM": seam,
               "HELM_STOP_GUARD_WIRING": wiring,
               "HELM_STOP_TIMING_AFTER": "off"}
        stdin = mock.Mock()
        stdin.buffer = io.BytesIO(self.stop_payload())
        err = io.StringIO()
        # `create=True` so the arm runs, and reddens on its spawn count, on a
        # tree whose seam rung still computes its own facts.
        patches = spy.patches() + [
            mock.patch.object(stopfacts.View, "seam_pairs", pairs,
                              create=True)]
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stderr", err):
            for p in patches:
                p.start()
            try:
                rc = cli.main(["chat", "stop-guard", "--hook-json",
                               "--seat", self.SEAT])
            finally:
                for p in reversed(patches):
                    p.stop()
        return rc, err.getvalue(), spy

    @staticmethod
    def entry(spawn):
        """Is this spawn one of the chat entry's named, pre-ladder reads?"""
        return spawn[-2] == "cli" and any(src in spawn[-1]
                                          for src in _ENTRY_SPAWNERS)

    def test_the_ladder_spawns_nothing_and_no_file_over_1MB_is_opened(self):
        """The whole ladder INCLUDING the seam rung (wiring alone is off, by
        its own kill switch, and named above)."""
        rc, err, spy = self.run_stop()
        # POSITIVE CONTROL: the lane in gate was exempted, which only the
        # claims rung reading the gate fact can do.
        self.assertEqual(rc, 0, err)
        self.assertIn("lane in gate", err)
        ladder = [s for s in spy.spawns if not self.entry(s)]
        self.assertEqual(ladder, [],
                         "the Stop ladder spawned %d process(es): %r"
                         % (len(ladder), ladder[:12]))
        self.assertEqual(spy.big, [],
                         "a Stop opened a file over 1 MB: %r" % spy.big[:6])
        self.assertNotIn(self.ledger, spy.opened,
                         "a Stop opened the dispatch ledger")
        self.assertNotIn(self.receipts, spy.opened,
                         "a Stop opened the gate-receipt ledger")
        # SEAM POSITIVE CONTROLS: the co-tenancy disclosure needs an EXACT
        # census, and the rung judged the pair witnesses of its own rooms and
        # the live peer EXACT — so it read the resident's rows, not nothing.
        self.assertIn("SEAM RUNG BLIND SPOT", err)
        self.assertTrue(spy.pairs, "control: the seam rung judged no pair")
        rooms, fresh = spy.pairs[-1]
        self.assertIn(os.path.abspath(self.peer), rooms)
        self.assertTrue(fresh.exact, fresh)
        self.assertNotIn("composition-seam rung has no exact reading", err)

    def test_the_resident_is_what_read_the_big_ledgers(self):
        """THE OTHER HALF OF THE CONTROL: the facts the stop judged were
        computed against the receipt ledger over the bound — the resident
        read it, off the hook, and recorded the mark the stop compared."""
        from helm import stopfacts
        snap, why = stopfacts.load()
        self.assertIsNone(why, why)
        facts = snap["seam"]["roots"][os.path.realpath(self.root)]
        self.assertGreater(facts["receipts"][gate.receipts_path()][1], BIG)
        self.assertIn(os.path.abspath(self.peer), facts["rooms"])

    def test_every_spawn_left_is_one_the_lane_names_as_a_follow_up(self):
        """Seam and wiring ON: whatever wiring and the entry still spawn,
        nothing else may — the seam rung included."""
        rc, err, spy = self.run_stop(seam="1", wiring="1")
        self.assertIn(rc, (0, 2), err)
        self.assertIn(self.RES, err, "control: the claims rung spoke")
        strays = [s for s in spy.spawns if not self.entry(s)
                  and s[-2] not in set(_NOT_YET_RESIDENT.values())]
        self.assertEqual(strays, [],
                         "a spawn the lane does not name: %r" % strays[:12])
        self.assertEqual(spy.big, [],
                         "a Stop opened a file over 1 MB: %r" % spy.big[:6])


if __name__ == "__main__":
    unittest.main()
