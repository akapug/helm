#!/usr/bin/env python3
"""EVERY DISPATCH-LEDGER WRITER FOLDS OUTSIDE THE LOCK (task/3020).

THE DEFECT. One minute after a land the first `dispatches.snapshot()` on the
new trunk took 102.2 s and the next took 0.23 s: a code change keys a new
fold checkpoint, so the first read after it replays the whole ledger. Every
writer read that fold INSIDE `eventledger.locked(ledger)`, so the first
writer after a land held the dispatch ledger lock for the whole replay while
every `dispatch send` and `dispatch verdict` in the fleet waited behind it.

THE SURFACE-BY-STATE MATRIX. Rows are the writers; columns are the ledger
states a write can meet. The census in `tests.test_ledger_unknown_kinds`
(`_SEQ_WRITER_ARMS`) keeps the row set exhaustive, and the arms here drive
its `_WRITERS` table, so a new writer joins this matrix by construction.

  ROWS: `_append_dispatch` (send, add, rebind and stale-cure redispatch),
  `_mark_delivered`, `record_findings_note` (and the findings pass),
  `mark_verdict` (and the advisory read), `mark_cancel`, `mark_custody`,
  `mark_hold`, `mark_release`, `superseded_parent_sweep --apply`, `retip`,
  `_record_discharge_proven`, `_record_withdraw_proven`,
  `_record_abandon_proven`, `_record_retire_proven`,
  `_record_close_landed_proven`, `_record_close_proven` (every reason, the
  dry run, compose-land v3), `record_delivered_report_correction`, and the
  fold-checkpoint advance after an append.

  BEFORE, EVERY ROW ALIKE: the lock around the whole writer, so the fold,
  every check, every git or gate probe, the append and the checkpoint
  advance all ran holding it.
    warm checkpoint      tail fold (0.23 s measured) under the lock
    cold (code change)   full replay (102.2 s measured) under the lock, the
                         fleet's writers waiting
    absent / unreadable  the same full replay under the lock
    append between the   cannot happen: the lock excludes it, so the other
      read and the write writer waits instead
    ledger replaced      read and write under one lock see one file
      (new inode)
    lock unavailable     refused before the read, in the writer's words
    ledger absent        read as empty and created by the append, under one
      (a fresh home)     lock
    checkpoint advance   a fold after the append, still under the lock

  AFTER, EVERY ROW THROUGH `dispatches._ledger_write`:
    warm checkpoint      tail fold unlocked; the lock covers the ledger
                         identity check and the append ................ arm 1
    cold (code change)   full replay unlocked; the fleet writes meanwhile;
                         the redo that follows is warm ................ arm 3
    absent / unreadable  full replay unlocked; the same redo .......... arm 7
    append between the   the identity differs at the write, the try runs
      read and the write again from a fresh read, the event lands exactly
                         once ......................................... arm 4
                         after LEDGER_WRITE_TRIES - 1 misses the last try
                         reads under the lock and lands ............... arm 5
    ledger replaced      the inode differs, the try runs again against the
      (new inode)        new file ..................................... arm 6
    lock unavailable     read first, then the same refusal words, nothing
                         appended ..................................... arm 8
    ledger absent        ABSENT is an identity: a ledger still absent at the
      (a fresh home)     write has not moved, so the first try lands . arm 11
    checkpoint advance   after the lock is released ................... arm 2

  ROWS THAT DEVIATE, each because its record is a LIVE measurement taken at
  the write. The ledger's identity proves only the ledger unchanged, so each
  of these takes the lock itself (`txn.lock()`, which proves the fold it read
  is the ledger now held) BEFORE its probe, and the probe, the checks after
  it and the append are one critical section as before. Only the fold moved
  out from under the lock ................................................ arm 9
    `_record_close_proven`, compose-land v3   KEPT LOCKED FOR ITS RE-MEASURE:
        the Git/gate inputs `compose_contract.capture` re-measures are not
        the ledger, so the identity check cannot subsume them
    `_record_close_proven`, discharged        the `_writer_landed` re-walk
    `_record_abandon_proven`                  the object and interlock probes
    `_record_retire_proven`                   `measure`
    `mark_custody`                            the authorization's freshness
  AND THE ROWS THAT NEVER LOCK OR LOCK DIFFERENTLY:
    `_record_close_proven`, dry run   returns its rehearsal before any append,
        so it never takes the lock and never reaches the locked last try; a
        compose-land v3 rehearsal re-measures unlocked ................ arm 10
    `record_findings_note`            `FINDINGS_NOTE_TRIES` tries, not the
        default (tests.test_findingspass holds its last-try arm)
    `superseded_parent_sweep --apply` every annotation in one critical
        section after one identity check
    `_append_dispatch`                `prepare` (roster, `_base` git probes)
        runs unlocked; the parent annotation rides the same critical section

  A REDO THAT FINDS ITS OWN EFFECT ALREADY DONE says "already done", never
  "I did it". Only one writer's answer claims an ACTION rather than a state:
  `send`'s `sent`, which a cured retry reports True for a successor another
  call wrote and delivered. On a redo (`txn.redo`: an earlier try of this
  call decided to write and found the ledger moved) that answer is sent=False
  with the existing row, as the locked writer gave the race's loser ... arm 12.
  Every other writer's idempotent answer is the row's STATE, identical to the
  answer a sequential retry gets and to what the locked writer gave the loser:
  `_mark_delivered` (same ref), `mark_verdict` and the advisory read (the
  standing verdict, its attestation reconciled, never re-emitted),
  `mark_cancel`, `mark_hold`, `mark_release`, the six close doors and the
  delivered-report correction (the standing row); `mark_custody` already says
  `already=True`; `superseded_parent_sweep` lists only what it appended;
  `retip` refuses a row that moved; `record_findings_note` has no idempotent
  answer.

  A structural arm (arm 0) holds the whole of it: no function in the ledger
  modules but `_LedgerTxn` takes the dispatch ledger lock.
"""
import ast
import os
import shutil
import subprocess
import sys
import threading
import types
import unittest
from unittest import mock

