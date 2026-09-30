"""ONE COLD FOLD PER PROCESS: concurrent readers of one ledger wait for it.

task/3082. Measured with py-spy on the resident helm web during a 169 s
post-land refold: about 3.7 cold whole-ledger folds ran AT ONCE. The callers
were different endpoints (the land board, the all-projects board, the owed
page, the gate-epoch recheck under both, the stop-facts resident), each
single-flighted only on its own web cache key, so the fold under them was
recomputed per endpoint. Measured on an isolated copy of the live home, one
cold fold alone is 95-140 s and about 1,460 git processes, and four at once
are 105-125 s each: the herd multiplies git work, and one fold instead of
four cut the cold burst from about 8,900 git spawns to about 6,000.

WHAT THESE ARMS PIN. Every arm reads the fold's ROADS off a spy on
`dispatches._fold_into`: a cold fold replays from position 0, and a reader
served by a checkpoint folds only its tail from position K. So "folded once"
is a count of cold roads, not a timing, and every arm that claims sharing also
states that the sharing HAPPENED (one cold road), so an isolation property is
never proved over a world where nothing was shared.

HOW THE HERD IS STAGED. The spy holds the first cold fold at its door until
the test has seen every other reader either waiting on the shared fold or
inside a cold fold of its own. That makes the race deterministic in both
directions: on a tree with no single-flight every reader takes its own cold
road, and on a tree with one exactly one does.

Every world is a temp HELM_HOME (DispatchBase); no arm reads the live fleet.
"""
import os
import shutil
import threading
import time
import traceback
from unittest import mock

from helm import dispatches, foldckpt, projscope
from tests.test_dispatches import DispatchBase

# A BOUND ON EVERY WAIT IN THIS FILE, so a regression that hangs a reader
# fails the arm instead of hanging the suite.
_PATIENCE_S = 20.0


def _waiting(ledger=None):
    """How many readers are waiting on a shared fold right now (of `ledger`
    when given). Zero on a tree that has no shared fold at all."""
    flights = getattr(dispatches, "_FOLD_FLIGHTS", {})
    return sum(f.waiting for key, f in list(flights.items())
               if ledger is None or key[0] == os.path.abspath(ledger))


class _Roads(object):
    """A spy on `_fold_into`: every road taken, and a door that holds cold
    ones until the test opens it."""

    def __init__(self, hold=lambda: True, fail=None, taint=None):
        self.real = dispatches._fold_into
        self.mu = threading.Lock()
        self.roads = []           # (ledger, base, n events), in call order
        self.who = []             # (thread name, lensed?, base, n events)
        self.cold_in = {}         # ledger -> cold roads that have entered
        self.go = threading.Event()
        # -> should THIS cold road wait at the door? True holds it on `go`;
        # an Event holds it on that door instead, so an arm can stage two
        # folds and open them one at a time.
        self.hold = hold
        self.fail = fail          # an exception every cold road raises
        # A reason every cold road taints its fold with, as a compose-landed
        # v3 close does: `foldckpt.plan` then refuses that fold's save.
        self.taint = taint

    def __call__(self, acc, events, base, bank=None):
        ledger = os.path.abspath(dispatches.ledger_path())
        cold = base == 0 and len(events) > 0
        lensed = getattr(dispatches._EPOCH_LENS, "fn", None) is not None
        with self.mu:
            self.roads.append((ledger, base, len(events)))
            self.who.append((threading.current_thread().name, lensed, base,
                             len(events)))
            if cold:
                self.cold_in[ledger] = self.cold_in.get(ledger, 0) + 1
        door = self.hold() if cold else False
        if door:
            door = door if isinstance(door, threading.Event) else self.go
            if not door.wait(_PATIENCE_S):
                raise AssertionError("the test never opened the door")
        if cold and self.fail is not None:
            raise self.fail
        if cold and self.taint is not None:
            foldckpt.taint(self.taint)
        return self.real(acc, events, base, bank=bank)

    def cold(self, ledger=None):
        return [r for r in self.roads if r[1] == 0 and r[2] > 0
                and (ledger is None or r[0] == os.path.abspath(ledger))]

    def entered(self, ledger=None):
        with self.mu:
            if ledger is None:
                return sum(self.cold_in.values())
            return self.cold_in.get(os.path.abspath(ledger), 0)


