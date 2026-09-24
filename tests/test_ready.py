#!/usr/bin/env python3
"""helm ready — the five-signal fleet-readiness gauge (#163).

The gauge is a READER over five existing authorities, and these tests pin
exactly that: every signal has a green, red (where one exists), and UNKNOWN
arm; the composition law (one red = NOT READY, no red + one UNKNOWN =
UNKNOWN, all green = READY, walled family = READY-with-note); the exit-code
contract (0/1/2, distinct on purpose); and the NO-FRESH-SWEEP pin — signal 4
must read proxywatch's recorded state, so its sweep machinery is
booby-trapped here and the mutation "make signal 4 sweep" turns this file
red instead of spending 8 upstream tokens per family per gauge.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-ready-", var="HELM_HOME")

from helm import harness, proxywatch, ready, web, web_ui_loader  # noqa: E402
from helm import seat  # noqa: E402,F401 — the facade the impl imports below require
from helm import seat_launch_assets, seat_lifecycle  # noqa: E402
from helm import seat_lifecycle_runtime as seat_runtime  # noqa: E402


def _out(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a)
    return rc, buf.getvalue()


class _Adapter:
    """A fake metaharness adapter — the panes()/list() surface only."""

    def __init__(self, name="orca", panes=None, listed=None, err=None):
        self.name = name
        if panes is not None or err is not None:
            self.panes = lambda: (panes or [], err)
        if listed is not None:
            self.list = lambda: listed


class _NoPanesAdapter:
    """herdr-shaped: a CLI list(), no RPC panes surface."""

    def __init__(self, listed=None, boom=None):
        self.name = "herdr"
        self._listed, self._boom = listed, boom

    def list(self):
        if self._boom:
            raise RuntimeError(self._boom)
        return self._listed


class DaemonSignalTest(unittest.TestCase):
    def test_no_metaharness_is_UNKNOWN_not_red(self):
        row = ready.signal_daemon(detect=lambda: None)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("unmeasurable", row["evidence"])

    def test_detection_crash_is_UNKNOWN(self):
        def boom():
            raise OSError("PATH scan died")
        row = ready.signal_daemon(detect=boom)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("PATH scan died", row["evidence"])

    def test_daemon_answering_is_GREEN_with_the_pane_count(self):
        ad = _Adapter(panes=[{"handle": "t1"}, {"handle": "t2"}])
        row = ready.signal_daemon(detect=lambda: ad)
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("2 panes listed", row["evidence"])

    def test_daemon_dark_is_RED_with_the_adapters_own_reason_and_a_repair(self):
        ad = _Adapter(err="orca rpc terminal.list: no usable unix transport")
        row = ready.signal_daemon(detect=lambda: ad)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("no usable unix transport", row["evidence"])
        self.assertIn("start orca", row["repair"])

    def test_an_adapter_without_the_rpc_surface_probes_via_its_cli_list(self):
        row = ready.signal_daemon(detect=lambda: _NoPanesAdapter(listed=[{}]))
        self.assertEqual(row["state"], ready.GREEN)
        red = ready.signal_daemon(
            detect=lambda: _NoPanesAdapter(boom="daemon not running"))
        self.assertEqual(red["state"], ready.RED)
        self.assertIn("daemon not running", red["evidence"])


def _lv(state, evidence):
    return {"state": state, "evidence": evidence, "blocked_on": None,
            "detail": None}


class SeatsSignalTest(unittest.TestCase):
    def test_blind_register_refuses_the_whole_signal(self):
        """A partly-readable register renders NO list — a short one would
        read as complete (seat._rebind and doctor._is_genesis precedent)."""
        row = ready.signal_seats(registered=lambda: (["codex"], True))
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("PARTLY unreadable", row["evidence"])
        self.assertNotIn("codex", row["evidence"])

    def test_register_crash_is_UNKNOWN(self):
        def boom():
            raise OSError("seats dir vanished")
        row = ready.signal_seats(registered=boom)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("seats dir vanished", row["evidence"])

    def test_empty_register_is_GREEN_with_the_honest_note(self):
        row = ready.signal_seats(registered=lambda: ([], False))
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("register", row["note"])

    def test_every_pane_live_is_GREEN_including_blocked_and_adopted_states(self):
        states = {"a": "RUNNING", "b": "IDLE", "c": "BLOCKED_ON_HUMAN",
                  "d": "LIVE"}
        row = ready.signal_seats(
            registered=lambda: (list(states), False),
            liveness=lambda n, repair: _lv(states[n], "pane-tail"))
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("4 registered seats", row["evidence"])

    def test_a_GONE_pane_is_RED_named_with_its_evidence_and_the_resume_verb(self):
        states = {"codex": ("GONE", "pid-dead"), "kimi": ("RUNNING", "pane-tail")}
        row = ready.signal_seats(
            registered=lambda: (list(states), False),
            liveness=lambda n, repair: _lv(*states[n]))
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("codex GONE (pid-dead)", row["evidence"])
        self.assertIn("helm seat resume", row["repair"])

    def test_an_exited_agent_under_a_live_pane_is_also_down(self):
        row = ready.signal_seats(
            registered=lambda: (["codex"], False),
            liveness=lambda n, repair: _lv("EXITED_PANE_ALIVE", "pane-tail"))
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("codex EXITED_PANE_ALIVE (pane-tail)", row["evidence"])
        self.assertIn("helm seat resume", row["repair"])

    def test_an_unprovable_pane_is_UNKNOWN_never_folded_into_pass_or_fail(self):
        row = ready.signal_seats(
            registered=lambda: (["codex", "kimi"], False),
            liveness=lambda n, repair: _lv("RUNNING", "pane-tail") if n == "kimi"
            else _lv("UNKNOWN", "stale-handle"))
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("codex: stale-handle", row["evidence"])

    def test_down_outranks_unknown_and_the_unknowns_ride_the_note(self):
        states = {"a": ("GONE", "pid-dead"), "b": ("UNKNOWN", "headless")}
        row = ready.signal_seats(
            registered=lambda: (list(states), False),
            liveness=lambda n, repair: _lv(*states[n]))
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("b: headless", row["note"])

    def test_a_crashing_liveness_probe_lands_in_the_unknown_bucket(self):
        def boom(name, repair):
            raise RuntimeError("adapter died")
        row = ready.signal_seats(registered=lambda: (["x"], False),
                                 liveness=boom)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("adapter died", row["evidence"])


class SeatsReadOnlyFanOutTest(unittest.TestCase):
    """The two halves of task/2887's cure, pinned separately.

    (a) A READ MUST NOT REPAIR. `seat_liveness` defaults to `repair=True` and
        hands it to the registered-pane resolver, which rewrites the spawn
        register to cure a stale handle. The gauge renders and never acts, so
        it must say `repair=False` — and the WHOLE fleet cost lived here: the
        same 20 seats measured 36.12s with repair and 14.73s without, with the
        same verdict for every seat.
    (b) A FASTER WRONG ANSWER IS NOT A CURE. The fan-out is bounded-parallel,
        so the per-seat verdicts and the order they are reported in must be
        bit-identical to a serial walk.
    """

    @staticmethod
    def _states(n):
        """A deterministic, MIXED per-seat world — one of each bucket, so a
        cure that flattened the verdicts could not pass by luck."""
        cycle = (("RUNNING", "pane-tail"), ("UNKNOWN", "stale-handle"),
                 ("IDLE", "pane-tail"), ("UNKNOWN", "read-failed"))
        return {"seat-%02d" % i: cycle[i % len(cycle)] for i in range(n)}

    def test_every_seat_is_probed_read_only(self):
        """repair=False on EVERY seat, not on a first one that stands for the
        rest. The call log is the observable and it is checked whole."""
        states = self._states(6)
        seen = []

        def liveness(name, repair):
            seen.append((name, repair))
            return _lv(*states[name])

        row = ready.signal_seats(registered=lambda: (list(states), False),
                                 liveness=liveness)
        # POSITIVE CONTROL on the same observable the claim is about: the log
        # is non-empty, it is exactly six entries long, and it covers the whole
        # register — so "no repair=True in it" cannot be true by the probe
        # never having run.
        self.assertEqual(len(seen), 6)
        self.assertEqual(len(states), 6)
        self.assertEqual(sorted(n for n, _ in seen), sorted(states))
        self.assertEqual([r for _, r in seen], [False] * len(states))
        self.assertIn("seat-01: stale-handle", row["evidence"])
        self.assertEqual(row["state"], ready.UNKNOWN)

    def test_the_fan_out_is_parallel_and_bounded(self):
        """Both bounds, in one run: more than one probe is in flight at once
        (or nothing was parallelised) and never more than the declared width
        (or the bound is decorative)."""
        # AN EXACT MULTIPLE OF THE WIDTH, deliberately: the barrier below
        # releases only when a full wave has gathered, so a trailing partial
        # wave would sit there until the timeout instead of measuring anything.
        waves = 3
        names = self._states(ready._SEAT_PROBE_WORKERS * waves)
        lock = threading.Lock()
        live = {"now": 0, "peak": 0}
        gate = threading.Barrier(ready._SEAT_PROBE_WORKERS, timeout=15)

        def liveness(name, repair):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            try:
                # Every wave must fill before any of it may retire, so the
                # high-water mark is the pool's real width and not a timing
                # accident that a fast machine could reduce to 1.
                gate.wait()
            finally:
                with lock:
                    live["now"] -= 1
            return _lv(*names[name])

        row = ready.signal_seats(registered=lambda: (list(names), False),
                                 liveness=liveness)
        # POSITIVE CONTROL on the ceiling itself: an upper bound proves nothing
        # against a width nobody pinned, so the declared number is asserted in
        # the same call as the bound that reads it.
        self.assertEqual(ready._SEAT_PROBE_WORKERS, 8)
        self.assertGreater(live["peak"], 1)
        self.assertLessEqual(live["peak"], ready._SEAT_PROBE_WORKERS)
        self.assertIn("seat-01: stale-handle", row["evidence"])
        self.assertEqual(row["state"], ready.UNKNOWN)

    def test_the_parallel_walk_gives_the_serial_walks_row_verbatim(self):
        """The per-seat equality proof, in-tree: a width-1 walk and the real
        bounded walk over one world must produce the SAME ROW — which pins the
        per-seat clauses AND their order, not merely the state counts."""
        states = self._states(20)
        registered = (lambda: (list(states), False))

        def liveness(name, repair):
            return _lv(*states[name])

        with mock.patch.object(ready, "_SEAT_PROBE_WORKERS", 1):
            serial = ready.signal_seats(registered=registered,
                                        liveness=liveness)
        parallel = ready.signal_seats(registered=registered,
                                      liveness=liveness)
        # POSITIVE CONTROL, unconditional and on both sides: each row is a
        # real, populated verdict naming a seat by name. An equality between
        # two empty rows, or between two rows nobody proved were built, would
        # prove nothing.
        self.assertEqual(serial["state"], ready.UNKNOWN)
        self.assertIn("seat-01: stale-handle", serial["evidence"])
        self.assertIn("seat-01: stale-handle", parallel["evidence"])
        self.assertEqual(serial, parallel)


# ---------------------------------------------------------------------------
# THE REPAIR AXIS — a real multi-seat world, built twice.
#
# `_probe_seat` says `repair=False`. What that rests on is an EQUALITY: the
# read-only walk must return the same verdict for every seat as the repairing
# walk did. The arms above pin the call log (the flag reached every seat) and
# the parallel axis (width changes no row); neither of them compares the two
# REPAIR verdicts, so the fixture below exists to.
#
# THE MUTATOR IS REAL AND ONE LAYER DOWN. `seat_lifecycle_runtime.
# _resolve_registered_pane` re-proves a drifted handle through the seat's
# recorded pane key; with `repair=True` it REWRITES spawn.json to the
# replacement, and with `repair=False` it returns the same proven handle and
# leaves the register alone. So this world drives the REAL resolver over a REAL
# spawn register on disk. The only stubs are the two things that are not the
# subject: the metaharness adapter (an inventory, a pane-key resolver and one
# tail per readable pane) and the live-session identity probe.
# ---------------------------------------------------------------------------

#: The five ways a seat can meet the resolver, and which of them can mutate.
_LIVE = "live"                  # the registered handle is listed live
_DRIFTED = "drifted"            # registered handle gone, pane key re-proves it
_STALE = "stale"                # registered handle gone, nothing re-proves it
_UNREADABLE = "unreadable"      # live pane, the read itself fails
_UNREGISTERED = "unregistered"  # no spawn record — the resolver is never reached

#: One seat per row, ONE spec for every home this module builds. Deliberately
#: mixed: six seats whose handle the repairing walk really does rewrite, and
#: fourteen that exercise the branches where repair has nothing to do — an
#: equality over a world where the mutator never fires would prove nothing.
_WORLD = (
    ("seat-01", _LIVE, "running"),
    ("seat-02", _DRIFTED, "running"),
    ("seat-03", _STALE, None),
    ("seat-04", _UNREADABLE, None),
    ("seat-05", _LIVE, "idle"),
    ("seat-06", _DRIFTED, "idle"),
    ("seat-07", _LIVE, "running"),
    ("seat-08", _UNREGISTERED, None),
    ("seat-09", _LIVE, "exited"),
    ("seat-10", _DRIFTED, "unrecognized"),
    ("seat-11", _LIVE, "running"),
    ("seat-12", _DRIFTED, "running"),
    ("seat-13", _STALE, None),
    ("seat-14", _UNREADABLE, None),
    ("seat-15", _LIVE, "idle"),
    ("seat-16", _DRIFTED, "exited"),
    ("seat-17", _LIVE, "running"),
    ("seat-18", _STALE, None),
    ("seat-19", _LIVE, "idle"),
    ("seat-20", _DRIFTED, "running"),
)

#: Pane tails the classifier's own pattern table recognises, one per verdict
#: this world wants — plus one it must refuse to recognise.
_TAILS = {
    "running": "doing work\n  ⏵⏵ bypass permissions on · 1 monitor · "
               "esc to interrupt",
    "idle": "done, parked\n❯ \n  ⏵⏵ bypass permissions on · 1 monitor · "
            "← for agents",
    "exited": "Resume this session with: claude --resume "
              "00000000-0000-4000-8000-000000000000",
    "unrecognized": "pane output matching no state this classifier knows",
}

_DRIFTED_SEATS = frozenset(n for n, bucket, _t in _WORLD if bucket == _DRIFTED)


#: The provider family every fixture seat belongs to. A fixture seat is named
#: by the house convention (`seat-NN`), which the name grammar cannot resolve
#: to a family, so `_seat_family` is answered for it — see `_family_seam`. The
#: family is REAL, because everything downstream of it (the instance directory,
#: the pool-wall anchor, the upstream row) is real and derives from it.
_FAMILY = "codex"


def _world_names():
    return [name for name, _bucket, _tail in _WORLD]


def _suffix(seat_name):
    return seat_name.rpartition("-")[2]


def _born_record(seat_name, bucket):
    """The spawn register row this seat is born with. A drifted seat's recorded
    handle names a pane the inventory does not list; its pane key is what the
    resolver re-proves it through."""
    i = _suffix(seat_name)
    return {"v": 1, "seat": seat_name, "harness": "orca", "room": "main",
            "session": "spawn-%s" % i,
            "pane_key": "tab-%s:leaf" % i,
            "worktree_id": "workspace:/w-%s" % i,
            "handle": ("pane-%s" % i) if bucket in (_LIVE, _UNREADABLE)
            else "pane-%s-drifted" % i}


@contextlib.contextmanager
def _pinned_home(root):
    """Every path helm derives comes off HELM_HOME, so a world IS its home."""
    with mock.patch.dict(os.environ, {"HELM_HOME": root}):
        os.environ.pop("MELD_HOME", None)
        yield


def _build_home(root):
    """One untouched world on disk, written by THIS function and no other — so
    two homes can differ in nothing a walk is able to read."""
    with _pinned_home(root):
        for seat_name, bucket, _tail in _WORLD:
            if bucket == _UNREGISTERED:
                continue
            d = seat_launch_assets._instance_dir(_FAMILY, seat_name)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "spawn.json"), "w") as f:
                json.dump(_born_record(seat_name, bucket), f, sort_keys=True)
    return root


def _register(root):
    """{seat: spawn record} — the state a repairing walk is able to rewrite,
    read back through the same path derivation that wrote it."""
    out = {}
    with _pinned_home(root):
        for seat_name, bucket, _tail in _WORLD:
            if bucket == _UNREGISTERED:
                continue
            d = seat_launch_assets._instance_dir(_FAMILY, seat_name)
            with open(os.path.join(d, "spawn.json")) as f:
                out[seat_name] = json.load(f)
    return out


class _FleetOrca(harness._CLIAdapter):
    """orca as this world's seats meet it: an inventory, a pane-key resolver
    that answers only for the panes that were reminted, and one tail per
    readable pane. `tails` overrides ONE seat's tail — the planted difference
    the comparator control needs, and nothing else uses it."""
    name, path = "orca", "/bin/orca"

    def __init__(self, tails=None):
        self.rows, self.resolved, self.tails, self.unreadable = [], {}, {}, set()
        override = tails or {}
        for seat_name, bucket, tail in _WORLD:
            i = _suffix(seat_name)
            row = {"handle": "pane-%s" % i, "pty_id": "pty-%s" % i,
                   "worktree_id": "workspace:/w-%s" % i,
                   "status": "connected", "writable": True, "orphaned": False}
            readable = None
            if bucket in (_LIVE, _UNREADABLE):
                self.rows.append(row)
                readable = row["handle"]
            elif bucket == _DRIFTED:
                # The reminted pane: listed, and reachable ONLY through the
                # seat's recorded pane key — which is what makes this seat's
                # handle repairable rather than merely dead.
                row = dict(row, handle="pane-%s-new" % i,
                           pty_id="pty-%s-new" % i)
                self.rows.append(row)
                self.resolved["tab-%s:leaf" % i] = {
                    "handle": row["handle"], "pty_id": row["pty_id"]}
                readable = row["handle"]
            if bucket == _UNREADABLE:
                self.unreadable.add(readable)
            elif readable is not None:
                self.tails[readable] = override.get(seat_name) or _TAILS[tail]

    def list(self):
        return [dict(row) for row in self.rows]

    def resolve_pane(self, pane_key):
        proven = self.resolved.get(pane_key)
        if proven is None:
            # orca's POSITIVE no-such-pane answer, as the adapter raises it.
            raise harness.HarnessError(
                "orca runtime rpc terminal.resolvePane: terminal_not_found")
        return dict(proven)

    def read(self, handle, limit=3000, timeout=60):
        if handle in self.unreadable:
            raise OSError("pane read failed")
        return self.tails[handle]


def _live_identity(d, session, identity=None, record=None):
    """The seat's own recorded pane identity, proven live. Stubbed because a
    /proc walk for a live session is not this axis's subject; it answers from
    the RECORD the resolver is deciding about, so it cannot hand two walks
    different identities."""
    return {"pid": 4242, "pane_key": record["pane_key"],
            "worktree_id": record["worktree_id"]}, None


@contextlib.contextmanager
def _family_seam(adapter):
    """The two seams a fixture seat needs, and NOTHING below them.

    `_seat_family` is answered because a house-convention fixture name carries
    no family in its grammar; it is patched in BOTH modules that ask it,
    because each one from-imports the name and a single patch would leave the
    other resolving the real thing. `harness.detect` hands the resolver this
    world's orca. Everything the axis is actually about — the register read,
    the identity proof, the repair write, the pane read, the classification —
    stays the shipped code.
    """
    with mock.patch.object(harness, "detect", return_value=adapter), \
            mock.patch.object(seat_runtime, "_live_session_orca_identity",
                              side_effect=_live_identity), \
            mock.patch.object(seat_lifecycle, "_seat_family",
                              return_value=(_FAMILY, None)), \
            mock.patch.object(seat_runtime, "_seat_family",
                              return_value=(_FAMILY, None)):
        yield


def _walk(root, axis, tails=None):
    """ready's own seat walk over one home, with the repair flag SUBSTITUTED at
    the liveness seam.

    `axis=True` reconstructs the PRE-CURE gauge: same signal, same bounded
    pool, same world, one flag apart. It has to be injected here because
    `_probe_seat` no longer passes anything but False — which is the cure, and
    which the returned flag log lets a caller assert rather than assume.

    The seam's parameter is named `repair` because that is the KEYWORD
    `_probe_seat` calls it by; a seam that renames it takes the exception arm
    on every seat and reports a whole fleet of crashes as an equality.

    Returns (composed row, {seat: liveness row}, [flag the seam received])."""
    rows, flags = {}, []
    adapter = _FleetOrca(tails)

    def liveness(seat_name, repair):
        flags.append(repair)
        row = seat_lifecycle.seat_liveness(seat_name, repair=axis)
        rows[seat_name] = row
        return row

    with _pinned_home(root), _family_seam(adapter):
        signal = ready.signal_seats(
            registered=lambda: (_world_names(), False), liveness=liveness)
    return signal, rows, flags


_ABSENT = object()


def _disagreements(left, right):
    """{seat: {field: (left, right)}} over the UNION of both sides' seats and
    both rows' fields.

    Walking one side's keys is how a comparator goes blind to a field that one
    walk emits and the other drops, so the union is the whole point — and
    `_ABSENT` keeps a missing field from comparing equal to a present None."""
    out = {}
    for seat_name in sorted(set(left) | set(right)):
        a, b = left.get(seat_name) or {}, right.get(seat_name) or {}
        diff = {k: (a.get(k, _ABSENT), b.get(k, _ABSENT))
                for k in sorted(set(a) | set(b))
                if a.get(k, _ABSENT) != b.get(k, _ABSENT)}
        if diff:
            out[seat_name] = diff
    return out


class SeatsRepairAxisEqualityTest(unittest.TestCase):
    """THE REPAIR AXIS, in tree: `repair=False` gives the same verdict PER SEAT
    as `repair=True`.

    TWO EQUIVALENT UNTOUCHED WORLDS, and here is how that is guaranteed rather
    than hoped. A repairing walk REWRITES the register it reads, so two walks
    over one fixture would have the first decide what the second could see.
    Each walk therefore gets its own home; both homes are built by ONE factory
    from ONE spec; and `_pair` ASSERTS the two registers are equal before
    either walk runs, so the equivalence is measured and not assumed. After the
    walks the same registers are read back: the repairing one must have moved
    exactly the drifted seats, and the read-only one must not have moved at
    all. Without that pair of checks the equality could hold because the
    mutator never fired.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ready-repair-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _pair(self, tag):
        """Two homes from one spec, PROVED equal before anything walks them."""
        repairing = _build_home(os.path.join(self.tmp, tag + "-repairing"))
        readonly = _build_home(os.path.join(self.tmp, tag + "-readonly"))
        born = _register(repairing)
        self.assertEqual(born, _register(readonly),
                         "the two walks must start from the same world")
        self.assertEqual(len(born), len(_WORLD) - 1)   # one seat is registerless
        return repairing, readonly, born

    def test_the_read_only_walk_gives_the_repairing_walks_verdict_per_seat(self):  # noqa: VACUOUS_ASSERTION — the empty-disagreement assert carries an unconditional positive control on the SAME comparator call, re-asked against one perturbed row
        """The claim the cure rests on, compared seat by seat and field by
        field — never by counts, and never by the call log."""
        repairing, readonly, born = self._pair("equality")
        repair_row, repair_seats, repair_flags = _walk(repairing, True)
        read_row, read_seats, read_flags = _walk(readonly, False)

        # POSITIVE CONTROL, both sides: every seat in the register produced a
        # verdict on both walks. An equality between two short maps, or two
        # empty ones, would prove nothing about the seats that went missing.
        self.assertEqual(sorted(repair_seats), sorted(_world_names()))
        self.assertEqual(sorted(read_seats), sorted(_world_names()))
        self.assertEqual(len(_world_names()), 20)
        # The flag `_probe_seat` really passes, on every seat of both walks —
        # the axis is substituted inside the seam, so this stays False for both
        # and pins that the production caller is still the read-only one.
        self.assertEqual(repair_flags, [False] * 20)
        self.assertEqual(read_flags, [False] * 20)

        # THE WORLD IS RICH, asserted rather than described: an equality over
        # twenty seats that all landed in one bucket would be a much smaller
        # claim than it looks.
        seen = {(row["state"], row["evidence"]) for row in read_seats.values()}
        for cell in (("RUNNING", "pane-tail"), ("IDLE", "pane-tail"),
                     ("EXITED_PANE_ALIVE", "pane-tail"),
                     ("UNKNOWN", "pane-tail"), ("UNKNOWN", "stale-handle"),
                     ("UNKNOWN", "read-failed"), ("UNKNOWN", "no-record")):
            self.assertIn(cell, seen)

        # THE MUTATOR REALLY FIRED. The repairing walk moved exactly the
        # drifted seats' handles onto their reminted panes; the read-only walk
        # left its register byte-for-byte as it was born. Both halves are
        # needed: the first stops the equality being vacuous, the second is the
        # promise the gauge makes about reading.
        after = _register(repairing)
        moved = {n for n, rec in after.items() if rec != born[n]}
        self.assertEqual(moved, set(_DRIFTED_SEATS))
        for name in moved:
            self.assertEqual(born[name]["handle"],
                             "pane-%s-drifted" % _suffix(name))
            self.assertEqual(after[name]["handle"],
                             "pane-%s-new" % _suffix(name))
        self.assertEqual(_register(readonly), born)

        # THE CLAIM: same verdict, per seat, whole row. Nothing is excluded —
        # see the detail arm below for why nothing needs to be.
        self.assertEqual(_disagreements(repair_seats, read_seats), {})
        # POSITIVE CONTROL ON THIS EXACT COMPARATOR CALL, unconditional: an
        # empty answer is worth nothing until the same call is shown giving a
        # non-empty one. Perturb one seat of the very map just compared — the
        # dedicated control below plants its difference in the WORLD, this one
        # plants it in the comparator's own input.
        perturbed = dict(read_seats)
        perturbed["seat-07"] = dict(read_seats["seat-07"], state="GONE")
        self.assertEqual(_disagreements(repair_seats, perturbed),
                         {"seat-07": {"state": ("RUNNING", "GONE")}})
        # ...and therefore the composed signal the gauge renders is the same
        # row, down to the clause order in its evidence and note.
        self.assertEqual(repair_row, read_row)
        self.assertEqual(read_row["state"], ready.RED)
        self.assertIn("seat-09 EXITED_PANE_ALIVE", read_row["evidence"])
        self.assertIn("seat-03: ", read_row["note"])

    def test_the_field_the_two_resolvers_write_differently_is_dropped(self):  # noqa: VACUOUS_ASSERTION — the resolver control at the end produces BOTH detail strings unconditionally, so the absence asserts above are read against a field proved non-empty one layer down
        """THE ONE KNOWN LEGITIMATE DIFFERENCE, named and pinned instead of
        excused by a loosened comparison.

        The two paths narrate the same outcome differently: repairing says it
        REWROTE the handle, dry-running says it PROVED the same replacement and
        left the register alone. That string is the resolver's `detail`, and it
        is the only field the two paths write differently. `_seat_liveness_row`
        DROPS it the moment a handle resolves: a classified pane's row carries
        `detail: None` outright, and a row whose tail the classifier REFUSES
        overwrites the field with its own sentence, which both walks write
        identically. Either way the resolver's narration never reaches the
        gauge, which reads `state` and `evidence` and nothing else. That is why
        the equality arm excludes no field — on a resolved pane there is
        nothing left to exclude — and this arm proves each half of that.
        """
        repairing, readonly, _born = self._pair("detail")
        _row, repair_seats, _f = _walk(repairing, True)
        _row, read_seats, _f = _walk(readonly, False)
        for name, bucket, tail in _WORLD:
            if bucket != _DRIFTED:
                continue
            for row in (repair_seats[name], read_seats[name]):
                self.assertNotIn("repaired spawn handle", row["detail"] or "")
                self.assertNotIn("dry-run", row["detail"] or "")
                if tail == "unrecognized":
                    # The refused-tail row returns before the authority fields
                    # are attached, so it carries no handle on EITHER walk —
                    # which is a field the union comparator can see is absent
                    # on both, and would have caught had one walk grown one.
                    self.assertNotIn("handle", row)
                    continue
                self.assertIsNone(row["detail"])
                self.assertEqual(row["handle"], "pane-%s-new" % _suffix(name))
            self.assertEqual(repair_seats[name]["detail"],
                             read_seats[name]["detail"])

        # POSITIVE CONTROL on the dropped field: it really would have differed.
        # A third, untouched home, and the DRY RUN GOES FIRST — running the
        # repair first would leave the handle already current and the dry run
        # with nothing to prove, which is the shape where a control quietly
        # measures nothing.
        home = _build_home(os.path.join(self.tmp, "detail-resolver"))
        name = sorted(_DRIFTED_SEATS)[0]
        adapter = _FleetOrca()
        with _pinned_home(home), _family_seam(adapter):
            d = seat_launch_assets._instance_dir(_FAMILY, name)
            _ad, dry_handle, dry_detail = \
                seat_runtime._resolve_registered_pane(name, d=d, repair=False)
            _ad, fix_handle, fix_detail = \
                seat_runtime._resolve_registered_pane(name, d=d, repair=True)
        self.assertEqual(dry_handle, "pane-%s-new" % _suffix(name))
        self.assertEqual(dry_handle, fix_handle)
        self.assertNotEqual(dry_detail, fix_detail)
        self.assertIn("register unchanged in dry-run", dry_detail)
        self.assertIn("repaired spawn handle", fix_detail)

    def test_the_comparator_reports_exactly_a_planted_per_seat_difference(self):
        """AN EQUALITY ARM PASSES AGAINST A COMPARATOR THAT CANNOT SEE.

        Plant one difference in one seat: the pane one live seat renders is
        IDLE to the read-only walk and RUNNING to the repairing one, every
        other seat untouched. The comparator must name that seat and no other,
        and inside it must name the field. `remediation` rides along because
        `_with_remediation` DERIVES it from `state`, and that it is reported
        too is the point — the comparator answers per field, not per row."""
        repairing, readonly, _born = self._pair("control")
        planted = "seat-07"
        self.assertIn((planted, _LIVE, "running"), _WORLD)
        _row, repair_seats, _f = _walk(repairing, True)
        _row, read_seats, _f = _walk(readonly, False,
                                     tails={planted: _TAILS["idle"]})
        diff = _disagreements(repair_seats, read_seats)
        self.assertEqual(set(diff), {planted})
        self.assertEqual(set(diff[planted]), {"state", "remediation"})
        self.assertEqual(diff[planted]["state"], ("RUNNING", "IDLE"))


