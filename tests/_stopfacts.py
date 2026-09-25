#!/usr/bin/env python3
"""A `helm web` resident that is exactly up to date, for tests that drive the
stop guard.

IN PRODUCTION THE STOP GUARD READS FACTS A RESIDENT WROTE (helm/stopfacts.py),
and a test process has no resident. Most stop-guard arms are about what a
rung DECIDES from those facts — a gate exemption, a room's findings, a spiral
— not about how fresh they are, so they run against this stand-in: before
every read the snapshot is recomputed with the resident's own `compute` and
written through its own `write`, which is the state a live resident reaches
within its poll. Freshness itself (STALE, ABSENT, a moved witness) is what
tests/test_stopfacts.py drives, with this stand-in OFF.

NOT A BYPASS. Nothing here computes a fact the resident would not: the facts
come from `stopfacts_resident.compute`, the file from `stopfacts_resident.
write`, and the reader under test is the shipped `stopfacts.read`.
"""
from unittest import mock


class Fresh(object):
    """The stand-in, switchable: `off()` leaves whatever snapshot is on disk
    for the reader to judge on its own, which is how an arm measures the
    guard with NO resident refreshing behind it."""

    def __init__(self):
        from helm import pk, stopfacts, stopfacts_resident
        real = stopfacts.load
        # THE RESIDENT IS ANOTHER PROCESS, so an arm that breaks THIS
        # process's writes (a read-only chat dir, patched in through
        # `pk.atomic_write`) must not also break the resident's: the writer it
        # uses is the one that existed before the arm began.
        real_write = pk.atomic_write

        def load(p=None):
            target = p or stopfacts.path()
            with mock.patch.object(pk, "atomic_write", real_write):
                stopfacts_resident.write(stopfacts_resident.compute(), target)
            return real(p)

        self._patchers = [mock.patch.object(stopfacts, "load",
                                            side_effect=load)]
        self.on = False

    def start(self):
        if not self.on:
            for p in self._patchers:
                p.start()
            self.on = True
        return self

    def off(self):
        if self.on:
            for p in reversed(self._patchers):
                p.stop()
            self.on = False


def always_fresh(case):
    """Install the stand-in for the life of `case` (cleanup registered);
    returns it, so an arm can switch it off."""
    fresh = Fresh().start()
    case.addCleanup(fresh.off)
    return fresh


class LaneWorld(object):
    """A project, one lane worktree, a review row at the lane's HEAD and this
    session's claim on the lane — the shape a gate-pending stop takes. Mixed
    into a `SeatsBase` subclass; call `build_lane_world()` from setUp.

    The claim RECORDS its repository, as `helm work claim` does, so the
    resident anchors the lease in the lease's own repository."""

    SEAT = "alice"
    RES = "worktree:proj:lane-g"

    def git(self, *args, cwd=None):
        import subprocess
        r = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                            *args], cwd=cwd or self.root, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def build_lane_world(self):
        import os
        from helm import dispatches, seats
        from tests._tmphome import helm_tree, pin_admission, pin_dispatch_home
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        self.git("init", "-q", "-b", "main")
        pin_dispatch_home(self, self.root)
        pin_admission(self)
        helm_tree(self, self.root)
        self.git("commit", "-q", "--allow-empty", "-m", "tip")
        self.head = self.git("rev-parse", "HEAD")
        self.wt = self.root + "-wt/lane-g"
        self.git("worktree", "add", "-q", "-b", "lane-g", self.wt)
        self.sid = "s-facts-" + os.urandom(6).hex()
        # THE SEAT STANDS IN ITS PROJECT, as a live stop's does. SeatsBase's
        # tearDown puts the process back where it started.
        os.chdir(self.root)
        self.common = os.path.realpath(os.path.join(self.root, ".git"))
        ok, msg, _lease = seats.claim(self.RES, self.SEAT, ttl=600,
                                      session=self.sid, repo=self.common)
        self.assertTrue(ok, msg)
        self.row = self.plant()

    def plant(self, lane="lane-g", ref=None, kind="review"):
        """A REAL ledger row through the real writer, delivery observed."""
        import os
        from unittest import mock as _mock
        from helm import dispatches
        with _mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "gate-fixture"}):
            row = dispatches.add("ds4pro", lane, ref=ref or self.head,
                                 kind=kind, notify=False, repo=self.root,
                                 new_work=True)
        self.assertIsNotNone(row, "plant: the real writer refused")
        dispatches._mark_delivered(row["id"], ref or self.head)
        return row

    def stop_payload(self):
        import json
        return json.dumps({"session_id": self.sid, "cwd": self.root}).encode()

    def stop(self):
        """One Stop through the hook verb, as the harness drives it."""
        return self.cmd("stop-guard", ["--hook-json", "--seat", self.SEAT],
                        stdin=self.stop_payload())

    def resident_writes(self, **over):
        """One resident refresh, the way `helm web` runs it, with any header
        field overridden (a stand-in for other code, an old write)."""
        from helm import stopfacts_resident
        snap = stopfacts_resident.compute()
        snap.update(over)
        why = stopfacts_resident.write(snap)
        self.assertIsNone(why, why)
        return snap