def _run(target, name, results):
    def body():
        try:
            results[name] = ("ok", target())
        except BaseException as exc:          # noqa: BLE001 — the arm reads it
            results[name] = ("raised", exc)
    t = threading.Thread(target=body, name=name, daemon=True)
    t.start()
    return t


class FoldSingleFlightBase(DispatchBase):

    def setUp(self):
        super().setUp()
        self.assertTrue(dispatches.ledger_path().startswith(self.tmp),
                        "the ledger is not under this test's HELM_HOME")
        for n in range(3):
            self.add(recipient="seat-%d" % n)
        self.count = len(dispatches.eventledger.events(
            dispatches.ledger_path()))
        self.assertGreater(self.count, 0)
        self.want = self.reference()
        self.cold_start()

    def cold_start(self, ledger=None):
        """No checkpoint for this code: the next read is a whole replay, as
        every read is after a land."""
        store = foldckpt.store_dir(ledger or dispatches.ledger_path())
        shutil.rmtree(store, ignore_errors=True)
        self.assertFalse(os.path.isdir(store))

    def reference(self):
        """Today's answer for the ledger as it stands: a full replay."""
        events = dispatches.eventledger.events(dispatches.ledger_path())
        out, verdicts, _taken = dispatches._fold(events)
        return repr(out), repr(verdicts)

    def settled(self, roads, predicate, what):
        deadline = time.monotonic() + _PATIENCE_S
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        roads.go.set()
        self.fail("the herd never formed: %s (cold roads entered %d, "
                  "waiting %d)" % (what, roads.entered(), _waiting()))

    def join(self, threads):
        for t in threads:
            t.join(_PATIENCE_S)
        alive = [t.name for t in threads if t.is_alive()]
        self.assertEqual(alive, [], "a reader hung")

    def herd(self, readers, roads):
        """Start every reader, wait until one holds the cold fold and every
        other is waiting on it or folding cold itself, then open the door."""
        results, threads = {}, []
        with mock.patch.object(dispatches, "_fold_into", side_effect=roads):
            for name, target in readers:
                threads.append(_run(target, name, results))
            n = len(readers)
            self.settled(roads, lambda: roads.entered() >= 1
                         and roads.entered() + _waiting() == n,
                         "%d readers" % n)
            roads.go.set()
            self.join(threads)
        return results


class ConcurrentReadersFoldOnce(FoldSingleFlightBase):

    def test_n_concurrent_same_key_readers_fold_once_and_all_agree(self):  # noqa: VACUOUS_ASSERTION — every reader's rows equal a full replay and the roads are counted exactly; the absences are unavailable=None
        roads = _Roads()
        results = self.herd([("r%d" % i, dispatches.snapshot_with_verdicts)
                             for i in range(4)], roads)
        self.assertEqual(sorted(results), ["r0", "r1", "r2", "r3"])
        for name, (how, value) in sorted(results.items()):
            self.assertEqual(how, "ok", "%s raised %r" % (name, value))
            rows, verdicts, unavailable = value
            self.assertIsNone(unavailable)
            # EQUAL TO A FULL REPLAY, not merely to each other: four readers
            # agreeing on a wrong answer is not the property.
            self.assertEqual((repr(rows), repr(verdicts)), self.want)
        self.assertEqual(len(roads.cold()), 1,
                         "every reader replayed the whole ledger itself: "
                         "%r" % roads.roads)
        # THE OTHER THREE WERE SERVED BY THE FOLD'S CHECKPOINT: a restore at
        # the whole ledger with no tail.
        self.assertEqual(sorted(r[1:] for r in roads.roads),
                         [(0, self.count)] + [(self.count, 0)] * 3)

    def test_readers_with_different_flags_share_the_one_cold_fold(self):  # noqa: VACUOUS_ASSERTION — three readers' rows equal a full replay and the cold roads are counted; the absence is the activity reader's unavailable=None
        """The herd the profile measured was lenient `snapshot`, strict
        `snapshot_and_events` and the verdict-indexed read at once. What they
        fold is one fold; only what they parse and return differs."""
        roads = _Roads()
        readers = [("lenient", dispatches.snapshot),
                   ("strict", dispatches.snapshot_and_events),
                   ("verdicts", dispatches.snapshot_with_verdicts),
                   ("actors", dispatches.seat_activity)]
        results = self.herd(readers, roads)
        for name, (how, value) in sorted(results.items()):
            self.assertEqual(how, "ok", "%s raised %r" % (name, value))
        self.assertEqual(repr(results["lenient"][1][0]), self.want[0])
        self.assertEqual(repr(results["strict"][1][0]), self.want[0])
        self.assertEqual(repr(results["verdicts"][1][0]), self.want[0])
        self.assertIsNone(results["actors"][1][3])
        self.assertEqual(len(roads.cold()), 1, roads.roads)