def _census(**kw):
    rep = {"seats": [], "live_probe": True, "agent_probe": True,
           "covered": [], "deaf": [], "deaf_in_effect": [], "vacant": [],
           "unproven": [],
           "unreachable": [], "ghosts": [], "beacons": 0, "surplus": 0}
    rep.update(kw)
    return rep


class BeaconsSignalTest(unittest.TestCase):
    def test_clean_census_is_GREEN(self):
        rep = _census(seats=[{"seat": "a"}, {"seat": "b"}],
                      covered=[{"seat": "a"}, {"seat": "b"}])
        row = ready.signal_beacons(census=lambda: rep)
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("2 seats on the roll", row["evidence"])

    def test_beacon_live_UNPROVEN_stays_green_and_rides_the_note(self):
        rep = _census(seats=[{"seat": "a"}], unproven=[{"seat": "a"}])
        row = ready.signal_beacons(census=lambda: rep)
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("UNPROVEN", row["note"])

    def test_an_unreachable_seat_is_RED_and_named(self):
        rep = _census(seats=[{"seat": "alpha"}],
                      unreachable=[{"seat": "alpha"}])
        row = ready.signal_beacons(census=lambda: rep)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("alpha", row["evidence"])
        self.assertIn("helm beacons", row["repair"])

    def test_the_generic_repair_never_promises_a_turn_will_re_arm(self):
        """The refuted sentence, pinned so it cannot come back. A census of
        twelve DEAF seats found four that stamped a presence beat AFTER their
        deaf spell began — only a turn stamps that beat, so those seats took
        turns and armed nothing. A line telling a reader to wait for the next
        turn therefore names an event that recurs without curing anything.

        The arm is a pair, because "does not contain a phrase" passes just as
        well on a line that says nothing at all: the same reader must be shown
        emitting the true claim it was replaced with.
        """
        rep = _census(seats=[{"seat": "alpha"}],
                      unreachable=[{"seat": "alpha"}])
        repair = ready.signal_beacons(census=lambda: rep)["repair"]
        self.assertNotIn("next turn", repair)
        self.assertIn("no turn arms a beacon", repair)
        # AND IT MUST NOT PRESCRIBE WHAT IT CANNOT PERFORM. The pane route is
        # named as a route and left with whoever owns the pane; the only verb
        # the gauge hands a reader is the one for a seat with no pane.
        self.assertIn("helm seat resume <seat>", repair)
        self.assertIn("owner's call", repair)

    def test_a_DEAF_seat_with_an_agent_home_is_named_apart(self):
        """Measured on the live fleet: every DEAF seat on the roll had a live
        process declaring it. The generic advice offers a wait-for-the-next-turn
        route and a relaunch route, and an occupied deaf pane is outside both,
        so the gauge has to say which case the reader is in."""
        from helm import beacons
        rep = _census(seats=[{"seat": "alpha"}],
                      deaf=[{"seat": "alpha"}],
                      unreachable=[{"seat": "alpha",
                                    "verdict": beacons.DEAF, "agent": True}])
        row = ready.signal_beacons(census=lambda: rep)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("alpha", row["repair"])
        self.assertIn("agent HOME", row["repair"])
        self.assertIn("owner's call", row["repair"])
        # AND IT MUST NOT REPEAT THE CLAIM THE MEASUREMENT REFUTED. Five of
        # twelve DEAF seats had spoken AFTER their deaf spell began, so "wait
        # for its next turn" describes something that already happened without
        # helping; the clause says turns HAPPEN and do not arm.
        self.assertIn("TAKE TURNS", row["repair"])

    def test_a_DEAF_seat_the_census_could_not_place_is_not_named(self):
        """THE NEGATIVE CONTROL, and it is the whole reason only TRUE splits.
        `agent` None is the census saying it could not tell whether anybody is
        home; sending a reader to the pane on that would have them relaunch a
        seat on ignorance. Paired with the arm above so the clause is proven to
        depend on the value and not merely to be printable."""
        from helm import beacons

        def repair_for(agent):
            rep = _census(seats=[{"seat": "alpha"}],
                          deaf=[{"seat": "alpha"}],
                          unreachable=[{"seat": "alpha",
                                        "verdict": beacons.DEAF,
                                        "agent": agent}])
            row = ready.signal_beacons(census=lambda: rep)
            self.assertEqual(row["state"], ready.RED)
            self.assertIn("alpha", row["evidence"])
            return row["repair"]

        # THE POSITIVE CONTROL, UNCONDITIONAL AND ON THE SAME OBSERVABLE. An
        # arm asserting an ABSENCE proves nothing until the same reader has
        # been shown producing that very string, because a clause that is never
        # emitted at all and a clause correctly withheld look identical here.
        self.assertIn("agent HOME", repair_for(True))
        self.assertNotIn("agent HOME", repair_for(None))
        self.assertNotIn("agent HOME", repair_for(False))

    def test_a_DEAF_IN_EFFECT_seat_keeps_its_own_clause_and_takes_no_other(self):
        """The two splits must not overlap. DEAF-IN-EFFECT has a LIVE beacon
        and an occupied pane, so the occupied-DEAF clause — whose whole claim
        is that there is no wake path — would be false of it."""
        from helm import beacons
        rep = _census(seats=[{"seat": "kappa"}],
                      deaf_in_effect=[{"seat": "kappa"}],
                      unreachable=[{"seat": "kappa",
                                    "verdict": beacons.DEAF_IN_EFFECT,
                                    "agent": True}])
        row = ready.signal_beacons(census=lambda: rep)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("DEAF-IN-EFFECT", row["repair"])
        self.assertNotIn("agent HOME", row["repair"])

    def test_vacant_and_ghosts_are_faults_too(self):
        rep = _census(seats=[{"seat": "a"}], vacant=[{"seat": "a"}],
                      ghosts=[{"pid": 1, "seat": "a"}])
        row = ready.signal_beacons(census=lambda: rep)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("vacant", row["evidence"])
        self.assertIn("1 ghost waiter", row["evidence"])

    def test_a_failed_session_probe_is_UNKNOWN_not_a_death_claim(self):
        row = ready.signal_beacons(census=lambda: _census(live_probe=False))
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("session liveness", row["evidence"])

    def test_a_failed_process_table_probe_is_UNKNOWN(self):
        row = ready.signal_beacons(census=lambda: _census(agent_probe=False))
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("process table", row["evidence"])

    def test_a_crashing_census_is_UNKNOWN(self):
        def boom():
            raise OSError("proc gone")
        row = ready.signal_beacons(census=boom)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("proc gone", row["evidence"])