from helm import dispatches, dispatches_close, eventledger, foldckpt
from tests import test_ledger_unknown_kinds as _uk
from tests._satellite_resolution import ledger_sources

#: Asks for the dispatch ledger lock from ANOTHER PROCESS and says whether it
#: could have it. Another process is what every other writer is.
_PROBE = ("import fcntl, os, sys\n"
          "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
          "try:\n"
          "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
          "except BlockingIOError:\n"
          "    print('held')\n"
          "else:\n"
          "    print('free')\n")


class LedgerWriteBase(_uk._Base):
    """Rows from the unknown-kinds matrix, plus a lock probe and a fold spy."""

    def lock_state(self):
        got = subprocess.run(
            [sys.executable, "-c", _PROBE, dispatches.ledger_path() + ".lock"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        return got.stdout.strip()

    def fold_spy(self):
        """(patch, probes): every fold the patch sees records the lock's
        state from another process, then folds for real."""
        probes = []
        real = dispatches._ledger_fold

        def fold(*args, **kwargs):
            probes.append(self.lock_state())
            return real(*args, **kwargs)
        return mock.patch.object(dispatches, "_ledger_fold",
                                 side_effect=fold), probes

    def probe(self, states, answer):
        """A live probe's stand-in: records the lock's state when the door
        calls it, then gives `answer`."""
        def measured(*_args, **_kwargs):
            states.append(self.lock_state())
            return answer
        return measured

    def events(self, rid, kind):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if e.get("id") == rid and e.get("event") == kind]

    def bystander(self):
        """An append by ANOTHER writer, landed with no lock of ours held."""
        other = self.row()
        seq = self.state(other["id"])["seq"]
        return lambda: self.assertTrue(eventledger.append_unlocked(
            dispatches.ledger_path(), {
                "v": 3, "event": "delivered", "seq": seq + 1,
                "id": other["id"], "ts": dispatches.pk.now_ts(),
                "delivery_ref": "bystander"})), other

    def reads(self, after_first=None, every=None):
        """(patch, reads): a snapshot spy counting reads; `after_first()`
        runs once, after the first read returns its (now stale) answer;
        `every(n)` runs after each read."""
        reads = []
        real = dispatches.snapshot

        def snapshot():
            got = real()
            reads.append(1)
            if after_first is not None and len(reads) == 1:
                after_first()
            if every is not None:
                every(len(reads))
            return got
        return mock.patch.object(dispatches, "snapshot",
                                 side_effect=snapshot), reads


class NoWriterTakesTheLockItselfTest(unittest.TestCase):
    """ARM 0, structural: the dispatch ledger lock has one owner."""

    def test_only_the_transaction_takes_the_dispatch_ledger_lock(self):
        tree = ast.parse("\n".join(src for _p, src in
                                   ledger_sources(dispatches)))
        functions = [(None, fn) for fn in tree.body
                     if isinstance(fn, ast.FunctionDef)]
        functions += [(cls.name, fn) for cls in tree.body
                      if isinstance(cls, ast.ClassDef)
                      for fn in cls.body if isinstance(fn, ast.FunctionDef)]
        owners = {("%s.%s" % (cls, fn.name)) if cls else fn.name
                  for cls, fn in functions if self.locks_the_ledger(fn)}
        # UNCONDITIONAL POSITIVE CONTROL: the walk finds the owner it must.
        self.assertIn("_LedgerTxn.lock", owners)
        self.assertEqual(owners, {"_LedgerTxn.__init__", "_LedgerTxn.lock"},
                         "a function other than the transaction takes the "
                         "dispatch ledger lock, so it can fold under it")

    @staticmethod
    def locks_the_ledger(fn):
        """Does `fn` call `eventledger.locked(...)` on anything but the
        attestation sidecar (`attest_path()`, directly or through a name
        bound to it), which is not the dispatch ledger?"""
        sidecar = {t.id for node in ast.walk(fn)
                   if isinstance(node, ast.Assign)
                   and "attest_path" in ast.dump(node.value)
                   for t in node.targets if isinstance(t, ast.Name)}
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) \
                    and isinstance(call.func, ast.Attribute) \
                    and call.func.attr == "locked" \
                    and isinstance(call.func.value, ast.Name) \
                    and call.func.value.id == "eventledger":
                arg = call.args[0] if call.args else None
                if "attest_path" in ast.dump(arg) or (
                        isinstance(arg, ast.Name) and arg.id in sidecar):
                    continue
                return True
        return False