class DifferentKeysFoldSeparately(FoldSingleFlightBase):

    def test_two_ledgers_fold_once_each_and_never_wait_on_each_other(self):  # noqa: VACUOUS_ASSERTION — every reader's rows equal a full replay and the cold roads per ledger are counted exactly
        """The key names the ledger. Readers of ledger B must not queue behind
        a fold of ledger A, and two readers of one ledger must share."""
        a = dispatches.ledger_path()
        b_home = os.path.join(self.tmp, "second-home")
        os.makedirs(b_home)
        b = os.path.join(b_home, os.path.basename(a))
        shutil.copyfile(a, b)
        self.cold_start(b)
        which = threading.local()
        real_path = dispatches.ledger_path

        def ledger_path():
            return getattr(which, "path", None) or real_path()

        def reader(path):
            def read():
                which.path = path
                return dispatches.snapshot()
            return read

        # LEDGER A's cold fold is held on the main door until B is done, and
        # B's on a door of its own until B's other reader is waiting on it:
        # left to timing, a reader that reached the registry after that fold
        # ended would lead a second cold fold of B, and this arm would read
        # a race it lost as the property failing.
        b_go = threading.Event()
        self.addCleanup(b_go.set)
        roads = _Roads(hold=lambda: b_go if getattr(which, "path", None) == b
                       else True)
        results, threads = {}, []
        with mock.patch.object(dispatches, "ledger_path", ledger_path), \
                mock.patch.object(dispatches, "_fold_into",
                                  side_effect=roads):
            for name, path in (("a1", a), ("a2", a)):
                threads.append(_run(reader(path), name, results))
            self.settled(roads, lambda: roads.entered(a) >= 1
                         and roads.entered(a) + _waiting(a) == 2,
                         "both readers of ledger A")
            b_threads = [_run(reader(b), name, results)
                         for name in ("b1", "b2")]
            self.settled(roads, lambda: roads.entered(b) == 1
                         and _waiting(b) == 1, "both readers of ledger B")
            b_go.set()
            # B FINISHES WHILE A's FOLD IS STILL HELD AT THE DOOR.
            self.join(b_threads)
            self.assertFalse(roads.go.is_set())
            self.assertTrue(all(t.is_alive() for t in threads),
                            "ledger A's readers finished before their fold "
                            "was released, so this arm staged nothing")
            roads.go.set()
            self.join(threads)
        for name, (how, value) in sorted(results.items()):
            self.assertEqual(how, "ok", "%s raised %r" % (name, value))
            self.assertIsNone(value[1])
            self.assertEqual(repr(value[0]), self.want[0])
        self.assertEqual((len(roads.cold(a)), len(roads.cold(b))), (1, 1),
                         roads.roads)