class FamiliesSignalTest(unittest.TestCase):
    def test_recorder_refusal_is_UNKNOWN_carrying_the_recorders_reason(self):
        err = "proxywatch state is 61m old, bar 40m — the watcher is not running"
        row = ready.signal_families(snapshot=lambda: (None, err))
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertEqual(row["evidence"], err)
        self.assertIn("rerun `helm proxywatch`", row["repair"])
        self.assertIn("cannot derive a restart action", row["repair"])
        self.assertNotIn("records a fresh verdict", row["repair"])

    def test_all_healthy_is_GREEN(self):
        snap = {"codex": {"state": "HEALTHY", "since": "x", "dark": False}}
        row = ready.signal_families(snapshot=lambda: (snap, None),
                                    minted=lambda: [("codex", "codex")])
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("codex", row["evidence"])

    def test_a_named_dark_cause_is_a_WALL_ready_with_note_never_red(self):
        snap = {"codex": {"state": "HEALTHY", "since": "x", "dark": False},
                "kimi": {"state": "AUTH-UNAVAILABLE",
                         "since": "2026-08-04T18:40:31Z", "dark": True}}
        row = ready.signal_families(
            snapshot=lambda: (snap, None),
            minted=lambda: [("codex", "codex"), ("kimi", "kimi")])
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("kimi AUTH-UNAVAILABLE since 2026-08-04T18:40:31Z",
                      row["note"])
        self.assertIn("a wall is a fact", row["note"])
        self.assertIn("kimi: remediation UNKNOWN", row["note"])
        self.assertNotIn("PRESCRIBES: restart this exact proxy", row["note"])

    def test_proxy_cooldown_derives_restart_helpful_in_the_same_signal(self):
        snap = {"codex": {"state": proxywatch._PROXY_COOLDOWN, "since": "x",
                          "dark": True, "falsification_bar_s": 1800,
                          "seats": {"codex": {
                              "state": proxywatch._PROXY_COOLDOWN, "dark": True,
                              "falsification_due": True,
                              "falsification_age_s": 3600,
                              "falsification_seat": "codex"}}}}
        row = ready.signal_families(snapshot=lambda: (snap, None),
                                    minted=lambda: [("codex", "codex")])
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("PRESCRIBES: restart this exact proxy", row["note"])
        self.assertIn("rerun helm proxywatch", row["note"])
        self.assertNotIn("remediation UNKNOWN", row["note"])

    def test_every_family_walled_is_still_ready_with_note(self):
        snap = {"kimi": {"state": "QUOTA-402", "since": "x", "dark": True}}
        row = ready.signal_families(snapshot=lambda: (snap, None),
                                    minted=lambda: [("kimi", "kimi")])
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("walled", row["evidence"])

    def test_all_dark_family_mixed_renders_the_split_without_actuating(self):  # noqa: VACUOUS_ASSERTION — GREEN plus both rendered seat states are positive controls before the intentional non-actuation assertion
        snap = {"codex": {"state": "FAMILY-MIXED", "since": "x",
                          "dark": True, "seats": {
                              "codex": {"state": "AUTH-401", "dark": True},
                              "seat-b": {"state": "QUOTA-402", "dark": True}}}}
        row = ready.signal_families(snapshot=lambda: (snap, None),
                                    minted=lambda: [("codex", "codex")])
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("codex FAMILY-MIXED", row["note"])
        self.assertIn("codex=AUTH-401", row["note"])
        self.assertIn("seat-b=QUOTA-402", row["note"])
        self.assertFalse(proxywatch.beacon_paused(snap["codex"]))

    def test_healthy_and_cooled_split_stays_nonactuating_but_visible(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN plus both rendered seat states are positive controls before the intentional non-actuation assertion
        snap = {"codex": {"state": "UNKNOWN", "since": "x", "dark": False,
                          "seats": {
                              "codex": {"state": proxywatch._PROXY_COOLDOWN,
                                        "dark": True},
                              "seat-b": {"state": "HEALTHY", "dark": False}}}}
        row = ready.signal_families(snapshot=lambda: (snap, None),
                                    minted=lambda: [("codex", "codex")])
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("codex=PROXY-COOLDOWN", row["evidence"])
        self.assertIn("seat-b=HEALTHY", row["evidence"])
        self.assertFalse(proxywatch.beacon_paused(snap["codex"]))

    def test_malformed_per_seat_state_is_unknown_not_a_renderer_crash(self):  # noqa: VACUOUS_ASSERTION — each named malformed fixture must render an explicit proxywatch error, and the loop cardinality is fixed at two
        malformed = ({"codex": {"state": "FAMILY-MIXED", "dark": True,
                                  "seats": {"codex": "not-an-object"}}},
                     {"codex": {"state": "FAMILY-MIXED", "dark": True,
                                  "seats": {1: {"state": "AUTH-401"}}}})
        for snap in malformed:
            with self.subTest(snap=snap):
                row = ready.signal_families(
                    snapshot=lambda: (snap, None),
                    minted=lambda: [("codex", "codex")])
                self.assertEqual(row["state"], ready.UNKNOWN)
                self.assertIn("proxywatch", row["evidence"])

    def test_an_UNKNOWN_family_state_blocks_READY(self):
        snap = {"grok": {"state": "UNKNOWN", "since": None, "dark": False}}
        row = ready.signal_families(snapshot=lambda: (snap, None),
                                    minted=lambda: [("grok", "grok")])
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("grok", row["evidence"])
        self.assertIn("remediation UNKNOWN", row["repair"])
        self.assertNotIn("re-measures", row["repair"])

    def test_a_minted_family_missing_from_the_record_is_UNKNOWN(self):
        """Coverage is checked against the minted census — a hardcoded list
        is how grok starved for two days (proxywatch's own docstring)."""
        snap = {"codex": {"state": "HEALTHY", "since": "x", "dark": False}}
        row = ready.signal_families(
            snapshot=lambda: (snap, None),
            minted=lambda: [("codex", "codex"), ("grok", "grok")])
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("grok: minted but no recorded verdict", row["evidence"])

    def test_a_failed_minted_census_is_UNKNOWN_not_a_shorter_answer(self):
        def boom():
            raise OSError("seats root unreadable")
        snap = {"codex": {"state": "HEALTHY", "since": "x", "dark": False}}
        row = ready.signal_families(snapshot=lambda: (snap, None), minted=boom)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("coverage unverifiable", row["evidence"])


class NoFreshSweepTest(unittest.TestCase):
    """THE PIN: signal 4 reads the RECORDED sweep; it never runs one. Every
    entry into proxywatch's canary machinery is booby-trapped, so the
    mutation 'make signal 4 sweep' crashes into an AssertionError, the
    signal stops reading GREEN, and this test goes red — while the honest
    reader path stays green having spent zero upstream tokens."""

    def setUp(self):
        self.traps = [mock.patch.object(
            proxywatch, name, side_effect=AssertionError(
                "signal 4 must READ the recorded sweep, never run one"))
            for name in ("_upstream_once", "upstream_canary",
                         "upstream_health", "health", "cmd_proxywatch")]
        for t in self.traps:
            t.start()
        self.addCleanup(lambda: [t.stop() for t in self.traps])

    def test_a_fresh_record_reads_GREEN_through_the_reader_path_alone(self):
        state = {"ts": time.time(), "upstream": {
            "codex": {"state": "HEALTHY", "since": "x", "dark": False}}}
        with mock.patch.object(proxywatch, "_read_watch_state",
                               return_value=(state, None)):
            row = ready.signal_families(minted=lambda: [("codex", "codex")])
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("codex", row["evidence"])

    def test_a_stale_record_reads_UNKNOWN_with_the_recorders_reason(self):
        state = {"ts": time.time() - proxywatch.UPSTREAM_CACHE_FRESH_S - 61,
                 "upstream": {
                     "codex": {"state": "HEALTHY", "since": "x", "dark": False}}}
        with mock.patch.object(proxywatch, "_read_watch_state",
                               return_value=(state, None)):
            row = ready.signal_families(minted=lambda: [("codex", "codex")])
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("watcher is not running", row["evidence"])


class CheckoutSignalTest(unittest.TestCase):
    def setUp(self):
        if not shutil.which("git"):
            raise unittest.SkipTest("git not available")
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ready-git-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        with open(os.path.join(self.repo, "f"), "w") as f:
            f.write("1\n")
        self._git("add", "f")
        self._git("commit", "-qm", "one")
        self._git("update-ref", "refs/remotes/origin/main", "HEAD")

    def _git(self, *argv):
        subprocess.run(["git", "-C", self.repo] + list(argv), check=True,
                       capture_output=True, text=True)

    def test_clean_at_origin_main_is_GREEN_and_says_as_last_fetched(self):
        row = ready.signal_checkout(root=self.repo)
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("as last fetched", row["evidence"])

    def test_a_dirty_shared_checkout_is_RED_with_the_lane_room_repair(self):
        with open(os.path.join(self.repo, "f"), "w") as f:
            f.write("2\n")
        row = ready.signal_checkout(root=self.repo)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("DIRTY", row["evidence"])
        self.assertIn("helm work claim", row["repair"])

    def test_a_head_off_origin_main_is_RED_naming_both_shas(self):
        with open(os.path.join(self.repo, "f"), "w") as f:
            f.write("2\n")
        self._git("commit", "-aqm", "two")
        row = ready.signal_checkout(root=self.repo)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("origin/main is", row["evidence"])

    def test_a_non_repo_root_is_UNKNOWN(self):
        row = ready.signal_checkout(root=self.tmp)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("git could not answer", row["evidence"])
        self.assertIn(self.tmp, row["evidence"])

    def test_no_containing_checkout_is_UNKNOWN(self):
        from helm.work import _lanes
        with mock.patch.object(_lanes, "find_root", return_value=None):
            row = ready.signal_checkout()
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("unmeasurable", row["evidence"])


def _stub(signal, state, note=None):
    return lambda: ready._row(signal, state, signal + " evidence", note=note)


class CompositionTest(unittest.TestCase):
    def _gauge(self, **over):
        stubs = {"signal_daemon": _stub("daemon", ready.GREEN),
                 "signal_seats": _stub("seats", ready.GREEN),
                 "signal_beacons": _stub("beacons", ready.GREEN),
                 "signal_families": _stub("families", ready.GREEN),
                 "signal_checkout": _stub("checkout", ready.GREEN)}
        stubs.update(over)
        with contextlib.ExitStack() as st:
            for name, fn in stubs.items():
                st.enter_context(mock.patch.object(ready, name, fn))
            return ready.gauge()

    def test_all_green_is_READY(self):
        rep = self._gauge()
        self.assertEqual(rep["ready"], ready.READY)
        self.assertEqual(len(rep["signals"]), 5)

    def test_one_red_is_NOT_READY_even_beside_an_unknown(self):
        rep = self._gauge(signal_seats=_stub("seats", ready.RED),
                          signal_families=_stub("families", ready.UNKNOWN))
        self.assertEqual(len(rep["signals"]), 5)
        self.assertEqual(rep["ready"], "NOT READY")
        self.assertEqual(rep["signals"][1]["state"], ready.RED)
        self.assertEqual(rep["signals"][3]["state"], ready.UNKNOWN)

    def test_no_red_one_unknown_is_UNKNOWN_never_READY(self):
        rep = self._gauge(signal_families=_stub("families", ready.UNKNOWN))
        self.assertEqual(rep["ready"], ready.UNKNOWN)
        self.assertEqual([r["state"] for r in rep["signals"]],
                         [ready.GREEN, ready.GREEN, ready.GREEN,
                          ready.UNKNOWN, ready.GREEN])

    def test_a_walled_family_keeps_READY_and_the_note_survives(self):
        rep = self._gauge(signal_families=_stub(
            "families", ready.GREEN, note="WALLED: kimi AUTH-UNAVAILABLE"))
        self.assertEqual(rep["ready"], ready.READY)
        self.assertIn("WALLED", rep["signals"][3]["note"])

    def test_a_crashing_signal_becomes_an_UNKNOWN_row_never_a_missing_one(self):
        def boom():
            raise RuntimeError("leg died")
        rep = self._gauge(signal_beacons=boom)
        self.assertEqual(len(rep["signals"]), 5)
        self.assertEqual(rep["signals"][2]["state"], ready.UNKNOWN)
        self.assertIn("leg died", rep["signals"][2]["evidence"])
        self.assertEqual(rep["ready"], ready.UNKNOWN)


def _rep(verdict, rows):
    return {"ready": verdict, "signals": rows, "ts": 0}


class CmdReadyTest(unittest.TestCase):
    """The exit-code contract IS the covering surface: 0 READY, 1 NOT READY,
    2 cannot-prove — distinct on purpose (beacons' precedent)."""

    def test_READY_exits_0_and_renders_the_advisory_line(self):
        rep = _rep(ready.READY, [ready._row("daemon", ready.GREEN, "ok")])
        with mock.patch.object(ready, "gauge", return_value=rep):
            rc, out = _out(ready.cmd_ready, [])
        self.assertEqual(rc, 0)
        self.assertIn("ADVISORY", out)
        self.assertIn("helm ready: READY — 1 green, 0 red, 0 unknown", out)

    def test_one_red_exits_1_and_prints_the_repair_verb(self):
        rep = _rep(ready.NOT_READY,
                   [ready._row("seats", ready.RED, "codex GONE",
                               repair="helm seat resume codex"),
                    ready._row("daemon", ready.GREEN, "answering",
                               repair="never shown while green")])
        with mock.patch.object(ready, "gauge", return_value=rep):
            rc, out = _out(ready.cmd_ready, [])
        self.assertEqual(rc, 1)
        self.assertIn("-> helm seat resume codex", out)
        self.assertNotIn("never shown while green", out)

    def test_unknown_exits_2(self):
        rep = _rep(ready.UNKNOWN, [ready._row("families", ready.UNKNOWN, "stale")])
        with mock.patch.object(ready, "gauge", return_value=rep):
            rc, _o = _out(ready.cmd_ready, [])
        self.assertEqual(rc, 2)

    def test_json_round_trips(self):
        rep = _rep(ready.READY, [ready._row("daemon", ready.GREEN, "ok")])
        with mock.patch.object(ready, "gauge", return_value=rep):
            rc, out = _out(ready.cmd_ready, ["--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["ready"], "READY")

    def test_junk_args_refuse_before_any_gauge_runs(self):
        with mock.patch.object(ready, "gauge",
                               side_effect=AssertionError("gauged anyway")):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = ready.cmd_ready(["--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg", buf.getvalue())


class WebApiTest(unittest.TestCase):
    def setUp(self):
        self._forget()
        self.addCleanup(self._forget)

    def _forget(self):
        web._qstate.pop("ready", None)
        web._qinflight.pop("ready", None)

    def test_the_route_is_registered_and_serves_the_gauge(self):
        self.assertIn("/api/ready", web.API)
        rep = _rep(ready.READY, [ready._row("daemon", ready.GREEN, "ok")])
        with mock.patch.object(ready, "gauge", return_value=rep):
            body = web.API["/api/ready"]()
        self.assertEqual(body["ready"], "READY")

    def test_the_cache_answers_the_second_poll_without_regauging(self):
        rep = _rep(ready.READY, [])
        with mock.patch.object(ready, "gauge", return_value=rep) as g:
            web.API["/api/ready"]()
            web.API["/api/ready"]()
        self.assertEqual(g.call_count, 1)

    def test_the_ttl_is_floored_by_what_the_fill_actually_cost(self):
        """A CACHE WHOSE TTL IS BELOW ITS OWN FILL NEVER PAYS FOR ITSELF —
        task/2887's third defect, and the one that outlives the cure. The
        effective ttl is floored at twice the LAST MEASURED fill, so the
        relation cannot regress into the trap again.

        The control is the same observable in the same arrangement: under the
        identical tiny ttl, a FAST fill really does expire and rebuild. Without
        it "the second poll did not rebuild" could be true of a cache that
        never rebuilds at all.
        """
        from helm import web_core
        self.addCleanup(setattr, web_core, "_READY_FILL_S", 0.0)
        rep = _rep(ready.READY, [])
        gap, fill = 0.05, 0.20

        with mock.patch.object(web_core, "_READY_TTL_S", 0.01):
            # POSITIVE CONTROL that the arrangement is the one described: the
            # ttl this route reads really is the tiny one. Patched onto the
            # wrong module it would still be 30, and every "did not rebuild"
            # below would be true for a reason that has nothing to do with the
            # floor.
            self.assertEqual(web_core._READY_TTL_S, 0.01)
            # CONTROL — a fill of ~0 leaves the floor below the ttl, so the ttl
            # governs and the entry expires inside the gap.
            web_core._READY_FILL_S = 0.0
            self._forget()
            with mock.patch.object(ready, "gauge", return_value=rep) as fast:
                web.API["/api/ready"]()
                time.sleep(gap)
                web.API["/api/ready"]()
            self.assertEqual(fast.call_count, 2)

            # THE CLAIM — an expensive fill raises the floor above itself, so
            # the same gap is served warm from the entry it paid for.
            web_core._READY_FILL_S = 0.0
            self._forget()

            def slow():
                time.sleep(fill)
                return rep
            with mock.patch.object(ready, "gauge", side_effect=slow) as g:
                web.API["/api/ready"]()
                time.sleep(gap)
                web.API["/api/ready"]()
            self.assertEqual(g.call_count, 1)
        self.assertGreaterEqual(web_core._READY_FILL_S, fill)

    def test_a_crashing_gauge_answers_unavailable_at_200_shape(self):
        with mock.patch.object(ready, "gauge",
                               side_effect=OSError("estate on fire")):
            body = web.API["/api/ready"]()
        self.assertIn("estate on fire", body["unavailable"])


class UiWiringTest(unittest.TestCase):
    """The page half: the section exists, the renderer is a function
    DECLARATION (the node harness lifts declarations only), the wiring line
    binds it to the section, and the card boots pending beside the other
    boots (the TDZ law: boot after every declaration). Each test reads the
    page itself, so the observable's provenance is straight-line."""

    def _ui(self):
        return web_ui_loader.read_text()

    def test_the_section_renderer_wiring_and_boot_all_exist(self):
        ui = self._ui()
        # positive control first: the page actually loaded and is the page
        self.assertGreater(len(ui), 10000)
        self.assertIn("<!doctype html>", ui)
        self.assertIn('<section id="readysec"></section>', ui)
        self.assertIn("function readyCardHTML(", ui)
        self.assertIn('$("#readysec").innerHTML = readyCardHTML(d)', ui)
        self.assertIn('readyShow({pending: true});', ui)
        self.assertIn('j("/api/ready"', ui)

    def test_the_boot_sits_after_the_declarations(self):
        ui = self._ui()
        # both anchors must EXIST (the positive control) before order means
        # anything — index() would raise, but a raise is not an assertion
        self.assertIn("function readyCardHTML(", ui)
        self.assertIn("readyShow({pending: true});", ui)
        self.assertLess(ui.index("function readyCardHTML("),
                        ui.index("readyShow({pending: true});"))


class ClientRuntimeTest(unittest.TestCase):
    """readyCardHTML under node — the exact source the page ships."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        from tests.test_web_chat_client_runtime import _extract_fn
        src = web_ui_loader.read_text()
        line = [ln for ln in src.splitlines() if ln.startswith("const esc = ")]
        assert len(line) == 1, "assembled web UI's esc definition moved"
        cls.tmp = tempfile.mkdtemp(prefix="helm-ready-runtime-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(line[0] + "\n\n" + _extract_fn(src, "readyCardHTML") + """

const cases = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const out = {};
for (const name of Object.keys(cases)) out[name] = readyCardHTML(cases[name]);
process.stdout.write(JSON.stringify(out));
""")
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def render(self, **cases):
        path = os.path.join(self.tmp, "cases.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cases, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_the_three_boards_render_their_states(self):
        board = {"ready": "NOT READY", "ts": 0, "signals": [
            {"signal": "seats", "state": "RED",
             "evidence": "codex GONE (pid-dead)",
             "repair": "helm seat resume codex", "note": None},
            {"signal": "families", "state": "GREEN",
             "evidence": "codex HEALTHY",
             "repair": "hidden while green",
             "note": "WALLED: kimi AUTH-UNAVAILABLE"}]}
        out = self.render(bad=board, pending={"pending": True},
                          dark={"unavailable": "/api/ready did not answer"})
        self.assertIn("NOT READY", out["bad"])
        self.assertIn("helm seat resume codex", out["bad"])
        self.assertNotIn("hidden while green", out["bad"])
        self.assertIn("WALLED: kimi AUTH-UNAVAILABLE", out["bad"])
        self.assertIn("not read yet", out["pending"])
        self.assertIn("UNKNOWN", out["dark"])

    def test_evidence_is_escaped_not_injected(self):
        board = {"ready": "UNKNOWN", "ts": 0, "signals": [
            {"signal": "seats", "state": "UNKNOWN",
             "evidence": "<script>alert(1)</script>", "repair": None,
             "note": None}]}
        html = self.render(x=board)["x"]
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