class TheFoldRunsWithoutTheLockTest(LedgerWriteBase):
    """ARM 1: every writer, with a warm checkpoint, folds with the lock free
    as another process sees it."""

    def test_every_writer_folds_with_the_lock_free(self):  # noqa: VACUOUS_ASSERTION — each writer's probe list is asserted non-empty before its exact contents, and every writer this fixture can drive to success is asserted to grow the ledger
        for name, prepare, write in _uk._WRITERS:
            with self.subTest(writer=name):
                row = self.row()
                if prepare:
                    prepare(self, row["id"])
                before = len(self.ledger())
                patch, probes = self.fold_spy()
                with patch:
                    write(self, row["id"])
                self.assertTrue(probes, "%s read no fold at all" % name)
                self.assertEqual(set(probes), {"free"},
                                 "%s folded while the dispatch ledger lock "
                                 "was held" % name)
                if name in _uk._SUCCEEDS_CLEAN:
                    self.assertGreater(len(self.ledger()), before,
                                       "%s wrote nothing" % name)


class TheCheckpointAdvanceRunsAfterReleaseTest(LedgerWriteBase):
    """ARM 2."""

    def test_the_advance_runs_with_the_lock_free(self):  # noqa: VACUOUS_ASSERTION — the probe list is asserted to an exact three-element value, one per write
        row = self.row()
        probes = []
        real = dispatches._advance_checkpoint

        def advance():
            probes.append(self.lock_state())
            return real()
        with mock.patch.object(dispatches, "_advance_checkpoint",
                               side_effect=advance):
            self.assertIsNone(dispatches.mark_hold(row["id"], "held")[1])
            self.assertIsNone(dispatches.mark_release(row["id"])[1])
            self.assertIsNone(dispatches.mark_cancel(row["id"], "moot")[1])
        self.assertEqual(probes, ["free"] * 3,
                         "the checkpoint advance ran under the lock")