class ASharedFoldsFailureReachesEveryWaiter(FoldSingleFlightBase):

    def test_an_exception_in_the_shared_fold_reaches_every_waiter(self):  # noqa: VACUOUS_ASSERTION — every reader's exception is asserted by type and text, the cold roads are counted, and the next read equals a full replay
        boom = RuntimeError("the shared fold broke")
        roads = _Roads(fail=boom)
        results = self.herd([("r%d" % i, dispatches.snapshot)
                             for i in range(4)], roads)
        for name, (how, value) in sorted(results.items()):
            self.assertEqual(how, "raised", "%s answered %r instead of the "
                                            "fold's failure" % (name, value))
            self.assertIsInstance(value, RuntimeError)
            self.assertEqual(str(value), str(boom))
        # ONE FOLD FAILED AND FOUR READERS HEARD IT: not four folds failing.
        self.assertEqual(len(roads.cold()), 1, roads.roads)
        # NOTHING STAYED REGISTERED: the next read is not parked behind a
        # fold that is gone.
        self.assertEqual(getattr(dispatches, "_FOLD_FLIGHTS", {}), {})
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertEqual(repr(rows), self.want[0])

    def test_each_waiter_raises_an_exception_object_of_its_own(self):  # noqa: VACUOUS_ASSERTION — every reader's exception is asserted by type, args and traceback, and the distinct objects and the cold roads are counted exactly
        """ONE OBJECT RAISED IN SEVERAL THREADS IS REWRITTEN BY EACH: every
        raise sets its `__traceback__` and `__context__`, so a waiter's
        handler could read another thread's frames. Every waiter raises a
        copy of its own, of the fold's type and args, whose traceback still
        reaches the frame where the fold raised."""
        boom = RuntimeError("the shared fold broke", 3082)
        roads = _Roads(fail=boom)
        results = self.herd([("r%d" % i, dispatches.snapshot)
                             for i in range(3)], roads)
        raised = []
        for name, (how, value) in sorted(results.items()):
            self.assertEqual(how, "raised", "%s answered %r instead of the "
                                            "fold's failure" % (name, value))
            self.assertIs(type(value), RuntimeError)
            self.assertEqual(value.args, boom.args)
            frames = [f.name for f in
                      traceback.extract_tb(value.__traceback__)]
            self.assertIn("__call__", frames, "%s's traceback lost the "
                                              "fold's frames" % name)
            raised.append(value)
        # SHARING HAPPENED: one fold failed and three readers heard it.
        self.assertEqual(len(roads.cold()), 1, roads.roads)
        self.assertEqual(len(set(map(id, raised))), 3,
                         "readers raised one shared exception object")


class AWaitersBudgetIsItsOwn(FoldSingleFlightBase):

    def test_an_expired_waiter_raises_and_the_shared_fold_finishes(self):
        # ONLY THE LEADER's fold is held: a reader that folds for itself must
        # be free to finish, or a tree with no shared fold would stall here
        # instead of answering.
        roads = _Roads(hold=lambda: threading.current_thread().name
                       == "leader")
        results = {}
        with mock.patch.object(dispatches, "_fold_into", side_effect=roads):
            leader = _run(dispatches.snapshot, "leader", results)
            self.settled(roads, lambda: roads.entered() == 1,
                         "the leader's cold fold")

            def budgeted():
                with projscope.scope(deadline=time.monotonic() + 0.5):
                    return dispatches.snapshot()

            waiter = _run(budgeted, "waiter", results)
            self.join([waiter])
            how, value = results["waiter"]
            self.assertEqual(how, "raised",
                             "the budgeted reader answered %r" % (value,))
            self.assertIsInstance(value, projscope.Expired)
            # THE SHARED FOLD IS STILL RUNNING: the waiter's deadline is its
            # own and cancelled nothing.
            self.assertTrue(leader.is_alive())
            self.assertNotIn("leader", results)
            roads.go.set()
            self.join([leader])
        how, value = results["leader"]
        self.assertEqual(how, "ok", "the leader raised %r" % (value,))
        self.assertIsNone(value[1])
        self.assertEqual(repr(value[0]), self.want[0])
        # THE WAITER NEVER STARTED A FOLD OF ITS OWN...
        self.assertEqual(len(roads.cold()), 1, roads.roads)
        # ...AND THE SHARED ONE WAS SAVED, so the next reader restores it.
        with mock.patch.object(dispatches, "_fold_into",
                               wraps=dispatches._fold_into) as spy:
            rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertEqual([(c.args[2], len(c.args[1]))
                          for c in spy.call_args_list], [(self.count, 0)])