class AColdFoldDoesNotStallAConcurrentSendTest(LedgerWriteBase):
    """ARM 3: a writer paying a cold fold does not make a concurrent
    `dispatch send` wait, and then lands its own event exactly once."""

    def test_a_send_lands_while_another_writer_folds_cold(self):  # noqa: VACUOUS_ASSERTION — the send and the cancel are both asserted to land exactly once and the cancel to have read twice; `waited` is the bounded wait itself
        row = self.row()
        # COLD: no checkpoint, so the cancel's first read is a full replay.
        shutil.rmtree(foldckpt.store_dir(dispatches.ledger_path()),
                      ignore_errors=True)
        release, entered = threading.Event(), threading.Event()
        canceller, results, cancel_reads = {}, {}, []
        real_fold, real_snapshot = dispatches._ledger_fold, dispatches.snapshot

        def fold(*args, **kwargs):
            out = real_fold(*args, **kwargs)
            if threading.get_ident() == canceller.get("id") \
                    and not entered.is_set():
                entered.set()
                # STANDS IN FOR A REPLAY LONG ENOUGH TO MATTER: the fixture
                # ledger replays in milliseconds, the live one in minutes.
                release.wait(120)
            return out

        def snapshot():
            if threading.get_ident() == canceller.get("id"):
                cancel_reads.append(1)
            return real_snapshot()

        def cancel():
            canceller["id"] = threading.get_ident()
            results["cancel"] = dispatches.mark_cancel(row["id"], "moot")

        def send():
            results["send"] = dispatches.send(
                "ghost-reviewer", "lane/concurrent-send", "review this tip",
                self.side, repo=self.repo, sign=False, kind="review",
                new_work=True)
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "ghost-author"}), \
                mock.patch.object(dispatches, "_ledger_fold", side_effect=fold), \
                mock.patch.object(dispatches, "snapshot", side_effect=snapshot):
            a = threading.Thread(target=cancel, daemon=True)
            a.start()
            self.assertTrue(entered.wait(60), "the cancel never folded")
            b = threading.Thread(target=send, daemon=True)
            b.start()
            b.join(60)
            waited = b.is_alive()
            release.set()
            a.join(120)
            b.join(120)
        self.assertFalse(waited, "a send waited on a writer's cold fold")
        sent, why, _sent = results["send"]
        self.assertIsNone(why, why)
        self.assertIn(sent["id"], dispatches.snapshot()[0])
        self.assertEqual(results["cancel"][1], None, results["cancel"])
        self.assertEqual(len(self.events(row["id"], "cancel")), 1)
        self.assertEqual(len(cancel_reads), 2,
                         "the cancel did not read again after the send "
                         "appended")