class OneCallersMutationStaysItsOwn(FoldSingleFlightBase):

    def test_a_mutated_result_never_changes_another_readers_result(self):  # noqa: VACUOUS_ASSERTION — the untouched readers' rows are compared by value with a full replay after the mutation
        roads = _Roads()
        results = self.herd([("r%d" % i, dispatches.snapshot_with_verdicts)
                             for i in range(3)], roads)
        for name, (how, value) in sorted(results.items()):
            self.assertEqual(how, "ok", "%s raised %r" % (name, value))
        # SHARING HAPPENED — without it, isolation is true of any tree.
        self.assertEqual(len(roads.cold()), 1, roads.roads)
        mine = results["r1"][1]
        rid = sorted(mine[0])[0]
        mine[0][rid]["status"] = "MUTATED-BY-ONE-READER"
        mine[0][rid]["planted"] = ["a", "list"]
        mine[0].pop(sorted(mine[0])[-1])
        mine[1]["planted"] = (0, "anchor")
        for other in ("r0", "r2"):
            rows, verdicts, _un = results[other][1]
            self.assertIsNot(rows, mine[0])
            self.assertEqual((repr(rows), repr(verdicts)), self.want,
                             "%s's result changed when r1 mutated its own"
                             % other)
        rows, verdicts, _un = dispatches.snapshot_with_verdicts()
        self.assertEqual((repr(rows), repr(verdicts)), self.want)