class AnAppendBetweenReadAndWriteTest(LedgerWriteBase):
    """ARMS 4, 5 AND 6."""

    def test_the_try_runs_again_and_the_event_lands_once(self):  # noqa: VACUOUS_ASSERTION — the read count, the bystander's landing and the single hold event are all asserted to exact values
        row = self.row()
        append, other = self.bystander()
        patch, reads = self.reads(after_first=append)
        with patch:
            out, err = dispatches.mark_hold(row["id"], "waiting")
        self.assertIsNone(err, err)
        self.assertEqual(len(reads), 2, "the write did not read again")
        self.assertEqual(len(self.events(other["id"], "delivered")), 1)
        self.assertEqual(len(self.events(row["id"], "hold")), 1)
        self.assertEqual(out["seq"], self.state(row["id"])["seq"])

    def test_after_every_optimistic_miss_the_last_try_reads_under_the_lock(self):  # noqa: VACUOUS_ASSERTION — the probe list, the bystander count and the single hold event are asserted to exact values
        row = self.row()
        tries = dispatches.LEDGER_WRITE_TRIES
        racers = [self.bystander()[0] for _ in range(tries)]
        probes = []

        def every(n):
            probes.append(self.lock_state())
            if probes[-1] == "free":
                racers[n - 1]()
        patch, reads = self.reads(every=every)
        with patch:
            out, err = dispatches.mark_hold(row["id"], "waiting")
        self.assertIsNone(err, err)
        self.assertEqual(probes, ["free"] * (tries - 1) + ["held"],
                         "every read but the last runs with the lock free; "
                         "the last reads under it")
        self.assertEqual(len(self.events(row["id"], "hold")), 1)
        self.assertEqual(out["status"], "held")

    def test_a_replaced_ledger_is_read_again(self):  # noqa: VACUOUS_ASSERTION — the inode is asserted to change, the read count to two and the event to land once in the file now at the path
        row = self.row()
        path = dispatches.ledger_path()
        inodes = [os.stat(path).st_ino]

        def replace():
            copy = path + ".replacement"
            shutil.copyfile(path, copy)
            os.chmod(copy, 0o600)
            os.replace(copy, path)
            inodes.append(os.stat(path).st_ino)
        patch, reads = self.reads(after_first=replace)
        with patch:
            _out, err = dispatches.mark_cancel(row["id"], "moot")
        self.assertIsNone(err, err)
        self.assertNotEqual(inodes[0], inodes[1], "nothing was replaced")
        self.assertEqual(len(reads), 2, "a replaced ledger was not re-read")
        self.assertEqual(os.stat(path).st_ino, inodes[1])
        self.assertEqual(len(self.events(row["id"], "cancel")), 1)


class ACheckpointThatCannotBeReadTest(LedgerWriteBase):
    """ARM 7."""

    def test_an_unreadable_checkpoint_replays_outside_the_lock(self):  # noqa: VACUOUS_ASSERTION — at least one checkpoint file is asserted to exist and be spoiled first; the probes and the single cancel event are asserted exactly
        row = self.row()
        dispatches.snapshot()            # a checkpoint exists to spoil
        store = foldckpt.store_dir(dispatches.ledger_path())
        spoiled = []
        for name in os.listdir(store):
            with open(os.path.join(store, name), "wb") as f:
                f.write(b"\x00not a checkpoint")
            spoiled.append(name)
        self.assertTrue(spoiled, "no checkpoint to spoil")
        patch, probes = self.fold_spy()
        with patch:
            _out, err = dispatches.mark_cancel(row["id"], "moot")
        self.assertIsNone(err, err)
        self.assertTrue(probes)
        self.assertEqual(set(probes), {"free"})
        self.assertEqual(len(self.events(row["id"], "cancel")), 1)