class TheFlightNeverHangsOrSerialises(FoldSingleFlightBase):
    """GUARDS, green on a tree with no single-flight by construction: they
    pin the two ways a single-flight itself can go wrong."""

    def test_a_fold_nested_in_the_leading_fold_does_not_wait_on_itself(self):  # noqa: VACUOUS_ASSERTION — both the outer and the nested read are compared by value with a full replay
        nested, entered = [], []
        real = dispatches._fold_into

        def spy(acc, events, base, bank=None):
            if base == 0 and events and not entered:
                entered.append(True)        # before the call: it re-enters
                nested.append(dispatches.snapshot())
            return real(acc, events, base, bank=bank)

        results = {}
        with mock.patch.object(dispatches, "_fold_into", side_effect=spy):
            t = _run(dispatches.snapshot, "outer", results)
            self.join([t])
        self.assertEqual(results["outer"][0], "ok", results)
        self.assertEqual(repr(results["outer"][1][0]), self.want[0])
        self.assertEqual(repr(nested[0][0]), self.want[0])

    def test_a_leader_that_saves_nothing_leaves_one_more_flight_then_alone(self):  # noqa: VACUOUS_ASSERTION — every reader's rows equal a full replay and the cold roads are counted exactly; the empty registry is the release
        """A refused save (trunk moved under the fold, an input nothing can
        re-verify) releases the waiters to read again, and they may form ONE
        more flight, so a transient refusal costs one fold rather than a herd.
        A reader that has waited twice folds alone, so a save that is refused
        every time costs three folds for three readers and hangs nobody."""
        roads = _Roads()
        with mock.patch.object(foldckpt.Session, "save",
                               lambda *a, **k: False):
            results = self.herd([("r%d" % i, dispatches.snapshot)
                                 for i in range(3)], roads)
        for name, (how, value) in sorted(results.items()):
            self.assertEqual(how, "ok", "%s raised %r" % (name, value))
            self.assertEqual(repr(value[0]), self.want[0])
        self.assertEqual(len(roads.cold()), 3, roads.roads)
        self.assertEqual(getattr(dispatches, "_FOLD_FLIGHTS", {}), {})

    def test_a_refused_save_is_followed_by_one_more_shared_fold(self):  # noqa: VACUOUS_ASSERTION — every reader's rows equal a full replay and the cold roads are counted exactly; the empty registry is the release
        """The transient refusal: the FIRST save is refused (as when trunk
        moves under the fold) and the next one holds. The waiters form one
        more flight, so the ledger is replayed twice in all and the third
        reader restores, rather than each waiter replaying it alone."""
        real = foldckpt.Session.save
        calls = []

        def refuse_once(session, *a, **k):
            calls.append(True)
            return False if len(calls) == 1 else real(session, *a, **k)

        # THE SECOND ROUND IS STAGED LIKE THE FIRST: its cold road (any cold
        # road after the refused save) waits on a door of its own until the
        # other reader is waiting on it. Left to timing, a reader that
        # reached the registry after that fold ended would lead a THIRD cold
        # fold, and this arm would read a race it lost as the wrong count.
        second = threading.Event()
        self.addCleanup(second.set)
        roads = _Roads(hold=lambda: second if calls else True)
        results, threads = {}, []
        with mock.patch.object(foldckpt.Session, "save", refuse_once), \
                mock.patch.object(dispatches, "_fold_into",
                                  side_effect=roads):
            for i in range(3):
                threads.append(_run(dispatches.snapshot, "r%d" % i, results))
            self.settled(roads, lambda: roads.entered() >= 1
                         and roads.entered() + _waiting() == 3, "3 readers")
            roads.go.set()
            self.settled(roads, lambda: roads.entered() == 2
                         and _waiting() == 1, "the second flight")
            second.set()
            self.join(threads)
        for name, (how, value) in sorted(results.items()):
            self.assertEqual(how, "ok", "%s raised %r" % (name, value))
            self.assertEqual(repr(value[0]), self.want[0])
        self.assertEqual(len(roads.cold()), 2, roads.roads)
        self.assertEqual(getattr(dispatches, "_FOLD_FLIGHTS", {}), {})

    def test_a_ledger_that_cannot_checkpoint_releases_its_waiters_at_once(self):  # noqa: VACUOUS_ASSERTION — every reader's rows are compared by value with the lenient replay and the cold roads are counted
        """A malformed complete line makes the prefix unclean, so no fold of
        this ledger can be saved. The leader knows that before it folds and
        releases its waiters to fold alone, instead of making them wait for a
        fold that can give them nothing."""
        with open(dispatches.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write("{this line is not json}\n")
        events, reason = dispatches.eventledger.checked_events(
            dispatches.ledger_path())
        self.assertIsNone(reason)
        want = repr(dispatches._fold(events)[0])
        roads = _Roads(hold=lambda: threading.current_thread().name == "r0")
        results = {}
        with mock.patch.object(dispatches, "_fold_into", side_effect=roads):
            leader = _run(dispatches.snapshot, "r0", results)
            self.settled(roads, lambda: roads.entered() == 1,
                         "the leader's cold fold")
            others = [_run(dispatches.snapshot, "r%d" % i, results)
                      for i in (1, 2)]
            # THEY FINISH WHILE THE LEADER IS STILL HELD AT THE DOOR.
            self.join(others)
            self.assertTrue(leader.is_alive())
            roads.go.set()
            self.join([leader])
        for name, (how, value) in sorted(results.items()):
            self.assertEqual(how, "ok", "%s raised %r" % (name, value))
            self.assertEqual(repr(value[0]), want)
        self.assertEqual(len(roads.cold()), 3, roads.roads)
        self.assertEqual(getattr(dispatches, "_FOLD_FLIGHTS", {}), {})


class ARefusalThatRecursEndsTheFlightAlone(FoldSingleFlightBase):

    def until(self, predicate):
        """Wait, bounded, for `predicate`; whether it came true."""
        deadline = time.monotonic() + _PATIENCE_S
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_a_tainted_fold_sends_its_waiters_to_fold_alone_at_once(self):  # noqa: VACUOUS_ASSERTION — every reader's rows equal a full replay and the staged roads and waiters are counted exactly; the empty registry is the release
        """A TAINT IS REFUSED BY EVERY FOLD THAT REPLAYS ITS ROW (a
        compose-landed v3 close reads gate receipts, which `plan` cannot
        re-verify), so a second flight would be a second refused fold to
        wait for. The leader ends its flight ALONE: three readers, three cold
        roads, and the two waiters fold at once, side by side, with no
        reader waiting twice."""
        second = threading.Event()
        self.addCleanup(second.set)
        roads = _Roads(hold=lambda: second if roads.go.is_set() else True,
                       taint="a compose-landed v3 close read gate receipts")
        results, threads = {}, []
        with mock.patch.object(dispatches, "_fold_into", side_effect=roads):
            for i in range(3):
                threads.append(_run(dispatches.snapshot, "r%d" % i, results))
            self.settled(roads, lambda: roads.entered() >= 1
                         and roads.entered() + _waiting() == 3, "3 readers")
            roads.go.set()
            # THE SECOND ROUND, held at a door of its own: both waiters in
            # cold roads of their own, or one leading a second flight and
            # the other waiting on it.
            self.until(lambda: roads.entered() == 3
                       or (roads.entered() == 2 and _waiting() == 1))
            staged = (roads.entered(), _waiting())
            second.set()
            self.join(threads)
        for name, (how, value) in sorted(results.items()):
            self.assertEqual(how, "ok", "%s raised %r" % (name, value))
            self.assertEqual(repr(value[0]), self.want[0])
        self.assertEqual(staged, (3, 0), "a waiter waited on a second fold "
                                         "the same taint refused: %r"
                         % roads.roads)
        self.assertEqual(len(roads.cold()), 3, roads.roads)
        self.assertEqual(getattr(dispatches, "_FOLD_FLIGHTS", {}), {})
        # NOTHING WAS SAVED, the taint refused all three: the next read,
        # untainted, replays the whole ledger.
        with mock.patch.object(dispatches, "_fold_into",
                               wraps=dispatches._fold_into) as spy:
            rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertEqual(repr(rows), self.want[0])
        self.assertEqual([(c.args[2], len(c.args[1]))
                          for c in spy.call_args_list], [(0, self.count)])

    def test_a_reader_holding_the_ledger_lock_folds_without_waiting(self):  # noqa: VACUOUS_ASSERTION — the lock holder's rows equal a full replay, the lock is asserted held, and the cold roads are counted exactly
        """A DISPATCH WRITER's LAST TRY READS UNDER THE LEDGER LOCK, which
        every writer in every process queues on. Waiting there for a fold
        another thread leads would hold that lock across the leader's fold
        and any flight after it, so the holder folds on its own."""
        roads = _Roads(hold=lambda: threading.current_thread().name
                       == "leader")

        def writer():
            return dispatches._ledger_write(
                lambda txn: (txn.held, dispatches.snapshot()), tries=1)

        results = {}
        with mock.patch.object(dispatches, "_fold_into", side_effect=roads):
            leader = _run(dispatches.snapshot, "leader", results)
            self.settled(roads, lambda: roads.entered() == 1,
                         "the leader's cold fold")
            holder = _run(writer, "holder", results)
            self.until(lambda: "holder" in results or _waiting() == 1)
            staged = ("holder" in results, _waiting(), leader.is_alive())
            roads.go.set()
            self.join([leader, holder])
        self.assertEqual(staged, (True, 0, True),
                         "the lock holder waited on the leader's fold")
        for name in ("leader", "holder"):
            self.assertEqual(results[name][0], "ok", results[name])
        held, (rows, unavailable) = results["holder"][1]
        self.assertTrue(held, "the writer's last try did not hold the lock")
        self.assertIsNone(unavailable)
        self.assertEqual(repr(rows), self.want[0])
        self.assertEqual(len(roads.cold()), 2, roads.roads)
        self.assertEqual(getattr(dispatches, "_FOLD_FLIGHTS", {}), {})


class ALensedReaderFoldsItsLensedLedgerOnItsOwnThread(FoldSingleFlightBase):

    def test_a_lensed_reader_waits_for_the_plain_fold_and_folds_the_lens_itself(self):  # noqa: VACUOUS_ASSERTION — both readers' rows equal a full replay and every cold road is named by thread and lens
        """A board folds twice: plain, to derive its gate-epoch term, then
        under that lens. The PLAIN fold is the one every reader shares, so
        the board waits for it and restores it. The LENSED fold stays on the
        board's own thread, because the git answers it leaves in that
        thread's `projscope` memo are the ones the board's projection reads
        next. Measured on a copy of the live ledger: running the lensed fold
        in the leader's scope instead cut it from 16-19 s and 659 git
        processes to 4-5 s and none, and still raised the cold burst from
        about 4,900 git processes to about 5,900, because about 1,650 more
        were spawned OUTSIDE any fold, where the board projections read
        after it."""
        roads = _Roads(hold=lambda: threading.current_thread().name
                       == "plain")

        def lensed():
            # No marker and no stamped verdict: the marker's own term is
            # None, so a lens serving None is keyed ("none") like any other.
            with dispatches.epoch_lens(lambda: None):
                return dispatches.snapshot_and_events()

        results = {}
        with mock.patch.object(dispatches, "_fold_into", side_effect=roads):
            plain = _run(dispatches.snapshot, "plain", results)
            self.settled(roads, lambda: roads.entered() == 1,
                         "the plain leader")
            board = _run(lensed, "lensed", results)
            # A tree with no shared fold never waits: the lensed reader folds
            # plain and lensed itself, and that is the third cold road.
            self.settled(roads, lambda: _waiting() == 1
                         or roads.entered() >= 3, "the lensed reader")
            roads.go.set()
            self.join([plain, board])
        for name in ("plain", "lensed"):
            how, value = results[name]
            self.assertEqual(how, "ok", "%s raised %r" % (name, value))
            self.assertEqual(repr(value[0]), self.want[0])
        self.assertIsNone(results["lensed"][1][4])
        cold = sorted((who, lensed) for who, lensed, base, n in roads.who
                      if base == 0 and n > 0)
        self.assertEqual(cold, [("lensed", True), ("plain", False)],
                         "the cold folds were %r" % roads.who)
        self.assertEqual(getattr(dispatches, "_FOLD_FLIGHTS", {}), {})
        # THE LENSED CHECKPOINT IS REAL: a second lensed read restores it.
        with mock.patch.object(dispatches, "_fold_into",
                               wraps=dispatches._fold_into) as spy:
            again = lensed()
        self.assertEqual(repr(again[0]), self.want[0])
        self.assertEqual([(c.args[2], len(c.args[1]))
                          for c in spy.call_args_list],
                         [(self.count, 0), (self.count, 0)])

    def test_two_lensed_readers_each_fold_the_lens_while_plain_readers_share(self):  # noqa: VACUOUS_ASSERTION — every reader's rows equal a full replay and the cold roads are counted by lens; the empty registry is the release
        """Both boards (`lr` and `lr_all`) read under a lens serving the same
        term, so their lensed folds would share one key. A board that
        restored the other's lensed fold would lose the memo its projection
        reads next, the cost measured above. So a LENSED read takes no part
        in a flight, neither leading nor waiting, and each board folds its
        lens on its own thread, while every plain read, the two boards' own
        plain folds included, still shares one cold fold."""
        lens_go = threading.Event()
        self.addCleanup(lens_go.set)

        def lensed_now():
            return getattr(dispatches._EPOCH_LENS, "fn", None) is not None

        roads = _Roads(hold=lambda: lens_go if lensed_now() else True)

        def lensed():
            with dispatches.epoch_lens(lambda: None):
                return dispatches.snapshot_and_events()

        def cold(lens):
            with roads.mu:
                return sum(1 for _who, was, base, n in roads.who
                           if base == 0 and n > 0 and was == lens)

        def lens_waiting():
            flights = getattr(dispatches, "_FOLD_FLIGHTS", {})
            return sum(f.waiting for key, f in list(flights.items())
                       if len(key) > 3 and key[3] is not None)

        readers = [("plain1", dispatches.snapshot),
                   ("plain2", dispatches.snapshot_with_verdicts),
                   ("board1", lensed), ("board2", lensed)]
        results, threads = {}, []
        with mock.patch.object(dispatches, "_fold_into", side_effect=roads):
            for name, target in readers:
                threads.append(_run(target, name, results))
            # EVERY PLAIN FOLD, the boards' included: one cold, three behind.
            self.settled(roads, lambda: cold(False) == 1 and _waiting() == 3,
                         "the plain herd")
            roads.go.set()
            # THE TWO LENSED FOLDS, held at their own door: each in a cold
            # road of its own, or one cold and the other waiting on it.
            self.settled(roads, lambda: cold(True) + lens_waiting() == 2,
                         "both boards' lensed folds")
            staged = (cold(True), lens_waiting())
            lens_go.set()
            self.join(threads)
        for name, (how, value) in sorted(results.items()):
            self.assertEqual(how, "ok", "%s raised %r" % (name, value))
            self.assertEqual(repr(value[0]), self.want[0])
        self.assertEqual(staged, (2, 0), "a board waited on the other "
                                         "board's lensed fold: %r"
                         % roads.who)
        self.assertEqual((cold(False), cold(True)), (1, 2), roads.who)
        self.assertEqual(getattr(dispatches, "_FOLD_FLIGHTS", {}), {})