class ALockThatCannotBeTakenTest(LedgerWriteBase):
    """ARM 8."""

    def test_the_refusal_comes_at_the_write_in_the_writers_words(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted exactly and the read count to one; the unchanged ledger is the no-append law
        row = self.row()
        before = self.ledger()

        class Refused(object):
            def __enter__(self):
                return False

            def __exit__(self, *exc):
                return False
        patch, reads = self.reads()
        with patch, mock.patch.object(eventledger, "locked",
                                      side_effect=lambda *a, **k: Refused()):
            out, err = dispatches.mark_cancel(row["id"], "moot")
        self.assertIsNone(out)
        self.assertEqual(err, "ledger unwritable (%s) — cancel NOT recorded"
                         % dispatches.ledger_path())
        self.assertEqual(len(reads), 1, "the write refused before it read")
        self.assertEqual(self.ledger(), before)


class ALiveProbeRunsUnderTheLockTest(LedgerWriteBase):
    """ARM 9: the doors whose record is a live measurement at the write take
    the lock BEFORE the probe, and still fold without it."""

    def check(self, write):
        """(probe states, fold states) for one door."""
        states = []
        patch, folds = self.fold_spy()
        with patch:
            write(states)
        self.assertTrue(states, "the door never reached its live probe")
        self.assertTrue(folds, "the door read no fold")
        return states, set(folds)

    def test_abandon_probes_the_object_under_the_lock(self):  # noqa: VACUOUS_ASSERTION — the probe states and the fold states are each asserted to exact values after check() asserts both non-empty
        row = self.verdicted(polarity="fix")
        states, folds = self.check(
            lambda states: dispatches_close._record_abandon_proven(
                row["id"], self.side, "matrix",
                self.probe(states, False), lambda *a: None))
        self.assertEqual((states, folds), (["held"], {"free"}))

    def test_retire_measures_under_the_lock(self):  # noqa: VACUOUS_ASSERTION — the probe states and the fold states are each asserted to exact values after check() asserts both non-empty
        row = self.row()
        states, folds = self.check(
            lambda states: dispatches_close._record_retire_proven(
                row["id"], dispatches.RETIRE_REASONS[0], "seat-a", None,
                self.probe(states, (None, "matrix stop"))))
        self.assertEqual((states, folds), (["held"], {"free"}))

    def test_custody_rechecks_its_authorization_under_the_lock(self):  # noqa: VACUOUS_ASSERTION — the probe states and the fold states are each asserted to exact values after check() asserts both non-empty
        from helm import takeover
        row = self.row()
        auth = types.SimpleNamespace(target="seat-b", source="ghost-author",
                                     reason="the lock matrix")

        def write(states):
            with mock.patch.object(takeover, "reassign_custody_error",
                                   side_effect=self.probe(states, None)):
                _out, err = dispatches.mark_custody(row["id"], auth)
            self.assertIsNone(err, err)
        states, folds = self.check(write)
        # the admission check before the read, then the recheck at the write
        self.assertEqual((states, folds), (["free", "held"], {"free"}))

    def test_compose_land_re_measures_under_the_lock(self):  # noqa: VACUOUS_ASSERTION — the probe states and the fold states are each asserted to exact values after check() asserts both non-empty
        from helm import compose_contract
        row = self.row()

        def write(states):
            with mock.patch.object(compose_contract, "writer_error",
                                   return_value=None), \
                    mock.patch.object(compose_contract, "capture",
                                      side_effect=self.probe(
                                          states, (None, "matrix stop"))):
                _out, err = dispatches_close._record_close_proven(
                    row["id"], "landed", self.side, close_proof_version=3)
            self.assertIn("matrix stop", err or "")
        states, folds = self.check(write)
        self.assertEqual((states, folds), (["held"], {"free"}))

    def test_a_discharge_re_walks_under_the_lock(self):  # noqa: VACUOUS_ASSERTION — the probe states and the fold states are each asserted to exact values after check() asserts both non-empty
        row = self.row()

        def write(states):
            with mock.patch.object(dispatches, "_close_mode_error",
                                   return_value=None), \
                    mock.patch.object(dispatches, "discharging_row",
                                      side_effect=self.probe(
                                          states, (None, None, "matrix stop"))):
                _out, err = dispatches_close._record_close_proven(
                    row["id"], "discharged", None, evidence="matrix")
            self.assertIn("matrix stop", err or "")
        states, folds = self.check(write)
        self.assertEqual((states, folds), (["held"], {"free"}))


class ARehearsalNeverLocksTest(LedgerWriteBase):
    """ARM 10: a compose-land v3 REHEARSAL re-measures without the lock."""

    def test_a_compose_land_dry_run_measures_unlocked(self):  # noqa: VACUOUS_ASSERTION — the probe states and the fold states are each asserted to exact values
        from helm import compose_contract
        row = self.row()
        states = []
        patch, folds = self.fold_spy()
        with patch, mock.patch.object(compose_contract, "writer_error",
                                      return_value=None), \
                mock.patch.object(compose_contract, "capture",
                                  side_effect=self.probe(
                                      states, (None, "matrix stop"))):
            _out, err = dispatches_close._record_close_proven(
                row["id"], "landed", self.side, close_proof_version=3,
                dry_run=True)
        self.assertIn("matrix stop", err or "")
        self.assertEqual((states, set(folds)), (["free"], {"free"}))



class ALostSendRaceIsNotASendTest(LedgerWriteBase):
    """ARM 12: two sends of one cured operation race; the one that loses finds
    the other's successor, written and delivered, on its redo, and must not
    report `sent`."""

    def test_the_send_that_lost_the_race_reports_not_sent(self):  # noqa: VACUOUS_ASSERTION — the winner is asserted sent, both answers one row, the loser's refusal by its text, and a sequential retry after them is asserted sent as the control
        parent = self.row()
        key = "stale-cure:%s:%s" % (parent["id"], self.b)
        held, released = threading.Event(), threading.Event()

        def hold(_current):
            # THE HOOK BETWEEN THE LOSER'S FOLD AND ITS LOCK: the cured
            # operation's last check runs inside the write's try, after the
            # read and before the append. The loser waits here until the
            # winner has appended and delivered.
            held.set()
            released.wait(120)

        def send(validate):
            return dispatches.send(
                "ghost-reviewer", "atomic-cure", "Review cure", self.b,
                repo=self.repo, kind="review", supersedes=parent["id"],
                key=key, unique_key=True, sign=False,
                _cured_operation={"validate": validate})
        results = {}
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "ghost-author"}):
            loser = threading.Thread(
                target=lambda: results.__setitem__("loser", send(hold)),
                daemon=True)
            loser.start()
            self.assertTrue(held.wait(120), "the loser never reached its "
                                            "write")
            won, why, sent = send(lambda _current: None)
            released.set()
            loser.join(120)
            self.assertFalse(loser.is_alive())
            again = send(lambda _current: None)
        self.assertIsNone(why, why)
        self.assertTrue(sent, "the winner did not send")
        lost, lost_why, lost_sent = results["loser"]
        self.assertEqual(lost["id"], won["id"])
        self.assertFalse(lost_sent, "the send that lost the race reported "
                                    "sent for a row it did not write")
        self.assertIn("do not resend", lost_why or "")
        self.assertNotIn(dispatches._WRITTEN_ELSEWHERE, lost)
        self.assertEqual(len([r for r in dispatches.rows().values()
                              if r.get("supersedes") == parent["id"]]), 1)
        # THE CONTROL: a cured retry AFTER the operation completed is still
        # the successful proof its caller reads, as it always was.
        self.assertEqual((again[0]["id"], again[2]), (won["id"], True))


class AFreshLedgerTest(unittest.TestCase):
    """ARM 11: an absent ledger is a known state, never a moved one."""

    def test_the_first_write_to_an_absent_ledger_lands_on_the_first_try(self):  # noqa: VACUOUS_ASSERTION — the try count, the answer and the one written event are asserted to exact values
        import tempfile
        root = tempfile.mkdtemp(prefix="helm-test-fresh-ledger-")
        self.addCleanup(shutil.rmtree, root, True)
        path = os.path.join(root, "fresh", "ledger.jsonl")
        tries = []

        def body(txn):
            tries.append(os.path.lexists(path))
            if not txn.append({"v": 1, "event": "probe", "id": "fresh"}):
                return "unwritable"
            return "wrote"
        self.assertEqual(dispatches._ledger_write(body, path=path), "wrote")
        self.assertEqual(tries, [False], "a write to an absent ledger read "
                                         "it as moved and tried again")
        self.assertEqual([e.get("id") for e in eventledger.events(path)],
                         ["fresh"])


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()
