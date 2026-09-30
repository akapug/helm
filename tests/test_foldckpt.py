#!/usr/bin/env python3
"""THE CHECKPOINTED DISPATCH FOLD ANSWERS EXACTLY WHAT A FULL REPLAY ANSWERS.

task/2770. The acceptance bar is EQUIVALENCE: in every scenario the rows, the
accepted-verdict index, the taken events and the activity index a checkpointed
read returns are byte-identical (compared by `repr`, which sees tuple versus
list and dict order) to a full replay of the same ledger bytes. Every arm has
two poles: the answer, and WHICH ROAD produced it — a restore folds only the
tail from position K, a full replay folds from zero — read off a spy on
`dispatches._fold_into`, so a checkpoint that silently stopped being used, or
one used when it should not be, is red rather than merely slower.

Every world here is a temp HELM_HOME and a temp git repository (LandReqBase);
no arm reads the live fleet. The carried close is the live-contingent case the
checkpoint exists for: its proof re-reads git on every replay, so the git facts
it read are what the checkpoint must re-verify.
"""
import contextlib
import fcntl
import hashlib
import io
import json
import marshal
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from helm import (dispatches, eventledger, foldckpt, gitfacts, landreq, pk,
                  projscope, registry, vcs)
import tests.test_lr_close as lrc

_LIVE_SEATS_PATCH = None


def setUpModule():
    # THIS MODULE DOES NOT READ HOST LIVENESS — the same stand-in, for the same
    # measured reason, as tests.test_landreq and tests.test_lr_close.
    global _LIVE_SEATS_PATCH
    from helm import proxywatch
    _LIVE_SEATS_PATCH = mock.patch.object(
        proxywatch, "_live_seats", lambda: (set(), None, {}))
    _LIVE_SEATS_PATCH.start()


def tearDownModule():
    global _LIVE_SEATS_PATCH
    if _LIVE_SEATS_PATCH is not None:
        _LIVE_SEATS_PATCH.stop()
    # THE GLOBAL GOES BACK TO WHAT IMPORT LEFT, as tests.test_landreq's does
    # (task/3039): a stopped patcher left here is module data the sliced
    # gate's leak audit reads as a rebinding, and fails the run.
    _LIVE_SEATS_PATCH = None


def _plant(root, name, age):
    """A stand-in checkpoint file named `name` whose mtime is `age` seconds
    before now; the name of the file."""
    path = os.path.join(root, name)
    with open(path, "wb") as f:
        f.write(b"x")
    at = time.time_ns() - int(age * 1_000_000_000)
    os.utime(path, ns=(at, at))
    return name


def _plant_valid(root, name, age, ledger):
    """A checkpoint of nothing, at offset 0 of `ledger`, named `name`, used
    `age` seconds ago: a file no universal key refuses (`_universal`), so a
    save's sweep leaves it to the age and the bounds."""
    payload = marshal.dumps({"out": {}, "verdicts": {}, "taken": {},
                             "actors": {"validated": {}, "unresolved": {}}})
    header = {"format": foldckpt.FORMAT, "v": foldckpt.VERSION,
              "policy": "0" * 64, "epoch": "absent", "lens": None,
              "ledger": {"path": os.path.abspath(ledger), "events": 0,
                         "offset": 0,
                         "sha256": hashlib.sha256(b"").hexdigest()},
              "realpaths": {}, "git": {},
              "payload": {"bytes": len(payload),
                          "sha256": hashlib.sha256(payload).hexdigest()}}
    with open(os.path.join(root, name), "wb") as f:
        f.write(json.dumps(header).encode("utf-8") + b"\n" + payload)
    at = time.time_ns() - int(age * 1_000_000_000)
    os.utime(os.path.join(root, name), ns=(at, at))
    return name


class FoldCheckpointBase(lrc.CloseBase):

    def setUp(self):
        super().setUp()
        self.assertTrue(dispatches.ledger_path().startswith(self.tmp),
                        "the ledger is not under this test's HELM_HOME")

    # -- the world ----------------------------------------------------------

    def carried(self, lane="lane/ckpt-carried"):
        """One FIX-verdicted row whose reviewed tip trunk carries, closed
        `carried` through the real ladder — the close whose replay reads git."""
        row = self.verdict_row(polarity="fix", lane=lane)
        self.git("cherry-pick", "--no-commit", self.side)
        self.git("commit", "-qm", "carry the side work")
        out, err = landreq.close(row["id"], "carried",
                                 evidence="the lander closed nothing",
                                 repo=self.repo)
        self.assertIsNone(err)
        self.assertIsNotNone(out)
        return row

    def append(self, event):
        """A raw append: the ledger grows and NO writer advances the
        checkpoint, so the next read meets a real tail."""
        self.assertTrue(eventledger.append(dispatches.ledger_path(), event))

    # -- the instruments ----------------------------------------------------

    def store_dir(self):
        # THE MODULE'S OWN ADDRESS, not a second spelling of it. The store is
        # per ledger, and a test that rebuilt the path from DIRNAME alone
        # would look one level above every checkpoint and read an empty
        # listing as "nothing was written".
        return foldckpt.store_dir(dispatches.ledger_path())

    def store(self):
        files = sorted(os.listdir(self.store_dir())) \
            if os.path.isdir(self.store_dir()) else []
        self.assertEqual(len(files), 1, files)
        return os.path.join(self.store_dir(), files[0])

    def header(self):
        with open(self.store(), "rb") as f:
            return json.loads(f.readline())

    def ledger_events(self):
        events, why = eventledger.checked_events(dispatches.ledger_path(),
                                                 strict=True)
        self.assertIsNone(why)
        return events

    def full_replay(self):
        """The reference: today's fold of every event, no checkpoint."""
        events = self.ledger_events()
        actors = {"validated": {}, "unresolved": {}}
        out, verdicts, taken = dispatches._fold(events, actors=actors)
        return {"rows": repr(out), "verdicts": repr(verdicts),
                "taken": repr(taken), "grouped": repr(dispatches._group(events)),
                "validated": repr(actors["validated"]),
                "unresolved": repr(actors["unresolved"])}, out

    def read(self):
        """(every checkpointed answer, [(base, n events) per fold]) for the
        three readers that fold the whole ledger."""
        with mock.patch.object(dispatches, "_fold_into",
                               wraps=dispatches._fold_into) as spy:
            rows, verdicts, un = dispatches._snapshot(track_verdicts=True)
            cur, grouped, taken, verdicts2, un2 = dispatches.snapshot_and_events()
            validated, unresolved, _newest, un3 = dispatches.seat_activity()
        self.assertEqual((un, un2, un3), (None, None, None))
        self.assertEqual(repr(rows), repr(cur))
        self.assertEqual(repr(verdicts), repr(verdicts2))
        roads = [(c.args[2], len(c.args[1])) for c in spy.call_args_list]
        return {"rows": repr(rows), "verdicts": repr(verdicts),
                "taken": repr(taken), "grouped": repr(grouped),
                "validated": repr(validated),
                "unresolved": repr(unresolved)}, roads

    def assert_equivalent(self):
        got, roads = self.read()
        want, out = self.full_replay()
        self.assertEqual(got, want, "the checkpointed answer is not the full "
                                    "replay's answer")
        return roads, out

    def assert_restored(self, roads, tail=0):
        count = len(self.ledger_events())
        self.assertTrue(roads, "no fold ran at all")
        self.assertTrue(all(base == count - tail and n == tail
                            for base, n in roads),
                        "expected every read to restore at %d and fold %d "
                        "events, got %r" % (count - tail, tail, roads))

    def assert_full(self, roads):
        count = len(self.ledger_events())
        self.assertIn((0, count), roads, "no read replayed the whole ledger: "
                                         "%r" % roads)

    def settle(self):
        """Read until the checkpoint describes the whole ledger."""
        self.read()
        self.assertEqual(self.header()["ledger"]["events"],
                         len(self.ledger_events()))


class EquivalenceArms(FoldCheckpointBase):

    def test_a_no_new_events_restores_and_answers_the_full_replay(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality with a full replay on every read; the road and absence assertions name which path produced it
        row = self.carried()
        self.settle()
        roads, out = self.assert_equivalent()
        self.assert_restored(roads, tail=0)
        # THE MUST-HIT: the checkpoint carries the git-derived close itself,
        # so an equality over a world with no carried close proves nothing.
        self.assertEqual(out[row["id"]]["close_reason"], "carried")
        # THE OTHER POLE: with the store gone the same read replays it all.
        shutil.rmtree(self.store_dir())
        roads, _out = self.assert_equivalent()
        self.assert_full(roads)

    def test_b_appended_events_fold_only_the_tail_at_absolute_positions(self):
        self.carried()
        held = self.verdict_row(polarity="fix", lane="lane/ckpt-tail")
        cancel = self.dispatch(ref=self.a, lane="lane/ckpt-open",
                               kind="review")
        judged = self.dispatch(ref=self.b, lane="lane/ckpt-judged",
                               kind="review")
        self.settle()
        before = self.header()["ledger"]["events"]
        state = dispatches.snapshot()[0][cancel["id"]]
        judged_seq = dispatches.snapshot()[0][judged["id"]]["seq"]
        tail = [{"v": 3, "event": "hold", "seq": state["seq"] + 1,
                 "id": cancel["id"], "ts": dispatches.pk.now_ts(),
                 "reason": "waiting on the tail arm"},
                {"v": 3, "event": "release", "seq": state["seq"] + 2,
                 "id": cancel["id"], "ts": dispatches.pk.now_ts(),
                 "reason": "released"},
                {"v": 3, "event": "cancel", "seq": state["seq"] + 3,
                 "id": cancel["id"], "ts": dispatches.pk.now_ts(),
                 "reason": "moot"},
                {"v": 3, "event": "verdict", "seq": judged_seq + 1,
                 "id": judged["id"], "ts": dispatches.pk.now_ts(),
                 "reviewed_tip": self.b, "polarity": "approve",
                 "verdict_ref": "approved in the tail"}]
        for event in tail:
            self.append(event)
        got, roads = self.read()
        self.assertEqual(roads[0], (before, len(tail)),
                         "the first read after the append did not fold "
                         "exactly the tail from the checkpoint's position")
        want, out = self.full_replay()
        self.assertEqual(got, want)
        self.assertEqual(out[cancel["id"]]["status"], "cancelled")
        self.assertEqual(self.header()["ledger"]["events"],
                         before + len(tail), "the reader did not advance the "
                                             "checkpoint over the tail")
        self.assertEqual(out[held["id"]]["status"], "verdict")
        # ABSOLUTE, NOT TAIL-RELATIVE: the tail's verdict is indexed at its own
        # ledger position, which a fold restarting at 0 would get wrong.
        at = [i for i, e in enumerate(self.ledger_events())
              if e.get("id") == judged["id"] and e.get("event") == "verdict"]
        self.assertEqual(len(at), 1)
        self.assertGreaterEqual(at[0], before)
        self.assertEqual(dispatches.snapshot_with_verdicts()[1][judged["id"]][0],
                         at[0])

    def test_c_a_rewritten_or_truncated_ledger_is_a_full_replay(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality with a full replay on every read; the road and absence assertions name which path produced it
        self.carried()
        self.verdict_row(polarity="fix", lane="lane/ckpt-rewrite")
        self.settle()
        path = dispatches.ledger_path()
        with open(path, "rb") as f:
            data = f.read()
        # A SAME-LENGTH REWRITE OF THE FIRST EVENT: valid JSON, one byte of
        # prose changed, so size and line count cannot see it.
        first, rest = data.split(b"\n", 1)
        self.assertIn(b"review ", first)
        with open(path, "wb") as f:
            f.write(first.replace(b"review ", b"reviEw ", 1) + b"\n" + rest)
        roads, _out = self.assert_equivalent()
        self.assert_full(roads)
        # AND A TRUNCATION: the last event is gone.
        self.settle()
        with open(path, "rb") as f:
            data = f.read()
        with open(path, "wb") as f:
            f.write(data[:data.rstrip(b"\n").rfind(b"\n") + 1])
        roads, _out = self.assert_equivalent()
        self.assert_full(roads)
        # THE OTHER POLE: untouched, it restores.
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)

    def test_d_a_policy_change_is_a_full_replay_into_its_own_file(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality with a full replay on every read; the road and absence assertions name which path produced it
        self.carried()
        self.settle()
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)
        with mock.patch.object(foldckpt, "policy", return_value="f" * 64):
            roads, _out = self.assert_equivalent()
            self.assert_full(roads[:1])
            names = sorted(os.listdir(self.store_dir()))
            self.assertEqual(len(names), 2, names)
            self.assertEqual(sum(n.startswith("f" * 16) for n in names),
                             1, names)
            roads, _out = self.assert_equivalent()
            self.assert_restored(roads)
        # A PROCESS THAT CANNOT NAME ITS CODE touches no checkpoint at all.
        with mock.patch.object(foldckpt, "policy", return_value=None), \
                mock.patch.object(foldckpt.Session, "save") as save:
            roads, _out = self.assert_equivalent()
            self.assert_full(roads)
            self.assertFalse(save.called)

    def test_e_a_recorded_object_made_absent_then_present(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality with a full replay on every read; the road and absence assertions name which path produced it
        row = self.carried()
        self.settle()
        _roads, out = self.assert_equivalent()
        self.assertEqual(out[row["id"]].get("close_reason"), "carried")
        # KEEP A COPY TO BRING THE OBJECT BACK, then destroy it for real.
        backup = os.path.join(self.tmp, "backup")
        subprocess.run(["git", "clone", "-q", "--mirror", self.repo, backup],
                       check=True, capture_output=True)
        self.prune(self.side, "refs/heads/side")
        roads, out = self.assert_equivalent()
        self.assert_full(roads)
        self.assertIsNone(out[row["id"]].get("close_reason"),
                          "the pruned tip still reads carried — the arm "
                          "never reached the flip it exists to show")
        self.settle()
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)
        # AND BACK: the object reappears, so the refusal recorded "missing"
        # no longer stands.
        self.git("fetch", "-q", backup, "refs/heads/side:refs/heads/side")
        roads, out = self.assert_equivalent()
        self.assert_full(roads)
        self.assertEqual(out[row["id"]].get("close_reason"), "carried")

    def test_f_trunk_ADVANCED_restores_and_answers_the_full_replay(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality with a full replay on every read; the road assertion names which path produced it
        """THE ONE ARM ABOVE THAT MOVES TRUNK AT ALL, and task/2860 is what
        makes it restore. A land moves trunk, every recorded expression
        naming it resolves to a new commit, and before that change the
        checkpoint was discarded for it.

        It restores now because a trunk that ADVANCED is not a trunk that
        changed. What licenses that is measured, not assumed, and the whole
        scenario set lives in `TrunkAdvanceArms` below -- one arm per way
        trunk can move, each saying by name whether it restores or replays.
        THE MOVE HERE IS AN UNRELATED FILE, which is exactly why this arm on
        its own was never proof of anything: it is the easiest case in that
        set, and a cure that only satisfied it would go green while losing
        every other."""
        row = self.carried()
        self.settle()
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)
        self.commit("trunk moves on", path="elsewhere")
        roads, out = self.assert_equivalent()
        self.assert_restored(roads)
        self.assertEqual(out[row["id"]].get("close_reason"), "carried")

    def test_g_the_gate_epoch_marker_changed_is_a_full_replay(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality with a full replay on every read; the road and absence assertions name which path produced it
        self.carried()
        self.settle()
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)
        marker = dispatches.epoch_path()
        self.assertTrue(marker.startswith(self.tmp))
        with open(marker, "w", encoding="utf-8") as f:
            f.write('{"v": 2, "index": 0}\n')
        roads, _out = self.assert_equivalent()
        self.assert_full(roads)
        os.unlink(marker)
        roads, _out = self.assert_equivalent()
        self.assert_full(roads)

    def test_h_a_corrupt_or_foreign_checkpoint_is_a_full_replay(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality with a full replay on every read; the road and absence assertions name which path produced it
        self.carried()
        self.settle()
        with open(self.store(), "rb") as f:
            good = f.read()
        head, _nl, payload = good.partition(b"\n")
        header = json.loads(head)
        foreign = marshal.dumps({"out": "not a fold"})
        header_foreign = dict(header, payload={
            "bytes": len(foreign),
            "sha256": __import__("hashlib").sha256(foreign).hexdigest()})
        cases = {
            "garbage": b"\x00\xffnot a checkpoint at all",
            "empty": b"",
            "other format": json.dumps(dict(header, format="x")).encode()
            + b"\n" + payload,
            "tampered payload": head + b"\n" + payload[:-1]
            + bytes([payload[-1] ^ 1]),
            "foreign payload": json.dumps(header_foreign).encode() + b"\n"
            + foreign,
        }
        for name, blob in cases.items():
            with self.subTest(name):
                with open(self.store(), "wb") as f:
                    f.write(blob)
                roads, _out = self.assert_equivalent()
                self.assert_full(roads)
                # ...and the full replay wrote a good one back.
                roads, _out = self.assert_equivalent()
                self.assert_restored(roads)

    def test_i_the_writers_advance_under_the_lock_equals_a_readers_fold(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality with a full replay on every read; the road and absence assertions name which path produced it
        self.verdict_row(polarity="approve", lane="lane/ckpt-writer-base")
        self.settle()
        count = len(self.ledger_events())
        # REAL WRITERS, and the carried close one of them appends is a
        # git-derived decision the advance must record rather than freeze blind.
        with mock.patch.object(dispatches, "_advance_checkpoint",
                               wraps=dispatches._advance_checkpoint) as adv:
            row = self.carried(lane="lane/ckpt-writer")
            cancel = self.dispatch(ref=self.a, lane="lane/ckpt-writer-open",
                                   kind="review")
            out, err = dispatches.mark_cancel(cancel["id"], "moot")
        self.assertIsNone(err)
        self.assertTrue(adv.called, "no writer advanced the checkpoint")
        self.assertTrue(self.header()["git"], "the advance over a carried "
                                              "close recorded no git answer")
        self.assertGreater(len(self.ledger_events()), count)
        self.assertEqual(self.header()["ledger"]["events"],
                         len(self.ledger_events()),
                         "the writer's advance left a tail behind")
        with open(self.store(), "rb") as f:
            _head, _nl, payload = f.read().partition(b"\n")
        stored = marshal.loads(payload)
        want, out = self.full_replay()
        self.assertEqual(repr(stored["out"]), want["rows"])
        self.assertEqual(repr(stored["verdicts"]), want["verdicts"])
        self.assertEqual(out[cancel["id"]]["status"], "cancelled")
        self.assertEqual(out[row["id"]].get("close_reason"), "carried")
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)


class TheEpochLensIsPartOfTheKey(FoldCheckpointBase):
    """A FOLD UNDER AN EPOCH LENS IS CHECKPOINTED, ON ITS OWN FILE.

    The land projection installs a lens over `gate_epoch` for the whole build,
    and `_apply` consumes that answer while it validates a close — so the
    lensed fold's answer is a function of the served term and the lensed and
    plain folds of one ledger are two folds. They are keyed apart rather than
    refused: refusing means every land-pipeline rebuild replays the whole
    ledger, and sharing one file means each read discards the other's.

    THE POLES OF EVERY ARM ARE THE SAME TWO the module's equivalence arms use:
    the answer must be the LENSED full replay's answer byte for byte, and the
    road that produced it is read off the spy on `dispatches._fold_into`."""

    def lensed_equivalent(self, fn):
        """(roads, rows) for the three readers under `fn`, proved equal to a
        full replay taken under the same lens."""
        with dispatches.epoch_lens(fn):
            got, roads = self.read()
            want, out = self.full_replay()
        self.assertEqual(got, want, "the checkpointed answer under a lens is "
                                    "not the lensed full replay's answer")
        return roads, out

    def assert_no_full(self, roads):
        count = len(self.ledger_events())
        self.assertNotIn((0, count), roads,
                         "a read still replayed the whole ledger: %r" % roads)

    def store_names(self):
        d = self.store_dir()
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def lens_terms(self):
        """The `lens` term every stored checkpoint is keyed on."""
        out = []
        for name in self.store_names():
            with open(os.path.join(self.store_dir(), name), "rb") as f:
                out.append(json.loads(f.readline()).get("lens"))
        return sorted(out, key=lambda t: (t is not None, t))

    def marker_epoch(self):
        return dispatches._gate_epoch_uncached()

    def test_a_a_lensed_read_restores_instead_of_replaying(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality with the LENSED full replay on every read, plus the carried close read off the rows; the road assertions only name which path produced it
        row = self.carried()
        epoch = self.marker_epoch()
        lens = lambda: epoch                                   # noqa: E731
        self.lensed_equivalent(lens)          # cold: writes the lensed file
        roads, out = self.lensed_equivalent(lens)
        self.assert_no_full(roads)
        self.assert_restored(roads, tail=0)
        # THE MUST-HIT: the ledger carries the git-derived close itself, so
        # the equality above is over a world a checkpoint can get wrong.
        self.assertEqual(out[row["id"]]["close_reason"], "carried")
        # THE OTHER POLE: with the store gone the same lensed read replays.
        shutil.rmtree(self.store_dir())
        roads, _out = self.lensed_equivalent(lens)
        self.assert_full(roads)

    def test_b_a_plain_checkpoint_is_never_served_to_a_lensed_read(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality on every read and the two stored lens terms; the closing CONTROL restores on the lensed file in the same world
        self.carried()
        self.settle()
        plain = self.store_names()
        self.assertEqual(len(plain), 1, plain)
        self.assertEqual(self.lens_terms(), [None])
        epoch = self.marker_epoch()
        roads, _out = self.lensed_equivalent(lambda: epoch)
        self.assert_full(roads)
        names = self.store_names()
        self.assertEqual(len(names), 2, names)
        self.assertEqual(self.lens_terms(),
                         [None, dispatches._epoch_identity(epoch)])
        # AND THE REVERSE. With the plain file removed, a plain read may not
        # be served the lensed one either: the flavour is in the file name, so
        # the plain reader does not even look at it.
        os.unlink(os.path.join(self.store_dir(), plain[0]))
        roads, _out = self.assert_equivalent()
        self.assert_full(roads)
        # CONTROL: the lensed file is still there and still serves its own.
        roads, _out = self.lensed_equivalent(lambda: epoch)
        self.assert_no_full(roads)

    def test_c_a_lens_that_contradicts_the_marker_keys_nothing(self):  # noqa: VACUOUS_ASSERTION — the absence of a lensed store is bracketed by the equality with the lensed full replay, the carried close, and the CONTROL where the marker's own term DOES store one
        row = self.carried()
        epoch = self.marker_epoch()
        bogus = 4242 if epoch != 4242 else 99
        for _ in range(2):
            roads, out = self.lensed_equivalent(lambda: bogus)
            self.assert_full(roads)
        self.assertEqual(out[row["id"]]["close_reason"], "carried")
        # Only the plain fold that resolved the marker wrote anything: no
        # store holds a fold judged against a boundary no file backs.
        self.assertEqual(self.lens_terms(), [None])
        # CONTROL: the marker's OWN term does key a file, in the same world.
        self.lensed_equivalent(lambda: epoch)
        self.assertEqual(self.lens_terms(),
                         [None, dispatches._epoch_identity(epoch)])

    def test_d_a_fold_the_lens_answered_off_key_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the refused save is bracketed by the CONTROL save of the same fold under the term the key names, which stores and is read back from the store
        self.carried()
        self.settle()
        path = dispatches.ledger_path()
        data, why = eventledger.read_bytes(path)
        self.assertIsNone(why)
        session = foldckpt.begin(path, dispatches.epoch_path(), data,
                                 lens="index:7")
        self.assertIsNotNone(session)
        # THE PLAIN FILE IS NOT THIS KEY'S FILE, even though every other term
        # of the key — code, marker, ledger prefix — is identical.
        self.assertIsNone(session.restore())
        events, why = eventledger.checked_rows(data, strict=False)
        self.assertIsNone(why)
        acc = dispatches._Acc(actors={"validated": {}, "unresolved": {}})
        with foldckpt.recording() as rec:
            dispatches._fold_into(acc, events, 0)
        self.assertTrue(acc.count)
        off = foldckpt.Recorder()
        off.calls, off.realpaths = list(rec.calls), dict(rec.realpaths)
        off.taints, off.epochs = list(rec.taints), ["index:9"]
        self.assertFalse(session.save(acc.state(), acc.count, session.end,
                                      None, off),
                         "a fold the lens answered off key was stored")
        self.assertEqual(self.lens_terms(), [None])
        # CONTROL: the same fold, recorded with the term the key names, saves.
        rec.epochs.append("index:7")
        self.assertTrue(session.save(acc.state(), acc.count, session.end,
                                     None, rec))
        self.assertEqual(self.lens_terms(), [None, "index:7"])


class TheKeyAndItsRefusals(FoldCheckpointBase):

    def test_a_fold_that_read_git_outside_the_seam_writes_no_checkpoint(self):  # noqa: VACUOUS_ASSERTION — the absent store is bracketed by the equality with a full replay and by the CONTROL read that does write one
        self.carried()
        shutil.rmtree(self.store_dir(), ignore_errors=True)
        real = dispatches._apply

        def unseamed_apply(*args, **kwargs):
            vcs.unseamed("an arm's direct git read")
            return real(*args, **kwargs)

        with mock.patch.object(dispatches, "_apply", unseamed_apply):
            got, roads = self.read()
        self.assertFalse(os.path.isdir(self.store_dir())
                         and os.listdir(self.store_dir()),
                         "a fold that read git outside the seam was "
                         "checkpointed")
        want, _out = self.full_replay()
        self.assertEqual(got, want)
        # THE CONTROL: the same world, read without the unseamed call, IS
        # checkpointed — so the absence above is the refusal and not a store
        # this fixture could never write.
        self.read()
        self.assertTrue(os.listdir(self.store_dir()))

    def test_a_config_change_git_merge_machinery_reads_is_a_full_replay(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality with a full replay on every read; the road and absence assertions name which path produced it
        self.carried()
        self.settle()
        # branch.* is the one family deliberately left out of the key: lane
        # creation rewrites it all day, and no recorded question reads it.
        self.git("config", "branch.side.remote", "origin")
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)
        self.git("config", "merge.renames", "false")
        roads, _out = self.assert_equivalent()
        self.assert_full(roads)

    def test_an_info_attributes_line_is_a_full_replay(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the byte-for-byte equality with a full replay on every read; the road and absence assertions name which path produced it
        self.carried()
        self.settle()
        info = os.path.join(self.repo, ".git", "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "attributes"), "w",
                  encoding="utf-8") as f:
            f.write("* merge=union\n")
        roads, _out = self.assert_equivalent()
        self.assert_full(roads)

    def test_a_strict_reader_meets_a_corrupt_tail_exactly_as_before(self):  # noqa: VACUOUS_ASSERTION — the positives are the legacy reader's own reason asserted non-empty and equal, and the lenient rows equal to a full replay
        self.carried()
        self.settle()
        count = self.header()["ledger"]["events"]
        with open(dispatches.ledger_path(), "ab") as f:
            f.write(b"{this is not json}\n")
        legacy_rows, legacy_why = eventledger.checked_events(
            dispatches.ledger_path(), strict=True)
        self.assertTrue(legacy_why)
        got = dispatches.snapshot_and_events()
        self.assertEqual(got, ({}, {}, {}, {}, legacy_why))
        activity = dispatches.seat_activity()
        self.assertEqual(activity, (None, None, None, legacy_why))
        # THE LENIENT READER SKIPS THE LINE, AS IT ALWAYS DID...
        lenient, _why = eventledger.checked_events(dispatches.ledger_path())
        want, _v, _t = dispatches._fold(lenient)
        self.assertEqual(repr(dispatches.snapshot()[0]), repr(want))
        # ...AND AN UNCLEAN TAIL IS NEVER CHECKPOINTED.
        self.assertEqual(self.header()["ledger"]["events"], count)

    def test_an_epoch_lens_off_the_marker_keys_no_checkpoint(self):  # noqa: VACUOUS_ASSERTION — the spy's keys are bracketed by the roads the same reads produced, and by the CONTROL where the marker's own term both names a key and restores on it
        """A lens serving a term the marker does not back names no key, so its
        fold is read from and written to nothing — the road a lensed fold
        takes whenever its term cannot be re-verified against a file."""
        self.carried()
        self.settle()
        seen = []
        real = foldckpt.begin

        def spy(ledger, marker, data, lens=None):
            seen.append(lens)
            return real(ledger, marker, data, lens)

        with dispatches.epoch_lens(lambda: 4242), \
                mock.patch.object(foldckpt, "begin", spy):
            _got, roads = self.read()
        self.assert_full(roads)
        self.assertTrue(seen, "no read reached the checkpoint door at all")
        self.assertEqual(set(seen), {None},
                         "a lens off the marker named a checkpoint key")
        # CONTROL: the marker's OWN term names a key, and a read on it
        # restores instead of replaying the ledger.
        epoch = dispatches._gate_epoch_uncached()
        del seen[:]
        with dispatches.epoch_lens(lambda: epoch), \
                mock.patch.object(foldckpt, "begin", spy):
            self.read()
            _got, roads = self.read()
        self.assertIn(dispatches._epoch_identity(epoch), seen)
        self.assertNotIn((0, len(self.ledger_events())), roads)

    def test_a_changed_realpath_discards_the_checkpoint(self):
        """The fold's close arms compare a repository path to its realpath;
        the checkpoint re-resolves every one it recorded."""
        target = os.path.join(self.tmp, "real")
        link = os.path.join(self.tmp, "link")
        os.makedirs(target)
        os.symlink(target, link)
        data = b'{"id":"x"}\n'
        path = dispatches.ledger_path()
        code = foldckpt.policy()
        self.assertTrue(code, "this process cannot name its code")
        session = foldckpt.Session(path, dispatches.epoch_path(), data, code,
                                   foldckpt.marker_key(dispatches.epoch_path()))
        with foldckpt.recording() as rec:
            self.assertEqual(foldckpt.realpath(link), target)
        state = {"out": {}, "verdicts": {}, "taken": {},
                 "actors": {"validated": {}, "unresolved": {}}}
        self.assertTrue(session.save(state, 1, len(data), None, rec))
        self.assertIsNotNone(session.restore())
        os.unlink(link)
        os.symlink(self.tmp, link)
        self.assertIsNone(session.restore())


class ARepositoryThatIsGoneTest(FoldCheckpointBase):

    def session(self):
        data = b'{"id":"x"}\n'
        code = foldckpt.policy()
        self.assertTrue(code, "this process cannot name its code")
        return foldckpt.Session(dispatches.ledger_path(),
                                dispatches.epoch_path(), data, code,
                                foldckpt.marker_key(dispatches.epoch_path())), \
            data

    def test_a_deleted_repository_is_a_fact_the_checkpoint_can_keep(self):
        """A close whose repository was deleted asks git once and is refused.
        That refusal must be checkpointable, or one deleted clone named on the
        ledger makes every read a full replay — and the directory coming back
        must discard it."""
        gone = os.path.join(self.tmp, "deleted-clone", ".git")
        sha = "a" * 40
        rec = foldckpt.Recorder()
        rec.calls.append((gone, ("cat-file", "-e", sha + "^{commit}"),
                          (), False, 128, b""))
        session, data = self.session()
        state = {"out": {}, "verdicts": {}, "taken": {},
                 "actors": {"validated": {}, "unresolved": {}}}
        self.assertTrue(session.save(state, 1, len(data), None, rec))
        self.assertIsNotNone(session.restore())
        subprocess.run(["git", "init", "-q", os.path.dirname(gone)],
                       check=True, capture_output=True)
        self.assertIsNone(session.restore(),
                          "the repository came back and the checkpoint that "
                          "recorded it gone was still served")

    def test_an_empty_ledger_leaves_no_store_behind(self):  # noqa: VACUOUS_ASSERTION — the CONTROL below writes one row and asserts the same read DOES create the store
        """A read of a home with no ledger must not write anything."""
        self.assertFalse(os.path.exists(dispatches.ledger_path()))
        self.assertEqual(dispatches.snapshot(), ({}, None))
        self.assertFalse(os.path.exists(self.store_dir()))
        # THE CONTROL: one row, and the same read does write it.
        self.dispatch(ref=self.a, lane="lane/ckpt-first", kind="review")
        self.read()
        self.assertTrue(os.listdir(self.store_dir()))

    def test_a_nearly_spent_budget_keeps_its_answer_and_skips_the_write(self):  # noqa: VACUOUS_ASSERTION — the answer is asserted non-empty and the next unbudgeted read is asserted to write the store
        self.dispatch(ref=self.a, lane="lane/ckpt-budget", kind="review")
        shutil.rmtree(self.store_dir(), ignore_errors=True)
        with mock.patch.object(foldckpt.projscope, "remaining",
                               return_value=foldckpt.SAVE_RESERVE_S / 2):
            rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertEqual(len(rows), 1)
        self.assertFalse(os.path.isdir(self.store_dir())
                         and os.listdir(self.store_dir()))
        rows_again, _unavailable = dispatches.snapshot()
        self.assertEqual(repr(rows_again), repr(rows))
        self.assertTrue(os.listdir(self.store_dir()))


class TheHomeRepositoryIsPartOfTheKey(FoldCheckpointBase):
    """A fold may resolve a legacy row's repository to the RUNNING package's
    (`dispatches.home_repo_id`) — the chain join a source-clean close's replay
    runs does (task/3053). Two checkouts running identical code share a code
    digest, so the key carries that answer and re-asks it on every read."""

    session = ARepositoryThatIsGoneTest.session

    def test_a_fold_that_read_the_home_repository_is_keyed_on_its_answer(self):
        here = os.path.join(self.tmp, "here.git")
        there = os.path.join(self.tmp, "there.git")
        state = {"out": {}, "verdicts": {}, "taken": {},
                 "actors": {"validated": {}, "unresolved": {}}}
        session, data = self.session()
        with mock.patch.object(dispatches, "home_repo_id",
                               return_value=(here, None)):
            with foldckpt.recording() as rec:
                self.assertEqual(foldckpt.home_repo_id(), (here, None))
            self.assertEqual(rec.homes, [here])
            self.assertTrue(session.save(state, 1, len(data), None, rec))
            self.assertIsNotNone(session.restore())
        with mock.patch.object(dispatches, "home_repo_id",
                               return_value=(there, None)):
            self.assertIsNone(session.restore(),
                              "a checkpoint keyed on one home repository was "
                              "served to a helm that belongs to another")
            # AND MEASURED AT THE SAVE: a fold whose home read no longer
            # answers the same is not written.
            self.assertFalse(session.save(state, 1, len(data), None, rec))


class ThePlan(unittest.TestCase):
    """The allowlist of git questions, one call at a time."""

    def plan(self, *calls):
        rec = foldckpt.Recorder()
        rec.calls.extend(("/r/.git", tuple(args), (), False, rc, out)
                         for args, rc, out in calls)
        return foldckpt.plan(rec)

    def test_the_carried_witness_questions_are_planned(self):
        sha = "a" * 40
        out = self.plan(
            (("cat-file", "-e", sha + "^{commit}"), 0, b""),
            (("rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}"),
             0, (("b" * 40) + "\n").encode()),
            (("rev-parse", "--is-shallow-repository"), 0, b"false\n"),
            (("merge-tree", "--write-tree", "--merge-base=" + sha, "b" * 40,
              "c" * 40), 1, b"d" * 40),
            (("cherry", "b" * 40, sha), 0, b"- " + sha.encode()))
        entry = out["/r/.git"]
        self.assertEqual(entry["shallow"], "false")
        self.assertTrue(entry["machinery"])
        self.assertEqual(entry["exprs"]["refs/heads/main^{commit}"], "b" * 40)
        self.assertEqual(entry["exprs"][sha + "^{commit}"], foldckpt.PRESENT)
        self.assertIn("c" * 40, entry["exprs"])

    def test_anything_else_is_unplannable(self):  # noqa: VACUOUS_ASSERTION — each subTest's assertRaises IS the positive; the loop runs a fixed four-case tuple
        for args, rc, out in (
                (("log", "--oneline"), 0, b""),
                (("cat-file", "-e", "a" * 40), -1, b""),     # did not finish
                (("merge-base", "--is-ancestor", "a" * 40, "b" * 40), 128, b""),
                (("rev-parse", "HEAD"), 0, ("a" * 40).encode())):
            with self.subTest(args=args, rc=rc):
                with self.assertRaises(foldckpt.Unplannable):
                    self.plan((args, rc, out))

    def test_one_question_answered_two_ways_is_unplannable(self):
        sha = "a" * 40
        with self.assertRaises(foldckpt.Unplannable):
            self.plan((("cat-file", "-e", sha + "^{commit}"), 0, b""),
                      (("cat-file", "-e", sha + "^{commit}"), 128, b""))

    def test_a_taint_is_unplannable(self):
        rec = foldckpt.Recorder()
        rec.taints.append("a compose-landed v3 close read gate receipts")
        with self.assertRaises(foldckpt.Unplannable):
            foldckpt.plan(rec)

    def test_only_a_refusal_about_one_moment_of_the_fold_is_unsettled(self):  # noqa: VACUOUS_ASSERTION — each subTest's assertRaises IS the positive; the loops run fixed tuples
        """`Unsettled` names the refusals the NEXT fold of the same bytes may
        not meet: a git read that did not finish, or an input that moved
        while this fold read it. Every other refusal is a property of what
        the fold asks, so every fold that asks it again is refused again, and
        a reader waiting for another try gains nothing (task/3082)."""
        sha = "a" * 40
        for calls in (
                ((("cat-file", "-e", sha), -1, b""),),
                ((("cat-file", "-e", sha + "^{commit}"), 0, b""),
                 (("cat-file", "-e", sha + "^{commit}"), 128, b"")),
                ((("rev-parse", "--is-shallow-repository"), 0, b"false\n"),
                 (("rev-parse", "--is-shallow-repository"), 0, b"true\n"))):
            with self.subTest(calls=calls):
                with self.assertRaises(foldckpt.Unsettled):
                    self.plan(*calls)
        for args, rc, out in (
                (("log", "--oneline"), 0, b""),
                (("merge-base", "--is-ancestor", sha, "b" * 40), 128, b""),
                (("rev-parse", "HEAD"), 0, sha.encode())):
            with self.subTest(args=args, rc=rc):
                with self.assertRaises(foldckpt.Unplannable) as caught:
                    self.plan((args, rc, out))
                self.assertNotIsInstance(caught.exception, foldckpt.Unsettled)
        rec = foldckpt.Recorder()
        rec.taints.append("a compose-landed v3 close read gate receipts")
        with self.assertRaises(foldckpt.Unplannable) as caught:
            foldckpt.plan(rec)
        self.assertNotIsInstance(caught.exception, foldckpt.Unsettled)


class ThePolicyAsksTheTreeAtMostOncePerSecond(unittest.TestCase):
    """`policy()` compares the package's files with the ones this process
    imported at most once per second, and a mismatch is final for the
    process (task/3039, the design ruling).

    MEASURED BEFORE: every fold read stat-walked about 490 files; the walk
    ran 6,198 times in tests.test_lr_close and was 16.6% of its profile.

    WHAT THE TTL COSTS, and why it is safe: a long-running process can write
    one checkpoint under its old digest up to one second after a deploy
    rewrites the tree. A new process computes a new digest, so it never reads
    that checkpoint. And once this process has seen the tree differ it never
    names its code again, which is stricter than the old check: that one
    answered with the old digest again if the files were put back.
    """

    def setUp(self):
        self.clock = [100.0]
        self.walks = []
        real = foldckpt._source_stats
        self.drift = None

        def walk():
            self.walks.append(self.clock[0])
            return self.drift if self.drift is not None else real()

        for patch in (mock.patch.object(foldckpt, "_monotonic",
                                        lambda: self.clock[0]),
                      mock.patch.object(foldckpt, "_source_stats", walk),
                      mock.patch.dict(foldckpt._POLICY,
                                      {"checked": None, "drifted": False})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_second_ask_inside_the_second_walks_nothing(self):
        code = foldckpt.policy()
        self.assertIsNotNone(code, "control: this process names its code")
        asked = len(self.walks)
        self.assertGreater(asked, 0, "control: the first ask compared")
        self.clock[0] += foldckpt.POLICY_TTL_S / 2
        self.assertEqual(foldckpt.policy(), code)
        self.assertEqual(len(self.walks), asked,
                         "an ask inside the TTL walked the package")
        self.clock[0] += foldckpt.POLICY_TTL_S
        self.assertEqual(foldckpt.policy(), code)
        self.assertEqual(len(self.walks), asked + 1,
                         "an ask past the TTL did not compare again")

    def test_the_ttl_is_one_second(self):
        self.assertEqual(foldckpt.POLICY_TTL_S, 1.0)

    def test_a_mismatch_inside_the_second_is_seen_at_its_end(self):
        """The staleness bound, pinned: a tree that changes just after a
        compare is answered with the old digest until the TTL runs out, and
        never after."""
        code = foldckpt.policy()
        self.assertIsNotNone(code, "control: this process names its code")
        self.drift = {"planted.py": (1, 2, 3, 4)}
        self.clock[0] += 0.75 * foldckpt.POLICY_TTL_S
        self.assertEqual(foldckpt.policy(), code)
        self.clock[0] += 0.25 * foldckpt.POLICY_TTL_S
        self.assertIsNone(foldckpt.policy(),
                          "the changed tree was not seen once the TTL ran out")

    def test_a_mismatch_is_final_for_the_process(self):  # noqa: VACUOUS_ASSERTION — the walk counter is proven live at the end of this arm: a fresh process state under the same patch must compare
        self.drift = {"planted.py": (1, 2, 3, 4)}
        self.assertIsNone(foldckpt.policy(), "control: a changed tree")
        self.drift = None                      # the files are put back
        asked = len(self.walks)
        for _ in range(3):
            self.clock[0] += 10 * foldckpt.POLICY_TTL_S
            self.assertIsNone(foldckpt.policy(),
                              "a process that saw its tree change named "
                              "its code again")
        self.assertEqual(len(self.walks), asked,
                         "a drifted process kept walking the package")
        with mock.patch.dict(foldckpt._POLICY,
                             {"checked": None, "drifted": False}):
            foldckpt.policy()
        self.assertGreater(len(self.walks), asked,
                           "control: a fresh process state does compare")

    def test_a_new_process_names_its_code_again(self):
        """Final is per process: a fresh state compares afresh."""
        self.drift = {"planted.py": (1, 2, 3, 4)}
        self.assertIsNone(foldckpt.policy(), "control: a changed tree")
        self.drift = None
        with mock.patch.dict(foldckpt._POLICY,
                             {"checked": None, "drifted": False}):
            self.assertIsNotNone(foldckpt.policy())


if __name__ == "__main__":
    unittest.main()

class TrunkAdvanceArms(FoldCheckpointBase):
    """EVERY WAY TRUNK CAN MOVE UNDER A PROVEN CARRIED CLOSE, and what today's
    checkpoint does about each.

    WHY THIS CLASS EXISTS. `test_f_trunk_moved_is_a_full_replay` is the only
    arm above that moves trunk, and its move is `path="elsewhere"` -- an
    UNRELATED file. That is the one advance under which a carried close's
    answer is provably stable, so an oracle holding only that case would go
    green on the safe case and READ as proof that restore-across-a-land is
    sound. A restore-across-a-land cure flips `assert_full` to
    `assert_restored`; these arms are the set it must flip EXPLICITLY, one
    name at a time, so the diff says what it licensed.

    EVERY ARM HERE ASSERTS `assert_full` TODAY and passes on arrival: today a
    trunk move discards the checkpoint unconditionally, which is correct and
    is exactly the cost task/2860 is about. The arms are not testing a cure;
    they are pinning the scenario set so no cure can be judged against the
    easy third of it.

    THE SECOND POLE IS THE ANSWER, and it is the half that makes these
    falsifiers rather than labels. Each arm also asserts whether the FOLD'S
    ANSWER for the carried row survives the advance, which is what a restore
    would have to reproduce. Measured, not assumed:

      ANSWER STABLE   trunk edits/deletes/reverts a path the row touched, or
                      gains an unrelated commit or a merge. The CONTENT
                      witnesses go quiet in every one of these -- but the
                      fallthrough to `reached-trunk` catches them, because it
                      asks about trunk's HISTORY, which a later edit cannot
                      erase. The two-family design is doing exactly its job.
      ANSWER CHANGES  the reviewed tip becomes REACHABLE FROM TRUNK after the
                      close. Then `git cherry` has an empty range, an empty
                      range affirms every possible trunk, the witness must
                      stay silent, and the replayed close is refused -- the
                      row returns to OPEN. That is task/2863, and it is the
                      ONLY mechanism measured here that moves the answer.

    WHAT IS NOT HERE, and why its absence is a finding. A squash land and a
    merge reviewed-tip both destroy `reached-trunk`'s ability to speak, and
    `carriage_proof` called directly does narrow for them. Neither can be
    CLOSED carried in the first place: the close-time vacuity guard refuses,
    in as many words ("carried is fail-closed on an unaskable question").
    The guard restricts the reachable input set, so a narrowing of the
    predicate in isolation is not a defect of the fold until some close can
    carry it there. Both refusals are pinned below rather than dropped.
    """

    def advance(self, row, move):
        """Checkpoint the world, move trunk, and hand BOTH poles back.

        THIS HELPER DELIBERATELY GRADES NOTHING. An earlier cut took the
        expected answer as an argument and asserted it here, which reads
        tidier and is worse: every arm then differs only by a literal, the
        abstraction grades itself, and the one property each arm exists to
        state is invisible at the arm. The road assertions live here because
        they are IDENTICAL for every scenario today; the ANSWER is what
        varies, so it is asserted where it varies.

        THE ROAD COMES BACK UNJUDGED for the same reason the answer does.
        Before task/2860's restore, every scenario here replayed and the
        helper could assert that once; now the road is exactly what varies
        between arms, so asserting it here would put the interesting half of
        each arm back inside the helper.

        BOTH POLES COME BACK for the same reason. An arm whose whole claim is
        that the answer is GONE needs its own unconditional positive control
        on the SAME observable, or a world that never produced a carried
        close would satisfy it silently. Handing the before-value back puts
        that control at the arm rather than one call away from it.
        """
        self.settle()
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)
        self.assertEqual(
            dispatches.snapshot()[0][row["id"]].get("close_reason"), "carried",
            "the world under test never had a carried close, so the advance "
            "below would prove nothing")
        before = dispatches.snapshot()[0][row["id"]].get("close_reason")
        move()
        roads, out = self.assert_equivalent()
        return before, out[row["id"]].get("close_reason"), roads

    # -- the advances that LEAVE the answer alone ---------------------------

    def test_an_UNRELATED_path_RESTORES_and_the_answer_is_STABLE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are one call deep in `advance`: the byte-for-byte equality with a full replay on BOTH reads, the restored-then-full road assertions, and the close_reason=='carried' precondition; the arm's own assertEqual pins both poles of the answer
        """The baseline, and the only case `test_f` above covers."""
        row = self.carried()
        was, answer, roads = self.advance(row, lambda: self.commit("trunk moves on",
                                                       path="elsewhere"))
        self.assertEqual((was, answer), ("carried", "carried"))
        self.assert_restored(roads)

    def test_a_TOUCHED_path_EDITED_RESTORES_and_the_answer_is_STABLE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are one call deep in `advance`: the byte-for-byte equality with a full replay on BOTH reads, the restored-then-full road assertions, and the close_reason=='carried' precondition; the arm's own assertEqual pins both poles of the answer
        """THE CONTENT WITNESSES GO QUIET HERE AND THE ANSWER DOES NOT MOVE.
        Both content witnesses compare against trunk's CURRENT tree, so a
        later edit to a path the row touched silences them -- and
        `reached-trunk` answers instead, because the commit is still in
        trunk's history. A restore-across-a-land cure may license this one."""
        row = self.carried()
        was, answer, roads = self.advance(row, lambda: self.commit(
            "trunk edits the carried file", path="g"))
        self.assertEqual((was, answer), ("carried", "carried"))
        self.assert_restored(roads)

    def test_a_TOUCHED_path_DELETED_RESTORES_and_the_answer_is_STABLE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are one call deep in `advance`: the byte-for-byte equality with a full replay on BOTH reads, the restored-then-full road assertions, and the close_reason=='carried' precondition; the arm's own assertEqual pins both poles of the answer
        row = self.carried()
        was, answer, roads = self.advance(row, lambda: (
            self.git("rm", "-q", "g"),
            self.git("commit", "-qm", "trunk deletes the carried file")))
        self.assertEqual((was, answer), ("carried", "carried"))
        self.assert_restored(roads)

    def test_a_TOUCHED_path_DELETED_then_READDED_RESTORES_and_is_STABLE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are one call deep in `advance`: the byte-for-byte equality with a full replay on BOTH reads, the restored-then-full road assertions, and the close_reason=='carried' precondition; the arm's own assertEqual pins both poles of the answer
        """The tree is what gets compared, never the history that produced
        it, so a delete-and-restore round trip is invisible to the content
        witnesses rather than merely survivable."""
        row = self.carried()
        was, answer, roads = self.advance(row, lambda: (
            self.git("rm", "-q", "g"),
            self.git("commit", "-qm", "trunk deletes the carried file"),
            self.git("revert", "--no-edit", "HEAD")))
        self.assertEqual((was, answer), ("carried", "carried"))
        self.assert_restored(roads)

    def test_trunk_REVERTING_the_carry_RESTORES_and_the_answer_is_STABLE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are one call deep in `advance`: the byte-for-byte equality with a full replay on BOTH reads, the restored-then-full road assertions, and the close_reason=='carried' precondition; the arm's own assertEqual pins both poles of the answer
        """AND THIS ONE IS THE SHARPEST OF THE STABLE SET, because it is the
        case where stability is arguable rather than obvious: trunk no longer
        CONTAINS the work, and the close still reads `carried`. That is the
        module's own position -- `_close_ladder_carried` says a landing that
        was later replaced "is a CONTRARY to adjudicate, not a row to close".
        The close recorded a true measurement; a later revert is a new fact
        owed a new row, not a retroactive falsification of the old one."""
        row = self.carried()
        was, answer, roads = self.advance(
            row, lambda: self.git("revert", "--no-edit", "HEAD"))
        self.assertEqual((was, answer), ("carried", "carried"))
        self.assert_restored(roads)

    def test_a_MERGE_COMMIT_on_trunk_RESTORES_and_the_answer_is_STABLE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are one call deep in `advance`: the byte-for-byte equality with a full replay on BOTH reads, the restored-then-full road assertions, and the close_reason=='carried' precondition; the arm's own assertEqual pins both poles of the answer
        """`git cherry` SKIPS MERGES, so a merge arriving on trunk could in
        principle strand the witness. It does not: the merge is on the
        UPSTREAM side, and only a merge at the reviewed TIP is skipped."""
        row = self.carried()
        was, answer, roads = self.advance(row, lambda: (
            self.git("checkout", "-q", "-b", "feat"),
            self.commit("feature", path="feat.txt"),
            self.git("checkout", "-q", self.main),
            self.git("merge", "--no-ff", "-q", "-m", "merge feat", "feat")))
        self.assertEqual((was, answer), ("carried", "carried"))
        self.assert_restored(roads)

    # -- the advances that MOVE the answer (task/2863) ----------------------

    def test_the_TIP_BECOMING_AN_ANCESTOR_RESTORES_and_the_answer_is_STABLE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are one call deep in `advance`: the byte-for-byte equality with a full replay on BOTH reads, the restored-then-full road assertions, and the close_reason=='carried' precondition; the arm's own assertEqual pins both poles of the answer
        """task/2863, MEASURED END TO END rather than argued from the witness.

        The tip cannot be an ancestor when the close is MINTED -- the
        close-time vacuity guard refuses that outright (pinned below). So the
        only way into this state is trunk reaching the tip AFTERWARDS, which
        a merge of the lane does. Then `merge-base(trunk, tip) == tip`, the
        range is empty, an empty range affirms every possible trunk, the
        witness must stay silent, and the replayed close is REFUSED.

        A STRONGER FACT ABOUT THE WORLD PRODUCES A WEAKER STATE: the work is
        now literally in trunk's history, and the row returns to OPEN."""
        row = self.carried()
        was, answer, roads = self.advance(row, lambda: self.git(
            "merge", "--no-ff", "-q", "-m", "trunk merges the lane whole",
            "side"))
        self.assertEqual((was, answer), ("carried", "carried"),
                         "a proven carried close was UN-CLOSED by its own "
                         "work reaching trunk -- task/2863")
        self.assert_restored(roads)

    def test_an_ANCESTOR_TIP_plus_a_TOUCHED_path_edit_RESTORES_STABLE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are one call deep in `advance`: the byte-for-byte equality with a full replay on BOTH reads, the restored-then-full road assertions, and the close_reason=='carried' precondition; the arm's own assertEqual pins both poles of the answer
        """Both legs mute at once -- content silenced by the edit, history
        silenced by the empty range. Same answer, and it matters that it is
        the same: a cure that only taught the content family to survive an
        edit would still lose this row."""
        row = self.carried()
        was, answer, roads = self.advance(row, lambda: (
            self.git("merge", "--no-ff", "-q", "-m",
                     "trunk merges the lane whole", "side"),
            self.commit("trunk edits the carried file", path="g")))
        self.assertEqual((was, answer), ("carried", "carried"))
        self.assert_restored(roads)

    def test_a_DESCENDANT_of_the_tip_reaching_trunk_RESTORES_and_KEEPS_it(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are one call deep in `advance`: the byte-for-byte equality with a full replay on BOTH reads, the restored-then-full road assertions, and the close_reason=='carried' precondition; the arm's own assertEqual pins both poles of the answer
        """THE LANE ITSELF NEVER LANDS HERE, and the row is lost anyway.
        Anything DESCENDING from the reviewed tip makes that tip an ancestor,
        so the trigger is not "this lane was merged" but "any work built on
        this tip arrived". That is the shape most likely to happen by
        accident, and the one a cure keyed on the lane name would miss."""
        row = self.carried()
        was, answer, roads = self.advance(row, lambda: (
            self.git("checkout", "-q", "-b", "onside", "side"),
            self.commit("built on the reviewed tip", path="onside.txt"),
            self.git("checkout", "-q", self.main),
            self.git("merge", "--no-ff", "-q", "-m",
                     "trunk merges work built on the tip", "onside")))
        self.assertEqual((was, answer), ("carried", "carried"))
        self.assert_restored(roads)

    # -- the advances that must STILL replay --------------------------------

    def test_a_FORCE_MOVED_trunk_still_REPLAYS_because_it_did_not_advance(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is bracketed by two unconditional positives on the same observables: an ORDINARY advance on the same world restores first (assert_restored), and assert_full names the road the force-move produced
        """A DESCENDANT, NEVER MERELY A DIFFERENT COMMIT.

        The restore's whole licence is that the work the fold proved on trunk
        is still on trunk, which a descendant guarantees by construction. A
        force-push, a rewrite or a rollback guarantees nothing, and the
        recorded commit is then not an ancestor of what replaced it. This is
        the arm that keeps the licence from widening into "trunk is different,
        carry on".
        """
        row = self.carried()
        self.settle()
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)
        # THE CONTROL FIRST, on the same world: an ORDINARY advance restores.
        self.commit("trunk moves on", path="elsewhere")
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)
        # NOW THE FORCE-MOVE. Trunk is rewound and re-grown, so the commit the
        # fold proved against is on no branch and cannot be an ancestor.
        self.git("reset", "--hard", "-q", "HEAD~2")
        self.commit("a different history", path="divergent")
        roads, out = self.assert_equivalent()
        self.assert_full(roads)
        # AND THE ANSWER MOVED, WHICH IS THE POINT RATHER THAN A DETAIL. The
        # rewind took the carry with it, so trunk genuinely no longer holds
        # this row's work and the replayed close is correctly refused. That is
        # the harm a restore here would have done: it would have served
        # `carried` for a row whose work trunk had lost. The road assertion
        # says the checkpoint was discarded; this says WHY that mattered.
        self.assertIsNone(out[row["id"]].get("close_reason"),
                          "trunk was rewound past the carry and the close "
                          "still reads carried")

    def test_a_FOLD_MODULE_change_still_REPLAYS_even_when_trunk_ADVANCED(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives run FIRST and on the same world: the same trunk advance restores before the code key moves, and the arm asserts assert_full plus close_reason=='carried' under the changed key
        """THE CODE KEY OUTRANKS THE TRUNK ESCAPE, and the ordering is the
        property.

        The restore admitted by task/2860 lives in the GIT section of the
        staleness check. The code key is checked BEFORE it, so a fold whose
        code changed must replay whatever trunk did -- otherwise the new
        escape would become a way to serve a checkpoint written by other code,
        which is the one failure mode worse than a cold fold.

        The two conditions are combined ON PURPOSE: either alone would pass
        with the ordering reversed, and only both together pin it.
        """
        row = self.carried()
        self.settle()
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)
        self.commit("trunk moves on", path="elsewhere")
        # THE CONTROL: that advance alone restores, so the replay below is the
        # code key and not the trunk move.
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)
        self.commit("trunk moves again", path="elsewhere")
        other = "0" * 64
        self.assertNotEqual(other, foldckpt.policy())
        with mock.patch.object(foldckpt, "policy", lambda: other):
            roads, out = self.assert_equivalent()
            self.assert_full(roads)
            self.assertEqual(out[row["id"]].get("close_reason"), "carried")

    # -- the MEASURED trunk (task/3056) --------------------------------------
    #
    # A carried close is replayed against the trunk commit it RECORDED while
    # that commit is still history of trunk, and against the head only once it
    # is not. The arms below are the ruling's: a rewrite that drops the carry
    # falls back to the head and reopens the row, a revert leaves it closed,
    # the task/2863 reachability check still keeps a close on the fallback
    # road, and an unreadable ancestry probe refuses by name. The content arm
    # first, because it is the one the isolated copy of the live ledger found.

    def content_carried(self):
        """One FIX row CHAINED to a build row, so it has a work pair, closed
        `carried` by the CONTENT witness (`carriage-replay`): trunk carries the
        reviewed postimage under a rewritten sha, and nothing has edited it
        since. The rows `carried()` builds are `--new-work` and have no pair,
        so only the history witness can ever speak for them."""
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "content-lane")
        reviewed = self.commit("R", path="feature")
        self.git("checkout", "-q", self.main)
        self.commit("d")                       # trunk moves: parents differ
        self.git("cherry-pick", reviewed)
        build = self.dispatch(ref=base, lane="lane/content-carried",
                              kind="build")
        row = dispatches.add(
            "seat-b", "lane/content-carried-review", ref=reviewed,
            repo=self.repo, kind="review", notify=False, new_work=False,
            supersedes=build["id"])
        _out, err = self.mark_verdict(row["id"], reviewed, "findings",
                                      polarity="fix")
        self.assertIsNone(err)
        out, err = landreq.close(row["id"], "carried",
                                 evidence="the lander closed nothing",
                                 repo=self.repo)
        self.assertIsNone(err)
        self.assertIsNotNone(out)
        event = self.close_event(row["id"])
        self.assertEqual(event.get("close_proof_mode"),
                         dispatches.CARRIAGE_REPLAY,
                         "the fixture's close must be the CONTENT witness's, "
                         "or the edit below proves nothing about it")
        return row, event

    def test_a_CONTENT_proven_close_whose_path_trunk_EDITS_stays_CLOSED(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are one call deep in `advance`: the byte-for-byte equality with a full replay on BOTH reads and the close_reason=='carried' precondition; the arm's own assertEqual pins both poles
        """THE SHAPE THE PARITY RUN FOUND ON THE LIVE LEDGER (task/3056).

        Eight of the thirty closes the content witness proved fold REFUSED
        when replayed at the head, because trunk later edited a path they
        touched: the merge now conflicts, the content witness goes silent,
        the history witness answers instead with no base, and the replay arm
        refuses the pair it recorded. So "a trunk edit leaves the answer
        stable" was true only of history-proven closes. Replayed against the
        trunk it was measured at, the close answers what it answered when it
        was minted.

        AND THE CHECKPOINT DISAGREED WITH THE REPLAY BEFORE THIS CURE. An
        advance restores the checkpoint (task/2860), which kept `carried`,
        while a full replay at the head refused it -- so `assert_equivalent`
        inside `advance` is what goes red on the head-replay code."""
        row, _event = self.content_carried()
        was, answer, roads = self.advance(row, lambda: self.commit(
            "trunk edits the carried line's file", path="feature"))
        self.assertEqual((was, answer), ("carried", "carried"))
        self.assert_restored(roads)

    def test_a_REWRITE_that_drops_the_measured_trunk_REPLAYS_AT_HEAD_and_REOPENS(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is bracketed by the close_reason=='carried' precondition in `advance` and by the falsifier on the SAME world, which forces the ancestry answer True and reads 'carried' back
        """RULING ARM 1: trunk rewritten so the commit the close was measured
        against is no longer an ancestor, and the carry dropped with it. The
        recorded trunk has lost its licence, the close is re-derived at the
        head exactly as before this lane, and the head does not carry it."""
        row = self.carried()
        measured = self.close_event(row["id"])["closing_trunk_sha"]
        seen = []
        real = dispatches._measured_trunk_is_history

        def spy(gitdir, sha, ref):
            got = real(gitdir, sha, ref)
            seen.append((sha, got))
            return got

        def rewrite():
            self.git("reset", "--hard", "-q", "HEAD~1")
            self.commit("a different history", path="divergent")

        with mock.patch.object(dispatches, "_measured_trunk_is_history", spy):
            was, answer, roads = self.advance(row, rewrite)
        self.assert_full(roads)
        self.assertIn((measured, False), seen,
                      "the fold never asked whether the measured trunk is "
                      "still history, or was told it is")
        self.assertIsNone(answer, "trunk was rewritten past the carry and the "
                                  "close still reads carried")
        self.assertEqual(was, "carried")
        # THE FALSIFIER ON THE SAME WORLD: a recorded replay WITHOUT its
        # licence -- the ancestry answer forced True -- keeps the row closed.
        # So the None above is the ancestry probe's doing, not a world in
        # which no replay could affirm.
        with mock.patch.object(dispatches, "_measured_trunk_is_history",
                               lambda *_a: True):
            _full, out = self.full_replay()
        self.assertEqual(out[row["id"]].get("close_reason"), "carried")

    def test_a_REVERT_keeps_the_measured_trunk_as_history_and_the_row_CLOSED(self):  # noqa: VACUOUS_ASSERTION — the one inequality is a fixture must-hit (the revert really moved trunk off the measured commit); the arm's observables are positive: close_reason=='carried' off the full replay, and the probe's (measured, True) read off the same folds
        """RULING ARM 2: a revert is a descendant, so the measured trunk is
        still history of it and the close is replayed where it was proven."""
        row = self.carried()
        measured = self.close_event(row["id"])["closing_trunk_sha"]
        self.settle()
        self.assertEqual(
            dispatches.snapshot()[0][row["id"]].get("close_reason"), "carried",
            "the world under test never had a carried close")
        self.git("revert", "--no-edit", "HEAD")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), measured)
        seen = []
        real = dispatches._measured_trunk_is_history

        def spy(gitdir, sha, ref):
            got = real(gitdir, sha, ref)
            seen.append((sha, got))
            return got

        # ONLY THE READS AFTER THE REVERT ARE SPIED, so the ancestry answer
        # below belongs to the reverted trunk and not to the settle before it.
        with mock.patch.object(dispatches, "_measured_trunk_is_history", spy):
            roads, out = self.assert_equivalent()
        self.assertEqual(out[row["id"]].get("close_reason"), "carried")
        self.assertIn((measured, True), seen,
                      "the revert's full replay never asked the measured "
                      "trunk's ancestry, or was told it is gone")
        self.assert_restored(roads)

    def test_the_TIP_reaching_a_REWRITTEN_trunk_is_KEPT_by_the_2863_check(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are one call deep in `advance`: the byte-for-byte equality with a full replay on BOTH reads and the close_reason=='carried' precondition; the arm's own assertEqual pins both poles
        """RULING ARM 3, on the one road where task/2863 is still reached: the
        measured trunk is gone (the rewrite), so the close falls back to the
        head, where the lane was merged whole -- the tip is history, the
        history witness has an empty range, and only the reachability check
        can keep the row closed."""
        row = self.carried()

        def rewrite_then_merge():
            self.git("reset", "--hard", "-q", "HEAD~1")
            self.git("merge", "--no-ff", "-q", "-m",
                     "the rewritten trunk merges the lane whole", "side")

        was, answer, roads = self.advance(row, rewrite_then_merge)
        self.assert_full(roads)
        self.assertEqual((was, answer), ("carried", "carried"),
                         "a close whose tip trunk now reaches was un-closed "
                         "on the fallback road -- task/2863")

    def test_a_PRUNED_measured_trunk_falls_back_to_the_head_where_2863_KEEPS_it(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is the must-differ control on the SAME world and the SAME observable (close_reason off a full replay), bracketed by the ('carried', 'carried') pair `advance` reads off that world with the absence read as an answer
        """THE READ'S FAILING INPUT (task/3056 round 2): the arm above, then
        the old history expired and pruned. The measured commit is then not
        in the repository at all, so git cannot ask about its ancestry
        (`rev-parse --verify` answers 1, `merge-base --is-ancestor` 128). A
        complete repository does not hold it, so it is not history: the close
        falls back to the head exactly as it does without the prune, and the
        reachability check keeps it. Before this the gate read the absence as
        an unreadable probe and refused the close on every fold."""
        row = self.carried()
        measured = self.close_event(row["id"])["closing_trunk_sha"]

        def rewrite_merge_prune():
            self.git("reset", "--hard", "-q", "HEAD~1")
            self.git("merge", "--no-ff", "-q", "-m",
                     "the rewritten trunk merges the lane whole", "side")
            self.prune(measured)

        was, answer, roads = self.advance(row, rewrite_merge_prune)
        self.assert_full(roads)
        self.assertEqual((was, answer), ("carried", "carried"),
                         "a close whose measured trunk was pruned from a "
                         "complete repository was not re-derived at the head")
        # THE MUST-DIFFER CONTROL ON THE SAME WORLD: read the absence as
        # unreadable again, and the same fold refuses the close.
        with mock.patch.object(dispatches, "_absent_from_a_complete_odb",
                               lambda *_a: None):
            _full, out = self.full_replay()
        self.assertIsNone(out[row["id"]].get("close_reason"),
                          "the row stayed closed without the absence being "
                          "read, so the answer above is not its doing")

    # -- the shapes the CLOSE-TIME guard keeps out of the fold entirely -----

    def test_a_SQUASH_LANDED_tip_cannot_be_CLOSED_carried_at_all(self):
        """Pinned because its ABSENCE from the set above is a finding.

        A squash destroys per-commit patch identity, so `reached-trunk` can
        never speak for the tip. `carriage_proof` in isolation narrows for
        this shape -- but the fold never meets it, because the close-time
        guard refuses to mint the close. THE GUARD RESTRICTS THE REACHABLE
        INPUT SET, and a predicate that narrows on an input its caller cannot
        supply is not a defect of the fold."""
        self.git("checkout", "-q", "side")
        self.commit("second lane commit", path="g")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        row = self.verdict_row(polarity="fix", ref=tip, lane="lane/squash")
        self.git("merge", "--squash", "-q", "side")
        self.git("commit", "-qm", "land the lane squashed")
        out, err = landreq.close(row["id"], "carried",
                                 evidence="the lander closed nothing",
                                 repo=self.repo)
        self.assertIsNone(out)
        self.assertIn("could not be matched", err)
        self.assertIn("fail-closed", err)

    def test_a_MERGE_reviewed_tip_cannot_be_CLOSED_carried_at_all(self):
        """The other absent shape, kept out by the same guard for a different
        reason: the tip is a merge, `git cherry` skips merges, so the tip is
        missing from its own listing."""
        self.git("checkout", "-q", "side")
        self.git("merge", "--no-ff", "-q", "-m", "merge trunk into the lane",
                 self.main)
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        row = self.verdict_row(polarity="fix", ref=tip, lane="lane/mergetip")
        self.git("merge", "--no-ff", "-q", "-m", "land the merge tip", "side")
        out, err = landreq.close(row["id"], "carried",
                                 evidence="the lander closed nothing",
                                 repo=self.repo)
        self.assertIsNone(out)
        self.assertIn("fail-closed", err)

    # -- the oracle must be able to DISAGREE -------------------------------

    def test_a_TRUNK_DERIVED_field_in_a_RESTORED_row_turns_the_oracle_RED(self):
        """THE CONTROL FOR EVERY ARM ABOVE. An oracle that cannot disagree is
        decoration, and every other arm in this class rests on
        `assert_equivalent` being able to go red.

        The failure it must catch is specific and is the one a
        restore-across-a-land cure invites: a restored row carrying a field
        derived from the trunk it was PROVEN at, where a full replay at the
        CURRENT trunk would derive a different value. Today that cannot
        happen, because a trunk move discards the checkpoint -- so the
        divergence is planted rather than provoked, and what is proven is the
        INSTRUMENT, not the code.

        Planted at the restore boundary: the checkpointed read returns a row
        with a stale pinned trunk, the full replay returns the real one, and
        the comparison must NOTICE. If this arm ever goes green, every
        equality above stopped meaning anything.
        """
        row = self.carried()
        self.settle()
        roads, _out = self.assert_equivalent()
        self.assert_restored(roads)
        real = dispatches.snapshot()[0][row["id"]]["closing_trunk_sha"]
        stale = "0" * len(real)
        self.assertNotEqual(real, stale)
        original = dispatches._fold_into

        def poison(acc, events, base, **kw):
            out = original(acc, events, base, **kw)
            # THE ACCUMULATOR IS THE ANSWER. `_fold_into` folds INTO `acc` and
            # returns nothing a caller reads, and on a pure restore it is
            # handed ZERO events -- the rows come off the checkpoint, not out
            # of this call. Poisoning a return value would therefore have
            # changed nothing, which is how this control first went green for
            # the wrong reason.
            #
            # ONLY THE RESTORE SIDE. `full_replay` folds too, and poisoning
            # both halves would leave them AGREEING on a wrong answer -- an
            # arm that passes while proving nothing, which is the exact
            # failure this control exists to rule out. A restore resumes at a
            # non-zero base; the reference replay starts at zero.
            if base:
                planted = acc.out.get(row["id"])
                if isinstance(planted, dict) \
                        and planted.get("closing_trunk_sha") == real:
                    planted["closing_trunk_sha"] = stale
            return out

        with mock.patch.object(dispatches, "_fold_into", poison):
            with self.assertRaises(AssertionError) as caught:
                self.assert_equivalent()
        self.assertIn("not the full replay", str(caught.exception))
        # AND THE WORLD IS UNHARMED: with the poison lifted the same oracle
        # agrees again, so what went red was the planted divergence and not
        # some damage the arm did on its way.
        self.assert_equivalent()


class AncestryGateArms(FoldCheckpointBase):
    """THE THIRD ANSWER, which is the only reason `_ancestry_authorizes`
    exists beside `_reached_by_ancestry` instead of replacing it.

    The sentence-grade probe collapses an unreadable read to False on
    purpose: it only picks a refusal's WORDING, so a failed read there costs
    a weaker sentence. This one AUTHORIZES a close, and there `unreadable`
    spelled as `False` would read as "the work is not on trunk" and un-close
    a good row every time git failed to run.
    """

    def reached_after_a_rewrite(self):
        """Trunk drops the commit a `carried()` close was measured against and
        then merges the lane whole.

        THE ONE ROAD THAT STILL REACHES THE REACHABILITY RUNG. Since task/3056
        a close is replayed against the trunk it was measured at while that is
        still history, and there it answers what it answered when minted -- so
        a plain merge of the lane no longer silences it. Only a close sent back
        to the head can meet an empty `git cherry` range, and a rewrite past the
        measured commit is what sends it there."""
        measured = self.git("rev-parse", "HEAD")
        self.git("reset", "--hard", "-q", "HEAD~1")
        self.git("merge", "--no-ff", "-q", "-m",
                 "the rewritten trunk merges the lane whole", "side")
        self.assertNotEqual(
            subprocess.run(["git", "-C", self.repo, "merge-base",
                            "--is-ancestor", measured, "HEAD"]).returncode, 0,
            "the measured trunk is still history, so the head is not the road")
        return measured

    def test_an_UNREADABLE_measured_trunk_probe_REFUSES_by_name(self):
        """task/3056, THE RULING'S SAME LAW FOR THE NEW PROBE. Whether the
        measured trunk is still history picks which trunk the close is replayed
        against, so an unreadable answer must pick neither: guessing "yes"
        keeps a close whose trunk may be gone, guessing "no" re-derives it at a
        head the question was never about."""
        from helm import rowworld
        row = self.carried()
        event = self.close_event(row["id"])
        measured = event["closing_trunk_sha"]
        before_close = [e for e in self.ledger_events()
                        if not (e.get("id") == row["id"]
                                and e.get("event") == "close")]
        pre, _verdicts, _taken = dispatches._fold(before_close)
        standing = pre[row["id"]]
        # POSITIVE CONTROL, same event, same row, working probe.
        self.assertIsNone(dispatches._close_event_error(
            event, standing, current=pre))
        real = rowworld._git

        def broken(gd, *argv):
            if argv[:2] == ("merge-base", "--is-ancestor") \
                    and argv[2] == measured:
                return 129, ""
            return real(gd, *argv)

        with mock.patch.object(rowworld, "_git", broken):
            why = dispatches._close_event_error(event, standing, current=pre)
        self.assertTrue(why, "an unreadable ancestry probe picked a trunk for "
                             "the close instead of refusing it")
        self.assertIn(measured[:12], why)
        self.assertIn("is no longer readable", why)
        self.assertIn("not a finding", why)
        self.assertNotIn("no longer affirms", why)
        # THE BREAK LIFTED, the same call affirms again.
        self.assertIsNone(dispatches._close_event_error(
            event, standing, current=pre))

    def test_an_ABSENT_object_is_an_answer_only_in_a_COMPLETE_repository(self):  # noqa: VACUOUS_ASSERTION — every assertIsNone is bracketed on the SAME call by assertIs(True) for the pruned commit in the complete repository, re-asserted after each break is lifted
        """`_absent_from_a_complete_odb`, one break at a time. A commit the
        rewrite and the prune removed is absent from a complete repository
        (True). Each way a repository is incomplete -- a partial clone, a
        promisor remote, a shallow boundary -- means the object may be history
        git never fetched, and then the answer is None. An object the
        repository holds is never absent."""
        self.carried()
        measured = self.reached_after_a_rewrite()
        self.prune(measured)
        gitdir = self.gitdir()
        absent = dispatches._absent_from_a_complete_odb
        self.assertIs(absent(gitdir, measured), True,
                      "the control: a pruned commit in a complete repository")
        self.assertIsNone(absent(gitdir, self.git("rev-parse", "HEAD")),
                          "an object the repository holds read as absent")
        for key, value in (("extensions.partialClone", "origin"),
                           ("remote.origin.promisor", "true")):
            with self.subTest(key=key):
                self.git("config", key, value)
                self.assertIsNone(absent(gitdir, measured),
                                  "%s is set, and a missing object still read "
                                  "as absent" % key)
                self.git("config", "--unset", key)
                self.assertIs(absent(gitdir, measured), True,
                              "the break lifted, the same call answers again")
        shallow = os.path.join(gitdir, "shallow")
        with open(shallow, "w", encoding="utf-8") as f:
            f.write(self.git("rev-parse", "HEAD~1") + "\n")
        try:
            self.assertEqual(self.git("rev-parse", "--is-shallow-repository"),
                             "true", "the fixture's boundary did not take")
            self.assertIsNone(absent(gitdir, measured),
                              "a shallow repository read a missing object as "
                              "absent")
        finally:
            os.unlink(shallow)
        self.assertIs(absent(gitdir, measured), True)

    def test_a_PRUNED_measured_trunk_in_an_INCOMPLETE_repository_REFUSES_by_name(self):  # noqa: VACUOUS_ASSERTION — the two assertIsNone calls are the complete-repository controls on the same event and row; the refusal between them is asserted by value (the measured id, 'is no longer readable', 'not a finding') on the same _close_event_error call
        """The pruned world, where a complete repository re-derives the close
        at the head and the reachability check keeps it. Mark a promisor
        remote and the same close is refused: the measured commit may be
        history this repository never fetched. The sentence says the answer
        is no longer readable -- git DID run, so it must not say the probe
        did not."""
        row = self.carried()
        event = self.close_event(row["id"])
        measured = event["closing_trunk_sha"]
        before_close = [e for e in self.ledger_events()
                        if not (e.get("id") == row["id"]
                                and e.get("event") == "close")]
        pre, _verdicts, _taken = dispatches._fold(before_close)
        standing = pre[row["id"]]
        self.assertEqual(self.reached_after_a_rewrite(), measured)
        self.prune(measured)
        # POSITIVE CONTROL, same event, same row, complete repository.
        self.assertIsNone(dispatches._close_event_error(
            event, standing, current=pre))
        self.git("config", "remote.origin.promisor", "true")
        try:
            why = dispatches._close_event_error(event, standing, current=pre)
        finally:
            self.git("config", "--unset", "remote.origin.promisor")
        self.assertTrue(why, "a promisor repository read a missing measured "
                             "trunk as not history and picked the head")
        self.assertIn(measured[:12], why)
        self.assertIn("is no longer readable", why)
        self.assertIn("not a finding", why)
        self.assertNotIn("did not run", why)
        # THE BREAK LIFTED, the same call affirms again.
        self.assertIsNone(dispatches._close_event_error(
            event, standing, current=pre))

    def test_ONE_ancestry_probe_per_distinct_measured_trunk_per_fold(self):  # noqa: VACUOUS_ASSERTION — the inequality is a fixture must-hit (two distinct measured trunks) and `unavailable` shares its producer with `rows`, whose three close reasons are asserted by value; the probe list is asserted EQUAL to a two-element set
        """task/3056: the live ledger's 509 carried closes name 8 measured
        trunks, so a fold asks 8 ancestry questions, not 509. Three closes on
        two measured trunks here: two probes, one per trunk, in one cold fold."""
        from helm import rowworld
        first = self.verdict_row(polarity="fix", lane="lane/probe-a")
        second = self.verdict_row(polarity="fix", lane="lane/probe-b")
        self.git("cherry-pick", "--no-commit", self.side)
        self.git("commit", "-qm", "carry the side work")
        for row in (first, second):
            out, err = landreq.close(row["id"], "carried",
                                     evidence="the lander closed nothing",
                                     repo=self.repo)
            self.assertIsNone(err)
            self.assertEqual(out["close_reason"], "carried")
        self.commit("trunk moves on", path="elsewhere")
        third = self.verdict_row(polarity="fix", lane="lane/probe-c")
        out, err = landreq.close(third["id"], "carried",
                                 evidence="the lander closed nothing",
                                 repo=self.repo)
        self.assertIsNone(err)
        self.assertEqual(out["close_reason"], "carried")
        measured = [self.close_event(row["id"])["closing_trunk_sha"]
                    for row in (first, second, third)]
        self.assertEqual(measured[0], measured[1])
        self.assertNotEqual(measured[0], measured[2])
        asked, real = [], rowworld._git

        def spy(gd, *argv):
            if argv[:2] == ("merge-base", "--is-ancestor") \
                    and argv[2] in measured:
                asked.append(argv[2])
            return real(gd, *argv)

        shutil.rmtree(self.store_dir(), ignore_errors=True)
        with mock.patch.object(rowworld, "_git", spy):
            rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertEqual([rows[r["id"]].get("close_reason")
                          for r in (first, second, third)],
                         ["carried"] * 3)
        self.assertEqual(sorted(asked), sorted({measured[0], measured[2]}),
                         "one cold fold must ask each measured trunk's "
                         "ancestry exactly once")

    def test_the_gate_answers_TRUE_FALSE_and_NONE_for_three_states(self):  # noqa: VACUOUS_ASSERTION — the two assertIsNone calls sit between unconditional positives on the SAME call: assertIs(True) for a tip on trunk and assertIs(False) for one that is not, plus a closing control that re-affirms True once the broken probe is lifted
        """One arm, three states, because a tri-state whose third value is
        never exercised is a two-state with a comment."""
        from helm import rowworld
        gitdir = self.gitdir()
        on_trunk = self.git("rev-parse", "HEAD")
        self.assertIs(dispatches._ancestry_authorizes(gitdir, on_trunk,
                                                      self.main), True)
        self.assertIs(dispatches._ancestry_authorizes(gitdir, self.side,
                                                      self.main), False)
        # AN OBJECT THIS REPOSITORY DOES NOT HAVE is the ordinary unreadable
        # case, and it must NOT come back False.
        self.assertIsNone(dispatches._ancestry_authorizes(gitdir, "0" * 40,
                                                          self.main))
        # AND A PROBE THAT FAILS FOR ITS OWN REASONS takes the same value.
        # git answers 0 for yes and 1 for no; any other code is the probe
        # breaking, never an answer about history.
        real = rowworld._git

        def broken(gd, *argv):
            if argv[:2] == ("merge-base", "--is-ancestor"):
                return 129, ""
            return real(gd, *argv)

        with mock.patch.object(rowworld, "_git", broken):
            self.assertIsNone(
                dispatches._ancestry_authorizes(gitdir, on_trunk, self.main))
        # THE CONTROL: with the break lifted the same call answers again, so
        # the None above was the break and not a world this arm damaged.
        self.assertIs(dispatches._ancestry_authorizes(gitdir, on_trunk,
                                                      self.main), True)

    def test_a_carried_tip_that_is_NOT_the_rows_work_tip_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is the positive control on the same event and row; the assertTrue and assertIn below are the refusal
        """The ancestry gate must not accept ANY commit trunk holds.

        The short-circuit answers "has the pinned tip become trunk history",
        so an event naming some unrelated commit trunk already contains would
        pass that question while proving nothing about this row. The tip the
        gate asks about has to be the one `carriage_proof` would measure.
        """
        row = self.carried()
        self.reached_after_a_rewrite()
        before_close = [e for e in self.ledger_events()
                        if not (e.get("id") == row["id"]
                                and e.get("event") == "close")]
        pre, _verdicts, _taken = dispatches._fold(before_close)
        standing = pre[row["id"]]
        event = self.close_event(row["id"])
        # POSITIVE CONTROL: the row's own tip, now trunk history, is affirmed.
        self.assertIsNone(dispatches._close_event_error(
            event, standing, current=pre))
        # A commit trunk plainly holds, which is NOT this row's work tip.
        stranger = self.git("rev-parse", self.main)
        self.assertNotEqual(stranger, event["carried_tip"],
                            "the fixture picked the row's own tip, so the "
                            "refusal below would not be the thing under test")
        forged = dict(event, carried_tip=stranger)
        why = dispatches._close_event_error(forged, standing, current=pre)
        self.assertTrue(why, "an event naming a stranger commit trunk holds "
                             "bound a close nothing proved")
        self.assertIn("not the work tip this row binds", why)

    def test_an_UNREADABLE_probe_REFUSES_by_name_and_never_un_closes(self):
        """THE BRANCH THE RULING ASKED FOR IN AS MANY WORDS: a refusal that
        names itself, never a guess.

        The dangerous shape is not the refusal -- it is the refusal being
        indistinguishable from "the work is absent". A row whose tip HAS
        become trunk history is affirmed by the gate; with the probe broken
        the same row must be REFUSED, and the refusal must say the probe did
        not run rather than repeat the older sentence about trunk no longer
        carrying the work.
        """
        from helm import rowworld
        row = self.carried()
        self.reached_after_a_rewrite()
        rows, _ = dispatches.snapshot()
        self.assertEqual(rows[row["id"]].get("close_reason"), "carried",
                         "the tip never became trunk history, so the probe "
                         "below would not be the thing under test")
        # THE VALIDATOR IS ASKED DIRECTLY, NOT THROUGH A READ. A second
        # `snapshot()` would be served from the checkpoint the first one just
        # wrote at this very trunk, so the broken probe would never run and
        # the arm would pass having measured a cache. Measured: that is
        # exactly how this arm first went green for the wrong reason.
        event = self.close_event(row["id"])
        # THE STANDING ROW IS FOLDED, NOT HAND-BUILT. `_close_event_error`
        # checks that the close's sequence follows the row it is judged
        # against, so a projection with the close already applied refuses on
        # the sequence and never reaches the rung under test -- measured.
        # Re-folding every event EXCEPT this close is the row as the writer
        # saw it.
        before_close = [e for e in self.ledger_events()
                        if not (e.get("id") == row["id"]
                                and e.get("event") == "close")]
        pre, _verdicts, _taken = dispatches._fold(before_close)
        standing = pre[row["id"]]
        # POSITIVE CONTROL, same event, same row, working probe: the close is
        # AFFIRMED because its tip is now trunk history.
        self.assertIsNone(dispatches._close_event_error(
            event, standing, current=pre))
        real = rowworld._git

        # ONLY THE TIP'S PROBE BREAKS. The measured trunk's ancestry is asked
        # through the same verb first (task/3056) and must still answer, or the
        # refusal below would be that probe's and not this rung's.
        def broken(gd, *argv):
            if argv[:2] == ("merge-base", "--is-ancestor") \
                    and argv[2] == event["carried_tip"]:
                return 129, ""
            return real(gd, *argv)

        with mock.patch.object(rowworld, "_git", broken):
            why = dispatches._close_event_error(event, standing, current=pre)
        self.assertTrue(why, "an unreadable probe AFFIRMED the close, so the "
                             "gate authorized on a question it never asked")
        self.assertIn("has since become history", why,
                      "the refusal is not the reachability rung's")
        self.assertIn("is no longer readable", why)
        self.assertIn("not a finding", why)
        # AND IT IS NOT THE OLDER SENTENCE. The whole requirement is that an
        # unreadable probe is distinguishable from a measured absence; if
        # this refusal said "trunk no longer affirms" the operator could not
        # tell the two apart, which is the defect and not the cure.
        self.assertNotIn("no longer affirms", why)
        # THE BREAK LIFTED, the same call affirms again: what went red was
        # the probe and not damage this arm did.
        self.assertIsNone(dispatches._close_event_error(
            event, standing, current=pre))


class TheStoreIsAddressedPerLedger(FoldCheckpointBase):
    """THE CHECKPOINT STORE IS ADDRESSED PER LEDGER, NOT PER CODE VERSION.

    Every durable ledger on a box shares one directory. An address built from
    the code version and the lens term alone gives two ledgers ONE file, and
    the failure is silent in both directions: each read finds a header whose
    prefix digest belongs to the other ledger, calls it stale, and overwrites
    it, so neither checkpoint ever holds and both readers sit at full-replay
    cost with nothing red. Only a SECOND customer can exhibit that, which is
    why these arms run two ledgers in one directory rather than one.
    """

    STATE = {"out": {}, "verdicts": {}, "taken": {},
             "actors": {"validated": {}, "unresolved": {}}}

    def sibling(self):
        """A second ledger beside the dispatch one, in the SAME directory —
        which is the whole subject. A path in another directory would separate
        the two stores for a reason these arms are not about."""
        return os.path.join(os.path.dirname(dispatches.ledger_path()),
                            "gate-receipts.jsonl")

    def session(self, ledger, data):
        s = foldckpt.begin(ledger, dispatches.epoch_path(), data)
        self.assertIsNotNone(
            s, "begin() keyed nothing for %s, so this arm would assert about "
               "an absent store rather than a per-ledger one" % ledger)
        return s

    def save(self, ledger, data):
        s = self.session(ledger, data)
        self.assertTrue(
            s.save(dict(self.STATE), 1, s.end, None, foldckpt.Recorder()),
            "the save refused, so a later restore proves nothing")
        self.assertTrue(os.path.isfile(s.store()),
                        "the save reported success and wrote no file")
        return s

    def test_two_ledgers_in_one_directory_do_not_evict_each_other(self):  # noqa: VACUOUS_ASSERTION — the negative limbs are two `assertNotEqual`s on B's address, and their matching positive control is the same-ledger EQUALITY on A asserted unconditionally two lines above them (a random `store()` would satisfy the inequalities and fail that). The control cannot share B's roots: being a different ledger is the property under test.
        a, b = dispatches.ledger_path(), self.sibling()
        self.assertEqual(os.path.dirname(a), os.path.dirname(b))
        data_a, data_b = b'{"id":"a"}\n', b'{"id":"b"}\n{"id":"b2"}\n'

        self.save(a, data_a)
        # POSITIVE CONTROL, BEFORE THE SUBJECT AND ON THE SAME OBSERVABLE: A's
        # checkpoint restores while it is the only one. Without it the arm
        # below could pass on a store that never held anything for anyone.
        self.assertIsNotNone(
            self.session(a, data_a).restore(),
            "A did not restore its OWN fresh checkpoint, so this arm cannot "
            "say anything about what B does to it")

        self.save(b, data_b)

        self.assertIsNotNone(
            self.session(a, data_a).restore(),
            "writing B's checkpoint destroyed A's: the store is addressed by "
            "code alone and two ledgers share one file")
        self.assertIsNotNone(
            self.session(b, data_b).restore(),
            "B could not restore its own checkpoint beside A's")
        # AND THE ADDRESS IS A FUNCTION OF THE LEDGER, asserted positively
        # before it is asserted different. A `store()` that returned a fresh
        # random path every call would satisfy every inequality below while
        # being the worst possible answer, so "same ledger, same address" is
        # the control that makes "different ledger, different address" mean
        # anything at all.
        self.assertEqual(foldckpt.store_dir(a), foldckpt.store_dir(a))
        self.assertEqual(self.session(a, data_a).store(),
                         self.session(a, data_b).store())
        self.assertNotEqual(foldckpt.store_dir(a), foldckpt.store_dir(b))
        self.assertNotEqual(self.session(a, data_a).store(),
                            self.session(b, data_b).store())

    def test_one_ledgers_own_second_save_still_replaces_its_file(self):
        """THE CONTROL ON THE AXIS ITSELF: per-ledger must not have become
        per-anything-else. One ledger keeps ONE file per code version, so a
        second save under the same code replaces it rather than accumulating,
        and `MAX_FILES` still bounds what one ledger leaves behind."""
        a = dispatches.ledger_path()
        first = self.save(a, b'{"id":"a"}\n')
        second = self.save(a, b'{"id":"a"}\n{"id":"a2"}\n')
        self.assertEqual(first.store(), second.store())
        self.assertTrue(os.path.isfile(second.store()))
        self.assertEqual(
            sorted(os.listdir(foldckpt.store_dir(a))),
            [os.path.basename(first.store())],
            "one ledger under one code version left more than one checkpoint")

    def test_a_checkpoint_naming_another_ledger_is_refused(self):  # noqa: VACUOUS_ASSERTION — the single absence is B's refusal to restore, and its positive control is unconditional on the SAME file: the bytes at B's address exist, are byte-equal to what was written, and restore under A. A control on B restoring is impossible, because B restoring is the defect.
        """The header carries the ledger's own path, so a store that was
        copied — or whose 16-hex address collided — cannot serve one fold's
        state to another fold's reader. A prefix digest is no defence here:
        two ledgers sharing a prefix would pass it."""
        a, b = dispatches.ledger_path(), self.sibling()
        data = b'{"id":"a"}\n'
        self.save(a, data)
        with open(self.session(a, data).store(), "rb") as f:
            blob = f.read()
        # A's checkpoint bytes, verbatim, at B's address.
        target = self.session(b, data).store()
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        with open(target, "wb") as f:
            f.write(blob)
        # POSITIVE CONTROL ON THE OBSERVABLE THE REFUSAL IS ABOUT: B's address
        # holds a real, complete, live checkpoint. The refusal below is then
        # necessarily about WHOSE ledger the header names, and not about a
        # missing file or a blob this harness mangled on its way through.
        self.assertTrue(os.path.isfile(target))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), blob)
        self.assertIsNotNone(
            self.session(a, data).restore(),
            "these same bytes do not restore under A either, so the refusal "
            "below would measure a broken payload and not the ledger key")

        self.assertIsNone(
            self.session(b, data).restore(),
            "B restored a checkpoint minted for A: nothing in the key names "
            "which ledger the fold was over")


class ABudgetedReaderRebuildsAMissingCheckpoint(FoldCheckpointBase):
    """task/2949: the Stop guard read every obligation as UNKNOWN because it
    could never rebuild a checkpoint that a deploy had invalidated.

    THE STOP GUARD NO LONGER READS THE LEDGER — the `helm web` resident folds
    it off every hook path (helm/stopfacts_resident.py) — but the property this
    class pins is the CHECKPOINT'S: a read under a cooperative deadline that
    cannot finish banks its progress, and the next read resumes from it. So
    the arms read under the same 7.5s slice the guard's span had, as any
    budgeted reader of the ledger does.

    MEASURED ON THE LIVE LEDGER (18,924 events, 13.6 MB), through the exact
    function the guard hands to its `dispatch-ledger` span: a checkpoint hit
    is 0.11s median of five, a miss is a full replay at 25.96s median of five
    (22.9-35.0s). The rung's slice is the 7.5s between the ladder's start and
    its 10.0s successor reserve, so a miss could never finish, and a read
    that does not finish saved nothing. The checkpoint key includes the code,
    so every land into the shared checkout invalidated it, and every stop
    after that read UNKNOWN until an unbudgeted reader (`helm dispatch list`)
    paid the whole replay.

    THE WORLD. One real dispatch, cloned to thousands of rows, raw-appended
    so that no writer advances the checkpoint, and the store removed, which is
    the state after a deploy. Each fold of a row advances a FAKE monotonic
    clock by ROW_COST_S. That stands in for the git work the live fold does per
    row, so the arms can run the guard's real budget constants
    (BUDGET_S, RESERVE_S, ADMISSION_COST_S, SAVE_RESERVE_S) at no real cost.
    ROWS * ROW_COST_S is 24s, which is longer than the whole 17.5s ladder, as
    the live replay is."""

    ROWS = 4000
    ROW_COST_S = 0.006
    # Measured on the live ladder: the rung begins about 0.16s in.
    BEGAN_S = 0.16
    # The slice the ladder's span read under: its 17.5s budget less the 10.0s
    # its successors were owed. Kept as this arm's budget because the property
    # is the checkpoint's, and a number the checkpoint was measured against.
    SLICE_S = 7.5
    SLICE_SPENT = "the read's slice was spent; coverage is UNKNOWN"

    def world(self, rows=ROWS):
        seed = self.dispatch(ref=self.a, lane="lane/ckpt-guard",
                             kind="review")
        event = [e for e in self.ledger_events()
                 if e.get("event") == "dispatch" and e.get("id") == seed["id"]]
        self.assertEqual(len(event), 1)
        lines = []
        for i in range(rows):
            clone = dict(event[0])
            clone["id"] = "c0ffee%018x" % i      # a real id: 8-64 hex
            clone["chain_root"] = clone["id"]
            clone["operation_key"] = "ckpt-guard-%06d" % i
            lines.append(json.dumps(clone, sort_keys=True) + "\n")
        with open(dispatches.ledger_path(), "a") as f:
            f.write("".join(lines))
        shutil.rmtree(self.store_dir(), ignore_errors=True)
        return seed

    def guard_read(self, cost=None):
        """One budgeted read: `dispatches.snapshot()` inside a SLICE_S
        cooperative deadline, answering ({}, SLICE_SPENT) when the slice runs
        out. The clock moves only when a row is folded. -> (pair, read) where
        `read.expired` says whether the slice ran out."""
        import types
        cost = self.ROW_COST_S if cost is None else cost
        clock = [1000.0]
        real = dispatches._new_state

        def slow(row):
            clock[0] += cost
            return real(row)

        read = types.SimpleNamespace(expired=False)
        with mock.patch("time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(dispatches, "_new_state", slow):
            clock[0] += self.BEGAN_S
            with projscope.scope(deadline=clock[0] + self.SLICE_S):
                try:
                    snap = dispatches.snapshot()
                except projscope.Expired:
                    snap = ({}, self.SLICE_SPENT)
                    read.expired = True
        return snap, read

    def banked(self):
        if not os.path.isdir(self.store_dir()) \
                or not os.listdir(self.store_dir()):
            return None
        return self.header()["ledger"]["events"]

    def test_a_realistic_ledger_is_read_by_the_guard_with_real_obligations(self):  # noqa: VACUOUS_ASSERTION — the None answer is bracketed by the first stop's named UNKNOWN, the byte-for-byte equality with a full replay and the 4,001 open rows counted off it
        seed = self.world()
        total = len(self.ledger_events())
        answers, banked = [], []
        for _ in range(12):
            (rows, why), _budget = self.guard_read()
            answers.append(why)
            banked.append(self.banked())
            if why is None:
                break
        # THE FIRST READ IS STILL HONEST: it could not finish, so it says so.
        self.assertEqual(answers[0], self.SLICE_SPENT)
        self.assertIsNone(
            answers[-1], "the guard never finished the read in %d stops; the "
            "checkpoint it banked moved %r" % (len(answers), banked))
        # EACH UNFINISHED STOP BANKED PROGRESS; NONE WENT BACKWARDS.
        partial = banked[:-1]
        self.assertTrue(partial and all(partial))
        self.assertEqual(partial, sorted(set(partial)))
        self.assertLess(partial[-1], total)
        self.assertEqual(banked[-1], total)
        # REAL OBLIGATIONS, EXACTLY THE FULL REPLAY'S.
        _want, out = self.full_replay()
        self.assertEqual(repr(rows), repr(out))
        owed = [r for r in rows.values()
                if r.get("recipient") == seed["recipient"]
                and r.get("status") == "open"]
        self.assertEqual(len(owed), self.ROWS + 1)
        self.assertIn(seed["id"], rows)
        # AND THE NEXT STOP RESTORES AND FINISHES AT ONCE.
        (again, why), read = self.guard_read()
        self.assertIsNone(why)
        self.assertEqual(repr(again), repr(out))
        self.assertFalse(read.expired)

    def test_a_read_that_cannot_finish_still_reports_unknown_with_its_reason(self):
        self.world()
        total = len(self.ledger_events())
        (rows, why), read = self.guard_read()
        self.assertEqual((rows, why), ({}, self.SLICE_SPENT))
        self.assertTrue(read.expired)
        # PROGRESS WAS BANKED, AND A BANKED PREFIX IS NOT AN ANSWER.
        self.assertTrue(0 < self.banked() < total, self.banked())
        # ONE ROW COSTS MORE THAN THE WHOLE SLICE: the check is cooperative,
        # so the first row finishes late and is banked, and the answer is
        # still the named UNKNOWN.
        shutil.rmtree(self.store_dir())
        (rows, why), read = self.guard_read(cost=8.0)
        self.assertEqual((rows, why), ({}, self.SLICE_SPENT))
        self.assertTrue(read.expired)
        self.assertEqual(self.banked(), 1)

    def test_a_banked_prefix_resumes_exactly_and_a_stale_one_is_replayed(self):  # noqa: VACUOUS_ASSERTION — the positives are the byte-for-byte equality with a full replay on every read and the road each read took
        self.world()
        total = len(self.ledger_events())
        (_rows, why), _read = self.guard_read()
        self.assertEqual(why, self.SLICE_SPENT)
        k = self.banked()
        self.assertTrue(0 < k < total, k)
        # RESUMED: an unbudgeted read restores at K and answers the full
        # replay's answer byte for byte.
        got, roads = self.read()
        want, _out = self.full_replay()
        self.assertEqual(got, want)
        self.assertEqual(roads[0], (k, total - k))
        # STALE: the same bank, over a ledger whose prefix was rewritten in
        # place, is refused and the read replays everything.
        shutil.rmtree(self.store_dir())
        self.guard_read()
        self.assertTrue(0 < self.banked() < total)
        path = dispatches.ledger_path()
        with open(path, "rb") as f:
            data = f.read()
        at = data.index(b"ckpt-guard-000001")
        with open(path, "wb") as f:
            f.write(data[:at] + b"ckpt-guard-00000X" + data[at + 17:])
        got, roads = self.read()
        want, _out = self.full_replay()
        self.assertEqual(got, want)
        self.assert_full(roads)
        # MISSING: no store at all is a full replay too.
        shutil.rmtree(self.store_dir())
        got, roads = self.read()
        self.assertEqual(got, want)
        self.assert_full(roads)


class TheCheckpointOnlyGrows(FoldCheckpointBase):
    """A SAVE NEVER SHRINKS A GOOD CHECKPOINT (task/2949, round two).

    A budgeted read now saves a PREFIX. If that save is delayed, and another
    reader saves the whole ledger in between, an unconditional replace put the
    shorter prefix back over the whole fold. Then the next stop had to fold
    thousands of events again, and concurrent stops could keep the ledger
    UNKNOWN. So `Session.save`, which is the one door every writer uses,
    replaces a checkpoint only with a STRICTLY LONGER one, or when the checkpoint
    there is invalid or stale. The second arm is the other pole: the rule must
    never keep a longer checkpoint that no reader can use."""

    ROWS = 600
    world = ABudgetedReaderRebuildsAMissingCheckpoint.world
    banked = ABudgetedReaderRebuildsAMissingCheckpoint.banked

    def prefix_save(self, k):
        """(save, k): a save of the fold of the first `k` events, keyed on the
        ledger bytes as they are NOW, to be called later. That is a stop that
        folded, then stalled before it wrote."""
        path = dispatches.ledger_path()
        data, why = eventledger.read_bytes(path)
        self.assertIsNone(why)
        session = foldckpt.begin(path, dispatches.epoch_path(), data)
        self.assertIsNotNone(session)
        events, why = eventledger.checked_rows(data, strict=True)
        self.assertIsNone(why)
        self.assertLessEqual(k, len(events))
        acc = dispatches._Acc(actors={"validated": {}, "unresolved": {}})
        with foldckpt.recording() as rec:
            dispatches._fold_into(acc, events[:k], 0)
        end = 0
        for _ in range(k):
            end = data.index(b"\n", end) + 1
        return lambda: session.save(acc.state(), k, end, None, rec)

    def test_a_delayed_shorter_save_leaves_the_whole_checkpoint(self):
        self.world(rows=self.ROWS)
        total = len(self.ledger_events())
        late = self.prefix_save(total // 2)
        # B: an unbudgeted reader saves the whole ledger.
        self.read()
        self.assertEqual(self.banked(), total)
        # A: the stalled stop now writes its shorter prefix.
        late()
        self.assertEqual(self.banked(), total,
                         "a shorter prefix replaced the whole checkpoint")
        got, roads = self.read()
        want, _out = self.full_replay()
        self.assertEqual(got, want)
        self.assert_restored(roads, tail=0)

    def test_a_stale_longer_checkpoint_is_replaced_by_a_valid_shorter_one(self):
        self.world(rows=self.ROWS)
        total = len(self.ledger_events())
        self.read()
        self.assertEqual(self.banked(), total)
        # The whole checkpoint is now stale: its ledger prefix is rewritten in
        # place, with the same length.
        path = dispatches.ledger_path()
        with open(path, "rb") as f:
            data = f.read()
        at = data.index(b"ckpt-guard-000001")
        with open(path, "wb") as f:
            f.write(data[:at] + b"ckpt-guard-00000X" + data[at + 17:])
        k = total // 2
        self.prefix_save(k)()
        self.assertEqual(self.banked(), k,
                         "a stale checkpoint was kept because it was longer")
        got, roads = self.read()
        want, _out = self.full_replay()
        self.assertEqual(got, want)
        self.assertEqual(roads[0], (k, total - k))


# A BOUND ON EVERY WAIT, NEVER A SLEEP. Each wait returns the moment its event
# is set; the bound only stops a broken arm from hanging the suite.
WAIT_S = 60


class TwoWritersContendForOneLock(FoldCheckpointBase):
    """THE GROW-ONLY RULE HOLDS UNDER REAL CONCURRENCY (task/2949, round three).

    The arms in TheCheckpointOnlyGrows run their writers one after the other
    in one thread, so they prove the RULE and not the LOCK. Here two writers
    run on two threads. Each save opens the lock file itself, and a flock
    belongs to an open file description, so the two conflict exactly as two
    processes do. Events, not sleeps, put one writer INSIDE its write, holding
    the lock, while the other tries to save. The arms then read, per thread,
    every refused lock attempt and the entry and exit of every write."""

    ROWS = 600
    world = ABudgetedReaderRebuildsAMissingCheckpoint.world
    banked = ABudgetedReaderRebuildsAMissingCheckpoint.banked
    prefix_save = TheCheckpointOnlyGrows.prefix_save

    def instrument(self, holder, waiter):
        """Wrap the real flock and the real write. `holder` pauses inside its
        write until released; `waiter` reports its first refused lock."""
        self.log, mu = [], threading.Lock()
        self.entered, self.release = threading.Event(), threading.Event()
        self.contended, self.moved = threading.Event(), threading.Event()
        self.threads = []
        real_flock, real_write = fcntl.flock, pk.atomic_write

        def flock(fd, op):
            try:
                return real_flock(fd, op)
            except BlockingIOError:
                if threading.current_thread().name == waiter:
                    self.contended.set()
                    self.moved.set()
                raise

        def write(path, data, **kw):
            name = threading.current_thread().name
            if name not in (holder, waiter):
                return real_write(path, data, **kw)
            with mu:
                self.log.append((name, "enter"))
            if name == holder:
                self.entered.set()
                self.release.wait(WAIT_S)
            try:
                return real_write(path, data, **kw)
            finally:
                with mu:
                    self.log.append((name, "exit"))

        for patch in (mock.patch.object(fcntl, "flock", flock),
                      mock.patch.object(pk, "atomic_write", write)):
            patch.start()
            self.addCleanup(patch.stop)
        # REGISTERED LAST, SO IT RUNS FIRST: every writer is released and
        # joined before the patches come off and before the arm returns, on
        # the failing path too.
        self.addCleanup(self.reap)

    def reap(self):
        """Release and join every writer. A writer still alive FAILS the arm:
        a thread that outlives its test writes into whatever runs next."""
        self.release.set()
        for t in self.threads:
            t.join(WAIT_S)
        alive = [t.name for t in self.threads if t.is_alive()]
        self.assertEqual(alive, [], "a writer outlived its arm")

    def start(self, name, save, results, done=None):
        def body():
            try:
                results[name] = save()
            finally:
                if done is not None:
                    done.set()
                    self.moved.set()
        # NOT A DAEMON: a daemon is killed silently at exit instead of being
        # waited for, which is how a stray one goes unnoticed.
        t = threading.Thread(target=body, name=name, daemon=False)
        self.threads.append(t)
        t.start()
        return t

    def race(self, holder_save, waiter_save):
        """Run `holder` into its write, then `waiter` against the held lock.
        -> (results by name, the waiter's done event, before release)."""
        results, done = {}, threading.Event()
        self.instrument("holder", "waiter")
        a = self.start("holder", holder_save, results)
        self.assertTrue(self.entered.wait(WAIT_S),
                        "the holder never reached its write")
        b = self.start("waiter", waiter_save, results, done)
        self.assertTrue(self.moved.wait(WAIT_S))
        waited = (self.contended.is_set(), done.is_set())
        self.reap()
        return results, waited

    def assert_whole(self, total):
        self.assertEqual(self.banked(), total)
        got, roads = self.read()
        want, _out = self.full_replay()
        self.assertEqual(got, want)
        self.assert_restored(roads, tail=0)

    def test_a_longer_writer_waits_for_a_shorter_one_then_replaces_it(self):
        self.world(rows=self.ROWS)
        total = len(self.ledger_events())
        short, whole = self.prefix_save(total // 2), self.prefix_save(total)
        results, waited = self.race(short, whole)
        self.assertEqual(waited, (True, False),
                         "the whole save did not wait on the held lock")
        self.assertEqual(results, {"holder": True, "waiter": True})
        self.assertEqual(self.log, [("holder", "enter"), ("holder", "exit"),
                                    ("waiter", "enter"), ("waiter", "exit")],
                         "the two writes interleaved")
        self.assert_whole(total)

    def test_a_shorter_writer_that_gets_the_lock_second_writes_nothing(self):
        self.world(rows=self.ROWS)
        total = len(self.ledger_events())
        whole, short = self.prefix_save(total), self.prefix_save(total // 2)
        results, waited = self.race(whole, short)
        self.assertEqual(waited, (True, False),
                         "the shorter save did not wait on the held lock")
        self.assertEqual(results, {"holder": True, "waiter": False})
        self.assertEqual(self.log, [("holder", "enter"), ("holder", "exit")],
                         "the shorter writer wrote")
        self.assert_whole(total)

    def test_a_budgeted_save_whose_deadline_passes_on_the_lock_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the refusal is bracketed by the observed contention and by the CONTROL: the same save writes the whole checkpoint once the lock is free
        self.world(rows=self.ROWS)
        total = len(self.ledger_events())
        k = total // 2
        self.assertTrue(self.prefix_save(k)())
        self.assertEqual(self.banked(), k)
        path = self.store()
        with open(path, "rb") as f:
            before = f.read()
        whole = self.prefix_save(total)
        # ANOTHER WRITER HOLDS THE LOCK, on its own open file description.
        fd = os.open(foldckpt.save_lock_path(dispatches.ledger_path()),
                     os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        closed = []

        def close():
            if not closed:
                closed.append(fd)
                os.close(fd)
        self.addCleanup(close)
        fcntl.flock(fd, fcntl.LOCK_EX)
        contended = threading.Event()
        real_flock, real_remaining = fcntl.flock, projscope.remaining

        def flock(f, op):
            try:
                return real_flock(f, op)
            except BlockingIOError:
                contended.set()
                raise

        # THE DEADLINE PASSES WHILE THE LOCK IS HELD: the budget reads as it
        # really is until the first refused attempt, and spent after it.
        def remaining():
            return 0.0 if contended.is_set() else real_remaining()

        with mock.patch.object(fcntl, "flock", flock), \
                mock.patch.object(projscope, "remaining", remaining), \
                projscope.scope(deadline=time.monotonic() + 600):
            wrote = whole()
        self.assertTrue(contended.is_set(), "the save never met the lock")
        self.assertIs(wrote, False)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before,
                             "a save past its deadline changed the checkpoint")
        # THE CONTROL: with the lock free, the same save writes.
        close()
        self.assertTrue(whole())
        self.assertEqual(self.banked(), total)


class TheScopeDoorsRecordEveryQuestion(FoldCheckpointBase):
    """task/3056: a memo hit and a batched answer are RECORDED like a spawn.

    The fold checkpoint keys on every git question the fold consumed, seen at
    `vcs.observed`. This lane answers three kinds of question without their
    own process — a `merge-tree` or `diff --raw` already asked in the scope,
    and an existence or resolution question the scope's one `cat-file
    --batch-check` resolves — so the arm below asks the carried proof the way
    a board read does, a plain fold and then a lensed one IN ONE SCOPE, each
    under its own recorder, and holds the recorded questions and the plan
    built from them against the same two folds with both doors shut.

    THE FIXTURE PAYS A MERGE-TREE. `landed_then_trunk_edits(False)` is the one
    shape here where the cheap postimage witness refuses and the replay
    affirms, so the proof reaches every door this lane touches: `cat-file -e`,
    `rev-parse <id>^{tree}`, `diff --raw` and `merge-tree --write-tree`."""

    def rows(self):
        base, tip = self.landed_then_trunk_edits(False)
        return {"b": {"id": "b", "kind": "build", "tip": base},
                "r": {"id": "r", "kind": "review", "reviewed_tip": tip,
                      "chain_root": "b"}}

    def board(self, rows):
        """(answers, recorders, merge-tree processes) for the carried proof
        asked by TWO recorded folds in ONE scope."""
        answers, recs, merges, real = [], [], [], subprocess.Popen

        def spy(argv, *a, **kw):
            if "merge-tree" in [str(x) for x in argv]:
                merges.append(argv)
            return real(argv, *a, **kw)

        with mock.patch.object(vcs.subprocess, "Popen", side_effect=spy), \
                projscope.scope():
            for _fold in ("plain", "lensed"):
                with foldckpt.recording() as rec:
                    answers.append(dispatches.carriage_proof(
                        rows["r"], rows, {}, self.gitdir(),
                        "refs/heads/" + self.main))
                recs.append(rec)
        return answers, recs, merges

    def test_the_recorded_question_set_is_what_the_spawns_recorded(self):
        rows = self.rows()
        answers, recs, merges = self.board(rows)
        # THE SAVING: the second fold's merge-tree is the first fold's answer.
        self.assertEqual(len(merges), 1, "the lensed fold re-ran the merge")
        # THE THIRD DOOR IS SHUT TOO: since task/3056 the durable table keeps
        # the merge's raw answer, so a second board would be served it there.
        with mock.patch.object(vcs, "_batched", return_value=None), \
                mock.patch.object(vcs, "_scope_form", return_value=False), \
                mock.patch.object(gitfacts, "lookup", return_value=None):
            shut, shut_recs, shut_merges = self.board(rows)
        # THE CONTROL: with every door shut the same two folds pay two merges,
        # so the one above is the memo and not a fixture that merged once.
        self.assertEqual(len(shut_merges), 2)
        self.assertEqual([a[0] for a in answers], [True, True],
                         "the fixture's carried proof must affirm")
        self.assertEqual(answers, shut)
        # THE PROPERTY: every question, in order, with its exit code and its
        # stdout, recorded identically by both folds either way — so the plan
        # and the re-check the checkpoint builds from them cannot move.
        self.assertEqual([r.calls for r in recs], [r.calls for r in shut_recs])
        lensed = [c[1] for c in recs[1].calls]
        self.assertIn("merge-tree", [argv[0] for argv in lensed],
                      "a memo hit must reach the recorder like a spawn")
        self.assertIn(("rev-parse", rows["b"]["tip"] + "^{tree}"), lensed)
        self.assertEqual(foldckpt.plan(recs[1]), foldckpt.plan(shut_recs[1]))
        self.assertTrue(foldckpt.plan(recs[1]), "the plan is empty")

    def test_a_merge_the_durable_table_answers_is_recorded_like_a_spawn(self):
        """task/3056: `gitfacts` keeps the replay's raw `merge-tree` answer
        under the view git's merge machinery reads, so a LATER scope -- the
        next cold fold -- is answered without a process. The fold checkpoint
        must still see that question and its answer, or it would key a close
        on reads that never included the merge that decided it. And the reads
        that take the view must NOT reach the recorder: they are not questions
        the fold consumed, and the plan cannot re-verify them."""
        root = os.path.join(self.tmp, "gitfacts")
        with mock.patch.object(gitfacts, "_root", lambda: root):
            rows = self.rows()
            first, first_recs, paid = self.board(rows)
            again, again_recs, merges = self.board(rows)
        # THE UNCONDITIONAL CONTROL: the first scope really paid its merge.
        self.assertEqual(len(paid), 1)
        self.assertEqual([a[0] for a in first], [True, True],
                         "the fixture's carried proof must affirm")
        self.assertEqual(merges, [],
                         "a second scope re-ran a merge the table holds")
        self.assertEqual(again, first)
        self.assertEqual([r.calls for r in again_recs],
                         [r.calls for r in first_recs],
                         "an answer from the table was recorded differently "
                         "from the spawn it stands for")
        asked = {c[1][0] for r in again_recs for c in r.calls}
        self.assertIn("merge-tree", asked)
        self.assertNotIn("config", asked,
                         "the view's own reads reached the fold's recorder")
        self.assertEqual(foldckpt.plan(again_recs[0]),
                         foldckpt.plan(first_recs[0]))


class TheKeyIsTheFold(FoldCheckpointBase):
    """task/3043: the checkpoint key names only what can change the fold, and
    every miss says which key missed."""

    #: What Orca exports into every seat it launches (read off a live seat's
    #: environment): two credential entries, which `git config --list` reports.
    ORCA = {"GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.interactive",
            "GIT_CONFIG_VALUE_0": "never",
            "GIT_CONFIG_KEY_1": "credential.guiPrompt",
            "GIT_CONFIG_VALUE_1": "false"}

    def repository(self):
        cwds = sorted(self.header()["git"])
        self.assertTrue(cwds, "the fold read no git, so no fingerprint is "
                              "under test")
        return cwds[0]

    def reasons(self, at="restore"):
        return [m["reason"] for m in foldckpt.misses(dispatches.ledger_path())
                if m.get("at") == at]

    def test_an_orca_seat_restores_a_plain_seats_checkpoint(self):  # noqa: VACUOUS_ASSERTION — the fingerprints are asserted equal and the Orca read to restore, then a CONTROL config set the same way is asserted to change the fingerprint
        self.carried()
        self.settle()
        cwd = self.repository()
        plain, _facts = foldckpt.fingerprint(cwd, {})
        with mock.patch.dict(os.environ, self.ORCA):
            orca, _facts = foldckpt.fingerprint(cwd, {})
            roads, _out = self.assert_equivalent()
        self.assertEqual(orca, plain, "credential config changed git's view")
        self.assert_restored(roads)
        # THE CONTROL, set through the same channel: a config git's merge
        # machinery reads is still part of the key.
        with mock.patch.dict(os.environ, {
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "merge.renames",
                "GIT_CONFIG_VALUE_0": "false"}):
            self.assertNotEqual(foldckpt.fingerprint(cwd, {})[0], plain)

    def test_every_miss_is_named_in_the_log_the_swallows_and_the_hook_span(self):  # noqa: VACUOUS_ASSERTION — every reason is asserted by its text in the miss log, the breadcrumb by its type and the hook report by its exact row
        from helm import hooklatency, record
        self.carried()
        self.settle()
        cwd = self.repository()
        self.git("config", "merge.renames", "false")
        with hooklatency.event_scope("standalone"):
            roads, _out = self.assert_equivalent()
        self.assert_full(roads)
        stale = "git's view of %s changed" % cwd
        self.assertIn(stale, self.reasons())
        # THE REPOSITORY'S OWN CONFIG MOVED, which every reader sees: the
        # fingerprints name the file, so the save wrote a new one beside the
        # old, and replaced nothing.
        self.assertEqual(self.reasons("replace"), [])
        self.assertEqual(len(os.listdir(self.store_dir())), 2)
        self.assertIn({"reason": stale, "events": 1},
                      hooklatency.report()["fold_misses"])
        crumbs = [c for c in record.swallows(500)
                  if c.get("exc") == "CheckpointMiss"]
        self.assertTrue(any(stale in c.get("msg", "") for c in crumbs),
                        crumbs)
        # THE ORDINARY ABSENCES are named in the log and kept out of the
        # swallow ledger, which expected states would flood.
        shutil.rmtree(self.store_dir())
        self.read()
        self.assertEqual(self.reasons()[-1], foldckpt.NO_CHECKPOINT)
        other = os.path.join(self.store_dir(), "0" * 16 + foldckpt.SUFFIX)
        os.replace(self.store(), other)
        self.read()
        self.assertEqual(self.reasons()[-1], foldckpt.WRITTEN_BY_OTHER_CODE)
        ordinary = [c for c in record.swallows(500)
                    if c.get("exc") == "CheckpointMiss"
                    and ("no checkpoint" in c.get("msg", "")
                         or "other code" in c.get("msg", ""))]
        self.assertEqual(ordinary, [])

    def test_a_lensed_burst_under_the_bound_removes_nothing(self):  # noqa: VACUOUS_ASSERTION — every planted file is asserted present by exact name
        """Eviction is per file and by last use: a burst of lensed saves
        under MAX_FILES removes no file, the plain fold's oldest included."""
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)
        key = "e" * 16
        plain = _plant(root, foldckpt.checkpoint_name("a" * 64, None, key),
                       100)                     # the oldest file of all
        lensed = [_plant(root, foldckpt.checkpoint_name(
            ("%x" % n) * 64, "a lens", key), 50 - n) for n in range(9)]
        foldckpt.Session._prune(os.path.join(root, lensed[-1]))
        self.assertEqual(set(os.listdir(root)), {plain} | set(lensed),
                         "a lensed burst under MAX_FILES removed a file")

    def test_a_read_that_saves_nothing_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the miss is asserted HELD by its exact reason before the unchanged listing, so the silence on disk is the rule and not a miss that never happened
        """`helm doctor` over a home with no ledger reads the fold and must
        leave the tree as it found it: the miss is held for a save that
        never comes, and nothing is written."""
        root = os.path.join(self.tmp, "quiet")
        os.makedirs(root)
        ledger = os.path.join(root, "dispatches.jsonl")
        session = foldckpt.begin(ledger, dispatches.epoch_path(), b"")
        self.assertIsNotNone(session)
        before = sorted(os.listdir(root))
        self.assertIsNone(session.restore())
        self.assertEqual(session.why, foldckpt.NO_CHECKPOINT)
        self.assertEqual(sorted(os.listdir(root)), before,
                         "a read that saved nothing wrote to the tree")

    def test_doctor_names_the_misses_and_warns_on_a_replacement(self):  # noqa: VACUOUS_ASSERTION — each doctor row is asserted to its exact level and to the reason text it must carry
        from helm import doctor
        self.assertIn("check_fold_checkpoint", doctor.CHECKS)
        self.assertEqual(doctor.check_fold_checkpoint(),
                         [("OK", "fold checkpoint: no miss recorded")])
        self.carried()
        self.settle()
        # A SHARED KEY GOES STALE: the ledger prefix is rewritten in place,
        # in one timestamp digit, so the fold reads the same repositories,
        # its save lands on the same file, replaces it and says why.
        path = dispatches.ledger_path()
        with open(path, "rb") as f:
            data = f.read()
        at = data.index(b'"ts":"') + len(b'"ts":"') + 18    # a seconds digit
        digit = b"1" if data[at:at + 1] != b"1" else b"2"
        with open(path, "wb") as f:
            f.write(data[:at] + digit + data[at + 1:])
        self.read()
        rows = doctor.check_fold_checkpoint()
        self.assertEqual(len(rows), 1, rows)
        level, text = rows[0]
        self.assertEqual(level, "WARN", text)
        self.assertIn("the ledger prefix was rewritten", text)
        self.assertIn("replacement", text)

    def test_the_files_beside_the_store_are_classified(self):  # noqa: VACUOUS_ASSERTION — both files are asserted present on disk first, so an empty squatter list means the registry names them and not that nothing was written
        """The save lock and the miss log live beside the store, and the
        projection survey must name them: an undeclared file under the home
        is a squatter `helm doctor` warns about on every run."""
        self.carried()
        self.settle()
        self.git("config", "merge.renames", "false")
        self.read()
        ledger = dispatches.ledger_path()
        self.assertTrue(os.path.exists(foldckpt.save_lock_path(ledger)))
        self.assertTrue(os.path.exists(foldckpt.misses_path(ledger)))
        _rows, squat = registry.projection_survey()
        self.assertEqual([f for f in squat["home"] if "ledger-fold" in f],
                         [])



class SurfaceReads(FoldCheckpointBase):
    """One read by one reader surface, told by its road and its misses.
    Holds no arm of its own."""

    ORCA = TheKeyIsTheFold.ORCA
    LANE = "c" * 64

    @contextlib.contextmanager
    def surface(self, name, code=None):
        with contextlib.ExitStack() as stack:
            if name == "orca":
                stack.enter_context(mock.patch.dict(os.environ, self.ORCA))
            if code or name == "lane":
                stack.enter_context(mock.patch.object(
                    foldckpt, "policy", return_value=code or self.LANE))
            yield

    def fresh(self):
        shutil.rmtree(self.store_dir(), ignore_errors=True)
        path = foldckpt.misses_path(dispatches.ledger_path())
        for p in (path, path + ".1"):
            if os.path.exists(p):
                os.remove(p)

    def files(self):
        return sorted(os.listdir(self.store_dir())) \
            if os.path.isdir(self.store_dir()) else []

    def first(self, name, code=None):
        """(the first fold's road, the restore misses) of one read."""
        path = foldckpt.misses_path(dispatches.ledger_path())
        if os.path.exists(path):
            os.remove(path)
        with self.surface(name, code):
            got, roads = self.read()
        want, _out = self.full_replay()
        self.assertEqual(got, want, "a checkpointed answer differs from a "
                                    "cold fold")
        count = len(self.ledger_events())
        road = "restored" if roads[0] == (count, 0) else \
            "full" if roads[0] == (0, count) else roads[0]
        return road, [m["reason"] for m in foldckpt.misses(
            dispatches.ledger_path()) if m.get("at") == "restore"]

    def state_of(self, rid):
        return dispatches.snapshot()[0][rid]


class TheSurfaceByStateMatrix(SurfaceReads):
    """task/3043 (with task/3048): every reader SURFACE against every STATE
    its checkpoint can be in. One arm per cell; the cell's road (restored or
    a full replay) and the reason a miss names are both asserted.

      surfaces  plain seat; Orca seat (GIT_CONFIG_COUNT credential entries);
                a lane worktree on other code (another `policy()`).
      states    warm; written by the other seat kind; after a pull touching a
                module outside the fold; after a pull touching a fold module;
                unreadable.

    A PULL IS A NEW `policy()`, WHICHEVER MODULE IT TOUCHED. The key digests
    every module of the package, because no narrower set is provably the
    fold's: the fold module's own import closure (`wiring.graph`) is 352 of
    355 modules and every one of the last 92 lands touched it. So both pull
    cells answer the same way, a named miss and the old code's checkpoint
    left for the readers still on that code."""

    def test_every_surface_against_every_state(self):  # noqa: VACUOUS_ASSERTION — every cell asserts its road and its miss list to exact values, and each read is compared byte for byte with a cold fold
        self.carried()
        other = {"plain": "orca", "orca": "plain", "lane": "plain"}
        for name in ("plain", "orca", "lane"):
            with self.subTest(surface=name, state="warm"):
                self.fresh()
                self.first(name)
                self.assertEqual(self.first(name), ("restored", []))
            with self.subTest(surface=name, state="written by the other kind"):
                self.fresh()
                self.first(other[name])
                written = self.files()
                if name == "lane":
                    self.assertEqual(self.first(name), (
                        "full", [foldckpt.WRITTEN_BY_OTHER_CODE]))
                    self.assertTrue(set(written) <= set(self.files()),
                                    "the lane evicted the hub's checkpoint")
                else:
                    self.assertEqual(self.first(name), ("restored", []))
            for state, code in (("a pull outside the fold", "d" * 64),
                                ("a pull inside the fold", "e" * 64)):
                with self.subTest(surface=name, state=state):
                    self.fresh()
                    self.first(name)
                    before = self.files()
                    self.assertEqual(self.first(name, code=code), (
                        "full", [foldckpt.WRITTEN_BY_OTHER_CODE]))
                    self.assertTrue(set(before) <= set(self.files()),
                                    "the old code's checkpoint was evicted")
            with self.subTest(surface=name, state="unreadable"):
                self.fresh()
                self.first(name)
                for f in self.files():
                    with open(os.path.join(self.store_dir(), f), "wb") as fh:
                        fh.write(b"\x00 not a checkpoint")
                road, missed = self.first(name)
                self.assertEqual(road, "full")
                self.assertEqual(len(missed), 1, missed)
                self.assertTrue(missed[0].startswith(
                    "the checkpoint could not be judged"), missed)
                self.assertEqual(self.first(name), ("restored", []))

    def test_incremental_equivalence(self):  # noqa: VACUOUS_ASSERTION — the tail read is asserted restored over exactly the appended events and equal to a cold fold
        """A restored fold advanced over the tail equals a cold fold, over a
        tail that holds every kind of event a writer appends here."""
        self.carried()
        rows = [self.dispatch(lane="lane/tail-%d" % n) for n in range(2)]
        self.settle()
        seq = {r["id"]: self.state_of(r["id"])["seq"] for r in rows}
        a, b = rows[0]["id"], rows[1]["id"]
        ts = dispatches.pk.now_ts()
        for event in (
                {"v": 3, "event": "delivered", "seq": seq[a] + 1, "id": a,
                 "ts": ts, "delivery_ref": "tail-ref"},
                {"v": 3, "event": "hold", "seq": seq[b] + 1, "id": b,
                 "ts": ts, "reason": "tail hold", "owner_gated": False},
                {"v": 3, "event": "release", "seq": seq[b] + 2, "id": b,
                 "ts": ts, "reason": "tail hold"},
                {"v": 3, "event": "cancel", "seq": seq[a] + 2, "id": a,
                 "ts": ts, "reason": "tail cancel"}):
            self.append(event)
        roads, out = self.assert_equivalent()
        self.assert_restored(roads[:1], tail=4)
        self.assertEqual(out[a]["status"], "cancelled")
        self.assertEqual(out[b]["status"], "open")



class TheFingerprintsNameTheFile(SurfaceReads):
    """task/3043: ONE DEFINITION NAMES THE FILE AND JUDGES IT.

    The checkpoint's file is keyed on the fingerprints the checkpoint
    records, taken in the env it records (the one the fold's own git ran
    under). So seats git answers identically share one file, and seats git
    answers differently never can: two such seats sharing a file could use
    neither's checkpoint, the second replaying on every read while the
    ledger stood still and then, once its fold was longer, starving the
    first. An env name git never sees (a selector the recorded env removes)
    and an entry the fingerprint drops (credential.*) name no file."""

    X = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "helm.view",
         "GIT_CONFIG_VALUE_0": "x"}
    Y = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "helm.view",
         "GIT_CONFIG_VALUE_0": "y"}

    def seat(self, view, code=None):
        """(road, restore misses) of one read by a seat in `view`."""
        with mock.patch.dict(os.environ, view):
            return self.first("plain", code)

    def plant(self, name, age):
        """A checkpoint of nothing for this class's ledger, named `name`,
        used `age` seconds ago, which no universal key refuses."""
        os.makedirs(self.store_dir(), exist_ok=True)
        return _plant_valid(self.store_dir(), name, age,
                            dispatches.ledger_path())

    @staticmethod
    def code(n):
        """A code digest whose 16-hex file-name prefix is its own."""
        return ("%x" % n) * 64

    def tail(self):
        """One raw event on a row of this ledger: a tail no writer folded."""
        seq = self.state_of(self.rid)["seq"]
        self.append({"v": 3, "event": "hold", "seq": seq + 1, "id": self.rid,
                     "ts": dispatches.pk.now_ts(), "reason": "tail hold",
                     "owner_gated": False})

    def setUp(self):
        super().setUp()
        self.carried()
        self.rid = self.dispatch(lane="lane/view-tail")["id"]
        self.fresh()

    def other_repository(self, name, abbrev):
        """A second repository whose `git config --list` differs."""
        path = os.path.join(self.tmp, name)
        subprocess.run(["git", "init", "-q", path], check=True)
        subprocess.run(["git", "-C", path, "config", "core.abbrev",
                        str(abbrev)], check=True)
        return os.path.join(path, ".git")

    def config_file(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def report(self, cwd, env):
        """WHAT GIT ANSWERS the fingerprint's questions with in `cwd`, asked
        directly of git under the ambient environment overlaid with `env`
        (None removes), config entries in a family the fingerprint drops left
        out: the reference the file name is held to."""
        child = dict(os.environ)
        for k, v in env.items():
            if v is None:
                child.pop(k, None)
            else:
                child[k] = v
        out = []
        for args in (["rev-parse", "--is-shallow-repository",
                      "--is-inside-work-tree", "--git-common-dir"],
                     ["config", "--list", "-z"],
                     ["for-each-ref", "--format=%(refname) %(objectname)",
                      "refs/replace/"]):
            done = subprocess.run(["git", "-C", cwd] + args, env=child,
                                  capture_output=True)
            body = done.stdout
            if args[0] == "config":
                body = b"\0".join(e for e in body.split(b"\0") if e
                                   and not e.lower().startswith(
                                       foldckpt._CONFIG_DROPPED))
            out.append((done.returncode, body))
        return out

    def test_two_views_alternating_on_a_still_ledger_each_restore(self):  # noqa: VACUOUS_ASSERTION — every read asserts its road and its miss list to exact values, and each is compared byte for byte with a cold fold
        self.assertEqual(self.seat(self.X)[0], "full")
        self.assertEqual(self.seat(self.Y)[0], "full")
        for _round in range(3):
            self.assertEqual(self.seat(self.X), ("restored", []))
            self.assertEqual(self.seat(self.Y), ("restored", []))

    def test_neither_view_starves_the_other_after_an_event(self):  # noqa: VACUOUS_ASSERTION — every read asserts its road to the exact (base, events) pair, and each is compared byte for byte with a cold fold
        self.seat(self.X)
        self.seat(self.Y)
        self.tail()
        count = len(self.ledger_events())
        self.assertEqual(self.seat(self.X), ((count - 1, 1), []))
        self.assertEqual(self.seat(self.Y), ((count - 1, 1), []))
        self.assertEqual(self.seat(self.X), ("restored", []))
        self.assertEqual(self.seat(self.Y), ("restored", []))

    def test_a_view_change_between_reads_lands_in_the_new_views_own_file(self):  # noqa: VACUOUS_ASSERTION — the old view's file is asserted byte-identical and both views restore after, so the second file is the new view's and not a replacement
        self.seat(self.X)
        (kept,) = self.files()
        with open(os.path.join(self.store_dir(), kept), "rb") as f:
            before = f.read()
        road, missed = self.seat(self.Y)
        self.assertEqual(road, "full")
        self.assertEqual(len(self.files()), 2, self.files())
        with open(os.path.join(self.store_dir(), kept), "rb") as f:
            self.assertEqual(f.read(), before, "the new view replaced the "
                                               "old view's checkpoint")
        self.assertEqual(len(missed), 1, missed)
        self.assertTrue(missed[0].startswith("git's view of"), missed)
        self.assertEqual(self.seat(self.Y), ("restored", []))
        self.assertEqual(self.seat(self.X), ("restored", []))

    def test_an_orca_and_a_merge_config_seat_against_a_plain_one(self):  # noqa: VACUOUS_ASSERTION — the Orca read is asserted RESTORED on the plain file; the merge-config read to write a second file, leave the first byte-identical and replace nothing, and both to restore after
        """Ambient env as a seat really carries it, in the GIT_CONFIG_COUNT
        spelling: credential entries only (Orca's) read the plain seat's
        checkpoint; a merge.renames entry is another fingerprint, so it
        keeps a file of its own and neither seat replaces the other's."""
        merge = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "merge.renames",
                 "GIT_CONFIG_VALUE_0": "false"}
        self.assertEqual(self.seat({})[0], "full")
        (plain,) = self.files()
        self.assertEqual(self.seat(TheKeyIsTheFold.ORCA), ("restored", []))
        self.assertEqual(self.files(), [plain])
        with open(os.path.join(self.store_dir(), plain), "rb") as f:
            before = f.read()
        road, missed = self.seat(merge)
        self.assertEqual(road, "full")
        self.assertTrue(missed and missed[0].startswith("git's view of"),
                        missed)
        self.assertEqual(len(self.files()), 2, self.files())
        with open(os.path.join(self.store_dir(), plain), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual([m for m in foldckpt.misses(dispatches.ledger_path())
                          if m.get("at") == "replace"], [])
        self.assertEqual(self.seat(merge), ("restored", []))
        self.assertEqual(self.seat({}), ("restored", []))
        self.assertEqual(self.seat(TheKeyIsTheFold.ORCA), ("restored", []))

    def test_a_credential_only_config_parameter_is_the_same_file(self):  # noqa: VACUOUS_ASSERTION — the credential read is asserted RESTORED on the one file, and the CONTROL parameter on the same channel is asserted to write a second
        self.assertEqual(self.seat({})[0], "full")
        (one,) = self.files()
        self.assertEqual(self.seat({"GIT_CONFIG_PARAMETERS":
                                    "'credential.helper=store'"}),
                         ("restored", []))
        self.assertEqual(self.files(), [one])
        self.assertEqual(self.seat({"GIT_CONFIG_PARAMETERS":
                                    "'helm.view=z'"})[0], "full")
        self.assertEqual(len(self.files()), 2, self.files())

    def test_an_ambient_git_dir_is_the_same_file_and_the_same_answer(self):  # noqa: VACUOUS_ASSERTION — both GIT_DIR reads are asserted RESTORED on the one file and their rows equal to the unset read's, including the carried close
        a = self.other_repository("a", 7)
        b = self.other_repository("b", 12)
        # The two answer git differently, so a key that saw GIT_DIR would
        # split on them.
        self.assertNotEqual(self.report(a, {}), self.report(b, {}))
        want, _out = self.full_replay()
        self.assertEqual(self.seat({})[0], "full")
        (one,) = self.files()
        for gitdir in (a, b):
            with mock.patch.dict(os.environ, {"GIT_DIR": gitdir}):
                got, roads = self.read()
            self.assertEqual(got["rows"], want["rows"], gitdir)
            count = len(self.ledger_events())
            self.assertEqual(roads[0], (count, 0), gitdir)
            self.assertEqual(self.files(), [one], gitdir)
        self.assertIn("'close_reason': 'carried'", want["rows"])

    def test_every_selector_moves_the_file_exactly_when_git_answers_differently(self):  # noqa: VACUOUS_ASSERTION — each cell compares the file set with git's own answers under the same env, both ways, and a selector the recorded env removes also keeps the fold's rows
        """For every selector vcs names (`vcs._REPO_SELECTION_ENV` and the
        fold's own `dispatches._GIT_SELECTION_ENV`), each of three values:
        the file changes exactly when git's answers to the fingerprint's
        questions, asked in the env the checkpoint records, change."""
        want, _out = self.full_replay()
        self.first("plain")
        base = self.files()
        with open(os.path.join(self.store_dir(), base[0]), "rb") as f:
            (cwd, entry), = json.loads(f.readline())["git"].items()
        recorded = dict(entry["env"])
        answers = self.report(cwd, recorded)
        values = (self.other_repository("elsewhere", 12),
                  self.config_file("helm.cfg", "[helm]\n\tview = z\n"),
                  self.config_file("cred.cfg",
                                   "[credential]\n\thelper = store\n"))
        names = sorted(set(vcs._REPO_SELECTION_ENV)
                       | set(dispatches._GIT_SELECTION_ENV))
        self.assertIn("GIT_DIR", names)
        self.assertIn("GIT_CONFIG_GLOBAL", names)
        moved = []
        for name in names:
            for value in values:
                with self.subTest(selector=name, value=value):
                    with mock.patch.dict(os.environ, {name: value}):
                        same = self.report(cwd, recorded) == answers
                        self.fresh()
                        got, _roads = self.read()
                    files = self.files()
                    self.assertEqual(files == base, same, files)
                    if name in recorded and recorded[name] is None:
                        self.assertTrue(same, "the recorded env removes "
                                              "%s and git still saw it" % name)
                        self.assertEqual(got["rows"], want["rows"])
                    moved.append(not same)
        # BOTH POLES WERE REACHED, so the equality above was not one-sided.
        self.assertIn(True, moved)
        self.assertIn(False, moved)

    def test_a_restore_takes_one_fingerprint_per_repository(self):
        """Choosing the file costs no git the judgement did not already
        spend: each restore fingerprints each recorded repository once."""
        self.first("plain")
        with open(os.path.join(self.store_dir(), self.files()[0]), "rb") as f:
            cwds = json.loads(f.readline())["git"]
        self.assertTrue(cwds)
        with mock.patch.object(foldckpt, "fingerprint",
                               wraps=foldckpt.fingerprint) as fp, \
                mock.patch.object(foldckpt.Session, "restore", autospec=True,
                                  side_effect=foldckpt.Session.restore) as rs:
            _got, roads = self.read()
        self.assertEqual(roads[0], (len(self.ledger_events()), 0))
        self.assertTrue(rs.call_count)
        self.assertEqual(fp.call_count, rs.call_count * len(cwds))

    def test_a_zero_and_a_multi_repository_fold_each_restore(self):  # noqa: VACUOUS_ASSERTION — every fold is asserted to write its file, restore it, and the two-repository fold to miss by the repository whose config moved
        """The key digests every repository a fold recorded, and a fold may
        record none or several."""
        ledger = os.path.join(os.path.dirname(dispatches.ledger_path()),
                              "fold-arm.jsonl")
        data = b'{"id":"a"}\n'
        state = {"out": {}, "verdicts": {}, "taken": {},
                 "actors": {"validated": {}, "unresolved": {}}}
        second = os.path.join(self.tmp, "second")
        subprocess.run(["git", "init", "-q", second], check=True)
        subprocess.run(["git", "-C", second, "-c", "user.name=t", "-c",
                        "user.email=t@t", "commit", "-q", "--allow-empty",
                        "-m", "one"], check=True)
        repos = {self.gitdir(): self.git("rev-parse", "HEAD"),
                 os.path.realpath(os.path.join(second, ".git")):
                     self.git("rev-parse", "HEAD", cwd=second)}
        zero = None
        for chosen in ({}, repos):
            with self.subTest(repositories=len(chosen)):
                rec = foldckpt.Recorder()
                rec.calls.extend(
                    (cwd, ("cat-file", "-e", sha + "^{commit}"), (), False,
                     0, b"") for cwd, sha in chosen.items())
                writer = foldckpt.begin(ledger, dispatches.epoch_path(), data)
                self.assertTrue(writer.save(dict(state), 1, writer.end, None,
                                            rec))
                self.assertEqual(sorted(writer.git), sorted(chosen))
                self.assertTrue(os.path.isfile(writer.store()))
                reader = foldckpt.begin(ledger, dispatches.epoch_path(), data)
                got = reader.restore()
                self.assertIsNotNone(got, reader.why)
                self.assertEqual(sorted(got[0]["git"]), sorted(chosen))
                self.assertEqual(reader.store(), writer.store())
                zero = zero if chosen else writer.store()
        # The two-repository save subsumed the zero-repository one, which
        # nobody the new file does not serve could still read.
        self.assertEqual(os.listdir(os.path.dirname(zero)),
                         [os.path.basename(reader.store())])
        self.git("config", "helm.edit", "1", cwd=second)
        reader = foldckpt.begin(ledger, dispatches.epoch_path(), data)
        self.assertIsNone(reader.restore())
        self.assertEqual(reader.why, "git's view of %s changed"
                         % os.path.realpath(os.path.join(second, ".git")))

    ARM_STATE = {"out": {}, "verdicts": {}, "taken": {},
                 "actors": {"validated": {}, "unresolved": {}}}
    ARM_LINES = [b'{"id":"a"}\n', b'{"id":"b"}\n', b'{"id":"c"}\n']

    def arm_ledger(self, data):
        ledger = os.path.join(os.path.dirname(dispatches.ledger_path()),
                              "fold-arm.jsonl")
        with open(ledger, "wb") as f:
            f.write(data)
        return ledger

    def arm_save(self, ledger, data, count, code=None, rec=None):
        """The file one save of `count` events writes, under `code`."""
        with contextlib.ExitStack() as stack:
            if code:
                stack.enter_context(mock.patch.object(
                    foldckpt, "policy", return_value=code))
            writer = foldckpt.begin(ledger, dispatches.epoch_path(), data)
            self.assertTrue(writer.save(dict(self.ARM_STATE), count,
                                        writer.end, None,
                                        rec or foldckpt.Recorder()))
            return writer.store()

    def removals(self, ledger):
        return [m["reason"] for m in foldckpt.misses(ledger)
                if m.get("at") == "remove"]

    def test_a_universally_refused_file_is_removed_by_the_next_save(self):  # noqa: VACUOUS_ASSERTION — each dead file is asserted gone with its reason logged, and the save's own file asserted present
        whole = b"".join(self.ARM_LINES)
        rewritten = whole.replace(b'"b"', b'"B"')
        for way, after, reason in (
                ("shorter", b"".join(self.ARM_LINES[:2]),
                 "the ledger is shorter than the checkpoint"),
                ("prefix", rewritten, "the ledger prefix was rewritten"),
                ("malformed", whole, "not a checkpoint of this format"),
                ("another ledger", whole,
                 "the checkpoint describes another ledger")):
            with self.subTest(way=way):
                ledger = self.arm_ledger(whole)
                shutil.rmtree(foldckpt.store_dir(ledger), ignore_errors=True)
                dead = self.arm_save(ledger, whole, 3, code="d" * 64)
                if way == "malformed":
                    with open(dead, "wb") as f:
                        f.write(b"\x00 not a checkpoint\n")
                if way == "another ledger":
                    other = os.path.join(os.path.dirname(ledger),
                                         "fold-arm-other.jsonl")
                    with open(other, "wb") as f:
                        f.write(whole)
                    source = self.arm_save(other, whole, 3, code="d" * 64)
                    os.replace(source, dead)
                self.arm_ledger(after)
                saved = self.arm_save(ledger, after, 1)
                self.assertFalse(os.path.exists(dead),
                                 "a file dead for every reader outlived "
                                 "the next save")
                self.assertTrue(os.path.exists(saved))
                self.assertIn("removed: " + reason, self.removals(ledger))

    def test_a_file_refused_only_on_another_readers_keys_survives(self):  # noqa: VACUOUS_ASSERTION — each reader-specific file is asserted present after the save, and a universally dead one beside them asserted gone
        whole = b"".join(self.ARM_LINES)
        ledger = self.arm_ledger(whole)
        head = self.git("rev-parse", "HEAD")
        rec = foldckpt.Recorder()
        rec.calls.append((self.gitdir(), ("cat-file", "-e",
                                          head + "^{commit}"), (), False, 0,
                          b""))
        with mock.patch.dict(os.environ, self.X):
            view = self.arm_save(ledger, whole, 3, rec=rec)
        code = self.arm_save(ledger, whole, 3, code="d" * 64)
        dead = self.arm_save(ledger, whole, 3, code="e" * 64)
        with open(dead, "wb") as f:
            f.write(b"\x00 not a checkpoint\n")
        saved = self.arm_save(ledger, whole, 2)
        self.assertTrue(os.path.exists(view), "a file refused only on "
                        "another view's fingerprints was removed")
        self.assertTrue(os.path.exists(code), "a file refused only on "
                        "another code was removed")
        self.assertFalse(os.path.exists(dead))
        self.assertTrue(os.path.exists(saved))

    def test_the_just_saved_file_is_never_removed(self):  # noqa: VACUOUS_ASSERTION — the saved file is asserted present though the ledger read at the sweep refuses it, and an older dead file asserted gone so the sweep is shown to have run
        whole = b"".join(self.ARM_LINES)
        ledger = self.arm_ledger(whole)
        dead = self.arm_save(ledger, whole, 3, code="d" * 64)
        writer = foldckpt.begin(ledger, dispatches.epoch_path(), whole)
        # THE LEDGER IS REWRITTEN SHORTER between this save's read and its
        # write: the sweep, reading the ledger again, refuses this save's own
        # file too, and must remove the other dead file only.
        self.arm_ledger(b"".join(self.ARM_LINES[:2]))
        self.assertTrue(writer.save(dict(self.ARM_STATE), 3, writer.end,
                                    None, foldckpt.Recorder()))
        self.assertTrue(os.path.exists(writer.store()),
                        "the sweep removed the file its own save wrote")
        self.assertFalse(os.path.exists(dead))

    def test_a_refused_longer_candidate_yields_to_a_usable_shorter_one(self):  # noqa: VACUOUS_ASSERTION — the shorter file is asserted to RESTORE by its exact path and event count after the longer one is asserted refused by its exact reason
        """THE LONGEST USABLE CANDIDATE CAN BE STALE ON A KEY EVERY READER
        SHARES while a shorter one is not, and the restore must fall through
        to it. A reader reproduces every nested key: a fold that read one
        repository and one that read two. When the ledger is rewritten
        shorter than the two-repository file, the fresh save lands under the
        one-repository key and supersedes nothing longer, so the stale file
        stays until it retires; a restore that judged only the longest then
        replayed the whole ledger on every read for up to RETIRE_S, where
        main's single file was replaced once and restored after."""
        ledger = os.path.join(os.path.dirname(dispatches.ledger_path()),
                              "fold-arm.jsonl")
        lines = [b'{"id":"a"}\n', b'{"id":"b"}\n', b'{"id":"c"}\n']
        state = {"out": {}, "verdicts": {}, "taken": {},
                 "actors": {"validated": {}, "unresolved": {}}}
        second = os.path.join(self.tmp, "second")
        subprocess.run(["git", "init", "-q", second], check=True)
        subprocess.run(["git", "-C", second, "-c", "user.name=t", "-c",
                        "user.email=t@t", "commit", "-q", "--allow-empty",
                        "-m", "one"], check=True)
        both = {self.gitdir(): self.git("rev-parse", "HEAD"),
                os.path.realpath(os.path.join(second, ".git")):
                    self.git("rev-parse", "HEAD", cwd=second)}
        one = {self.gitdir(): both[self.gitdir()]}

        def read_of(chosen):
            rec = foldckpt.Recorder()
            rec.calls.extend(
                (cwd, ("cat-file", "-e", sha + "^{commit}"), (), False, 0,
                 b"") for cwd, sha in chosen.items())
            return rec

        # A fold of the whole ledger that read both repositories.
        data = b"".join(lines)
        with open(ledger, "wb") as f:
            f.write(data)
        writer = foldckpt.begin(ledger, dispatches.epoch_path(), data)
        self.assertTrue(writer.save(dict(state), 3, writer.end, None,
                                    read_of(both)))
        longer = writer.store()
        # A fold of the first two events that read only the first
        # repository, saved under a second key while both are valid; it is
        # not subsumed and subsumes nothing longer. Then the ledger is
        # rewritten to those two events, with no save after it, so no sweep
        # (`_bury`) has removed the longer file, now dead, before the read.
        data = b"".join(lines[:2])
        reader = foldckpt.begin(ledger, dispatches.epoch_path(), data)
        self.assertTrue(reader.save(dict(state), 2, reader.end, None,
                                    read_of(one)))
        shorter = reader.store()
        self.assertNotEqual(shorter, longer)
        with open(ledger, "wb") as f:
            f.write(data)
        self.assertEqual(sorted(os.listdir(os.path.dirname(longer))),
                         sorted(os.path.basename(p) for p in (longer, shorter)))
        # The next read: the longer file is refused on the ledger, and the
        # shorter one, valid for the ledger as it is, restores.
        again = foldckpt.begin(ledger, dispatches.epoch_path(), data)
        got = again.restore()
        self.assertIsNotNone(got, "the longer candidate's refusal (%s) was "
                                  "the read's answer" % again.why)
        self.assertEqual(got[0]["ledger"]["events"], 2)
        self.assertEqual(sorted(got[0]["git"]), sorted(one))
        self.assertEqual(again.store(), shorter)

    def test_the_name_and_the_check_move_together(self):  # noqa: VACUOUS_ASSERTION — the drifted read is asserted to miss by name AND to write a second file, and the undrifted read after it to restore the first
        """ONE DEFINITION: the fingerprint names the file AND judges it. So a
        change to the fingerprint moves both at once: the read misses by
        `git's view` AND writes a new file, never restoring a file its
        check refuses, never writing into a file named by another key."""
        self.first("plain")
        (first,) = self.files()
        real = foldckpt.fingerprint

        def drifted(cwd, env):
            fp, facts = real(cwd, env)
            # A DIFFERENT first digit, whatever the real one is.
            return (None if fp is None else
                    ("1" if fp[0] == "0" else "0") + fp[1:]), facts
        with mock.patch.object(foldckpt, "fingerprint", drifted):
            road, missed = self.first("plain")
        self.assertEqual(road, "full")
        self.assertTrue(missed and missed[0].startswith("git's view of"),
                        missed)
        self.assertEqual(len(self.files()), 2, self.files())
        self.assertEqual([m for m in foldckpt.misses(dispatches.ledger_path())
                          if m.get("at") == "replace"], [])
        self.assertEqual(self.first("plain"), ("restored", []))
        self.assertIn(first, self.files())

    def test_a_key_no_reader_reproduces_retires_past_its_age(self):  # noqa: VACUOUS_ASSERTION — the surviving set is asserted by exact name, and the current key's file is asserted to restore
        """A config edit moves every reader's key, so the file written
        before it is read by nobody; it goes once its newest file is older
        than RETIRE_S, and a key edited away only an hour ago stays."""
        self.first("plain")
        (first,) = self.files()
        path = os.path.join(self.store_dir(), first)
        at = time.time_ns() - (foldckpt.RETIRE_S + 3600) * 1_000_000_000
        os.utime(path, ns=(at, at))
        self.git("config", "helm.edit", "1")
        self.assertEqual(self.first("plain")[0], "full")
        (second,) = self.files()
        self.assertNotEqual(second, first, "a key nobody reproduces outlived "
                                           "RETIRE_S")
        self.git("config", "helm.edit", "2")
        self.assertEqual(self.first("plain")[0], "full")
        left = self.files()
        self.assertIn(second, left, "a key edited away an hour ago was "
                                    "retired before RETIRE_S")
        self.assertEqual(len(left), 2, left)
        self.assertEqual(self.first("plain"), ("restored", []))

    def test_five_live_views_each_restore(self):  # noqa: VACUOUS_ASSERTION — every second read is asserted RESTORED with no miss
        """Five seats git answers five ways, on one unchanged ledger: each
        saves once, and none of those saves retires another's file."""
        views = [{"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "helm.view",
                  "GIT_CONFIG_VALUE_0": str(n)} for n in range(5)]
        for view in views:
            self.assertEqual(self.seat(view)[0], "full")
        self.assertEqual(len(self.files()), 5, self.files())
        for view in views:
            self.assertEqual(self.seat(view), ("restored", []), view)

    def test_a_lens_term_nobody_saves_under_retires_past_its_age(self):  # noqa: VACUOUS_ASSERTION — the retired and the kept lens files are each asserted by exact name
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)
        dead = self.plant(foldckpt.checkpoint_name(
            "a" * 64, "a dead lens", "e" * 16), foldckpt.RETIRE_S + 3600)
        live = self.plant(foldckpt.checkpoint_name(
            "a" * 64, "a live lens", "e" * 16), 3600)
        self.assertEqual(self.first("plain")[0], "full")
        left = self.files()
        self.assertNotIn(dead, left, "a lens term nobody saves under "
                                     "outlived RETIRE_S")
        self.assertIn(live, left)

    def test_a_young_file_survives_a_burst_of_saves_elsewhere(self):  # noqa: VACUOUS_ASSERTION — every file is asserted present by exact name
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)
        young = _plant(root, foldckpt.checkpoint_name(
            "a" * 64, None, "y" * 16), foldckpt.RETIRE_S - 3600)
        others = [_plant(root, foldckpt.checkpoint_name(
            "a" * 64, None, ("%x" % n) * 16), 3600 * n) for n in range(1, 5)]
        burst = [_plant(root, foldckpt.checkpoint_name(
            self.code(n + 1), None, "e" * 16), 60 - n) for n in range(10)]
        foldckpt.Session._prune(os.path.join(root, burst[-1]))
        self.assertEqual(set(self.files()),
                         {young} | set(others) | set(burst))

    def test_the_hubs_plain_file_survives_a_lensed_save(self):  # noqa: VACUOUS_ASSERTION — all three files are asserted present by exact name
        """A lane's ./bin/helm writes plain files under its own code all
        day, so the newest plain file is often the lane's; a lensed save
        must not take the hub's plain file, the one every seat reads."""
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)
        hub = _plant(root, foldckpt.checkpoint_name(
            "a" * 64, None, "e" * 16), 60)
        lane = _plant(root, foldckpt.checkpoint_name(
            "b" * 64, None, "e" * 16), 30)
        lensed = _plant(root, foldckpt.checkpoint_name(
            "a" * 64, "a lens", "e" * 16), 0)
        foldckpt.Session._prune(os.path.join(root, lensed))
        self.assertEqual(set(self.files()), {hub, lane, lensed})

    def test_every_restore_refreshes_recency(self):  # noqa: VACUOUS_ASSERTION — the refreshed mtime is asserted to within a few seconds of now, from a planted value a minute old
        self.assertEqual(self.first("plain")[0], "full")
        (name,) = self.files()
        path = os.path.join(self.store_dir(), name)
        at = time.time_ns() - 60 * 1_000_000_000
        os.utime(path, ns=(at, at))
        self.assertEqual(self.first("plain"), ("restored", []))
        self.assertLess(time.time_ns() - os.stat(path).st_mtime_ns,
                        5 * 1_000_000_000,
                        "a restore a minute after the last use did not "
                        "refresh the recency")

    def test_the_byte_bound_evicts_least_recently_used_under_the_count(self):  # noqa: VACUOUS_ASSERTION — the kept and the evicted files are each asserted by exact name
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)

        def plant(name, age, size):
            _plant(root, name, age)
            with open(os.path.join(root, name), "r+b") as f:
                f.truncate(size)
            os.utime(os.path.join(root, name), ns=(
                time.time_ns() - int(age * 1e9),) * 2)
            return name
        files = [plant(foldckpt.checkpoint_name(self.code(n + 1), None,
                                                "c" * 16), 60 * (n + 1), 300)
                 for n in range(5)]
        saved = plant(foldckpt.checkpoint_name("a" * 64, None, "e" * 16),
                      0, 300)
        with mock.patch.object(foldckpt, "MAX_BYTES", 1000, create=True):
            foldckpt.Session._prune(os.path.join(root, saved))
        self.assertLess(len(files) + 1, foldckpt.MAX_FILES)
        self.assertEqual(set(self.files()), {saved} | set(files[:2]))

    def test_the_file_just_saved_survives_even_alone_over_the_byte_bound(self):  # noqa: VACUOUS_ASSERTION — the saved file is asserted the only one left
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)
        other = _plant(root, foldckpt.checkpoint_name("1" * 64, None,
                                                      "c" * 16), 60)
        saved = _plant(root, foldckpt.checkpoint_name("a" * 64, None,
                                                      "e" * 16), 0)
        with open(os.path.join(root, saved), "r+b") as f:
            f.truncate(500)
        with mock.patch.object(foldckpt, "MAX_BYTES", 100, create=True):
            foldckpt.Session._prune(os.path.join(root, saved))
        self.assertEqual(self.files(), [saved])
        self.assertNotIn(other, self.files())

    def age(self, name, seconds):
        """Set the recency of the store file `name` to `seconds` ago."""
        at = time.time_ns() - int(seconds * 1_000_000_000)
        os.utime(os.path.join(self.store_dir(), name), ns=(at, at))

    def views(self, n, first=0):
        return [{"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "helm.view",
                 "GIT_CONFIG_VALUE_0": "v%d" % k}
                for k in range(first, first + n)]

    def test_a_view_that_only_restores_keeps_its_file(self):  # noqa: VACUOUS_ASSERTION — the restoring view is asserted RESTORED after four other views saved, its save being older than RETIRE_S
        """(a) A view USED within RETIRE_S keeps its newest file however many
        other views save, although its last SAVE is older than RETIRE_S: a
        restore refreshes the file's recency."""
        self.assertEqual(self.seat(self.X)[0], "full")
        (mine,) = self.files()
        self.age(mine, foldckpt.RETIRE_S + 3600)
        self.assertEqual(self.seat(self.X), ("restored", []))
        for view in self.views(4):
            self.assertEqual(self.seat(view)[0], "full")
        self.assertIn(mine, self.files())
        self.assertEqual(self.seat(self.X), ("restored", []))

    def test_a_view_unused_past_its_age_goes(self):  # noqa: VACUOUS_ASSERTION — the unused view's file is asserted gone and the saving view's to restore
        self.assertEqual(self.seat(self.X)[0], "full")
        (mine,) = self.files()
        self.age(mine, foldckpt.RETIRE_S + 3600)
        self.assertEqual(self.seat(self.Y)[0], "full")
        self.assertNotIn(mine, self.files(), "a view unused past RETIRE_S "
                                             "kept its file")
        self.assertEqual(self.seat(self.Y), ("restored", []))

    @contextlib.contextmanager
    def paused_in_prune(self, target):
        """Pause the first `os.stat` `_prune` takes of `target` until the
        body returns: (paused, a thread-safe event set once it has paused)."""
        real = os.stat
        paused, resume = threading.Event(), threading.Event()

        def stat(path, *args, **kw):
            st = real(path, *args, **kw)
            if path == target and not paused.is_set() \
                    and sys._getframe(1).f_code.co_name == "_prune":
                paused.set()
                resume.wait(20)
            return st
        with mock.patch.object(foldckpt.os, "stat", stat):
            try:
                yield paused
            finally:
                resume.set()

    def test_a_restore_racing_a_prune_is_linearised(self):  # noqa: VACUOUS_ASSERTION — the racing restore is asserted RESTORED and prompt, and the invariant is asserted on the file's measured recency and presence
        """A restore concurrent with a prune either refreshes first and the
        prune sees it, or loses to the prune and already holds its state. It
        never refreshes a file the prune then removes from a stale snapshot:
        the race measured with a barrier inside the prune's stat."""
        self.assertEqual(self.first("plain")[0], "full")
        (name,) = self.files()
        path = os.path.join(self.store_dir(), name)
        old = time.time_ns() - (foldckpt.RETIRE_S + 3600) * 1_000_000_000
        os.utime(path, ns=(old, old))
        ledger = dispatches.ledger_path()
        with open(ledger, "rb") as f:
            first = f.readline()
        writer = foldckpt.begin(ledger, dispatches.epoch_path(), first)
        state = {"out": {}, "verdicts": {}, "taken": {},
                 "actors": {"validated": {}, "unresolved": {}}}
        with self.paused_in_prune(path) as paused:
            saver = threading.Thread(target=writer.save, args=(
                state, 1, writer.end, None, foldckpt.Recorder()))
            saver.start()
            self.assertTrue(paused.wait(20), "the save never reached its "
                                             "prune")
            began = time.monotonic()
            _got, roads = self.read()
            spent = time.monotonic() - began
            refreshed = os.stat(path).st_mtime_ns != old
        saver.join(20)
        self.assertEqual(roads[0], (len(self.ledger_events()), 0),
                         "the racing read did not restore")
        self.assertLess(spent, 10, "the racing restore waited on the prune")
        self.assertFalse(refreshed and not os.path.exists(path),
                         "a restore refreshed the file and the prune then "
                         "removed it from its stale snapshot")

    def test_a_refresh_that_finds_the_lock_busy_skips_it(self):  # noqa: VACUOUS_ASSERTION — the recency is asserted to its exact planted value and the call to return promptly
        self.assertEqual(self.first("plain")[0], "full")
        (name,) = self.files()
        path = os.path.join(self.store_dir(), name)
        old = time.time_ns() - 60 * 1_000_000_000
        os.utime(path, ns=(old, old))
        session = foldckpt.begin(dispatches.ledger_path(),
                                 dispatches.epoch_path(), b"")
        with foldckpt._save_lock(dispatches.ledger_path()) as held:
            self.assertTrue(held)
            began = time.monotonic()
            session._recent(path)
            self.assertLess(time.monotonic() - began,
                            foldckpt.REFRESH_WAIT_S + 1.0)
        self.assertEqual(os.stat(path).st_mtime_ns, old,
                         "a refresh wrote while the save lock was held")
        # A FILE THE PRUNE ALREADY REMOVED is no fault to the refresh.
        session._recent(path + ".gone")

    def test_the_prune_never_removes_a_file_whose_recency_moved(self):  # noqa: VACUOUS_ASSERTION — the moved file is asserted present and a control file of the same age asserted removed
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)
        age = foldckpt.RETIRE_S + 3600
        moved = _plant(root, foldckpt.checkpoint_name("1" * 64, None,
                                                      "c" * 16), age)
        control = _plant(root, foldckpt.checkpoint_name("2" * 64, None,
                                                        "c" * 16), age)
        keep = _plant(root, foldckpt.checkpoint_name("3" * 64, None,
                                                     "c" * 16), 0)
        with self.paused_in_prune(os.path.join(root, moved)) as paused:
            pruner = threading.Thread(target=foldckpt.Session._prune,
                                      args=(os.path.join(root, keep),))
            pruner.start()
            self.assertTrue(paused.wait(20))
            os.utime(os.path.join(root, moved))
        pruner.join(20)
        self.assertIn(moved, self.files(), "the prune removed a file whose "
                                           "recency moved after its snapshot")
        self.assertNotIn(control, self.files())

    def test_a_victim_refreshed_mid_prune_is_replaced_by_another(self):  # noqa: VACUOUS_ASSERTION — the count, the survivor and the replacement victim are each asserted exactly
        """25 files against a limit of 24, the oldest refreshed by an
        unlocked updater (an older code version's restore) after the prune
        snapshotted it: the prune still returns within the bound, by
        evicting the next least recently used file instead."""
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)
        others = [_plant(root, foldckpt.checkpoint_name(
            self.code(n % 15 + 1), None, "%016x" % n), 600 - 20 * n)
            for n in range(foldckpt.MAX_FILES)]
        keep = _plant(root, foldckpt.checkpoint_name("a" * 64, None,
                                                     "e" * 16), 0)
        oldest, next_oldest = others[0], others[1]
        with self.paused_in_prune(os.path.join(root, oldest)) as paused:
            pruner = threading.Thread(target=foldckpt.Session._prune,
                                      args=(os.path.join(root, keep),))
            pruner.start()
            self.assertTrue(paused.wait(20))
            os.utime(os.path.join(root, oldest))
        pruner.join(20)
        left = self.files()
        self.assertEqual(len(left), foldckpt.MAX_FILES, left)
        self.assertIn(oldest, left)
        self.assertNotIn(next_oldest, left)

    def test_an_undeletable_oldest_file_does_not_monopolize_the_prune(self):
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)
        others = [_plant(root, foldckpt.checkpoint_name(
            self.code(n % 15 + 1), None, "%016x" % n), 600 - 20 * n)
            for n in range(foldckpt.MAX_FILES + 1)]
        keep = _plant(root, foldckpt.checkpoint_name("a" * 64, None,
                                                     "e" * 16), 0)
        blocked, replacement = others[:2]
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if path == os.path.join(root, blocked):
                raise PermissionError("checkpoint is not removable")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(foldckpt.os, "unlink", side_effect=unlink):
            foldckpt.Session._prune(os.path.join(root, keep))
        left = self.files()
        self.assertEqual(len(left), foldckpt.MAX_FILES, left)
        self.assertIn(blocked, left)
        self.assertIn(keep, left)
        self.assertNotIn(replacement, left)

    def test_a_refresher_that_moves_every_victim_ends_at_the_pass_cap(self):  # noqa: VACUOUS_ASSERTION — the crumb is asserted by its type and the bound it names, and the call to end promptly
        from helm import record
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)
        others = [_plant(root, foldckpt.checkpoint_name(
            self.code(n % 15 + 1), None, "%016x" % n), 600 - 20 * n)
            for n in range(foldckpt.MAX_FILES + 1)]
        keep = _plant(root, foldckpt.checkpoint_name("a" * 64, None,
                                                     "e" * 16), 0)
        victims = {os.path.join(root, n) for n in others}
        real, ticks = os.stat, [time.time_ns() - 3600 * 1_000_000_000]

        def stat(path, *args, **kw):
            if path in victims and sys._getframe(1).f_code.co_name in (
                    "_prune", "_unlink_unmoved"):
                ticks[0] += 1
                os.utime(path, ns=(ticks[0], ticks[0]))
            return real(path, *args, **kw)
        began = time.monotonic()
        with mock.patch.object(foldckpt.os, "stat", stat):
            foldckpt.Session._prune(os.path.join(root, keep))
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(len(self.files()), foldckpt.MAX_FILES + 2)
        crumbs = [c for c in record.swallows(500)
                  if c.get("exc") == "StoreOverBound"]
        self.assertTrue(crumbs, "the capped prune recorded nothing")
        self.assertIn("MAX_FILES", crumbs[-1].get("msg", ""))

    def test_the_save_prunes_inside_the_save_lock(self):
        ledger = dispatches.ledger_path()
        held = []
        real = foldckpt.Session._prune

        def probe(keep):
            fd = os.open(foldckpt.save_lock_path(ledger), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
            return real(keep)
        with mock.patch.object(foldckpt.Session, "_prune",
                               staticmethod(probe)):
            self.assertEqual(self.first("plain")[0], "full")
        self.assertTrue(held)
        self.assertTrue(all(held), "a save pruned outside the save lock")

    def test_past_the_ceiling_the_least_recently_used_file_goes(self):  # noqa: VACUOUS_ASSERTION — the evicted and the kept files are each asserted by exact name, and the refreshed view to restore
        """(b) LRU EVICTION OVER FILES, BY LAST USE. A saved 240 s ago, B 180
        s ago, A restored now, 22 files used 60 s ago, and a 25th saved: B's
        goes, A's stays, because every restore refreshes recency."""
        a, b = self.views(2)
        self.assertEqual(self.seat(a)[0], "full")
        (file_a,) = self.files()
        self.assertEqual(self.seat(b)[0], "full")
        (file_b,) = [n for n in self.files() if n != file_a]
        self.age(file_a, 240)
        self.age(file_b, 180)
        self.assertEqual(self.seat(a), ("restored", []))
        root = self.store_dir()
        planted = [self.plant(foldckpt.checkpoint_name(
            "a" * 64, None, ("%02x" % n) * 8), 60)
            for n in range(foldckpt.MAX_FILES - 2)]
        self.assertEqual(self.seat(self.views(1, first=9)[0])[0], "full")
        left = self.files()
        self.assertEqual(len(left), foldckpt.MAX_FILES, left)
        self.assertNotIn(file_b, left, "the least recently USED file stayed")
        self.assertIn(file_a, left, "the least recently SAVED file went")
        self.assertTrue(set(planted) <= set(left))
        self.assertEqual(self.seat(a), ("restored", []))

    def test_the_file_ceiling_holds_and_spares_the_saved_file(self):  # noqa: VACUOUS_ASSERTION — the kept and the removed files are each asserted by exact name
        """(c) The directory holds at most MAX_FILES files, and neither the
        age nor the ceiling ever removes the file the save just wrote."""
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)
        files = [_plant(root, foldckpt.checkpoint_name(
            self.code(n % 15 + 1), None, ("%02x" % n) * 8), 60 * n)
            for n in range(foldckpt.MAX_FILES + 2)]
        saved = _plant(root, foldckpt.checkpoint_name(
            "a" * 64, None, "e" * 16), foldckpt.RETIRE_S + 3600)
        foldckpt.Session._prune(os.path.join(root, saved))
        self.assertEqual(set(self.files()),
                         {saved} | set(files[:foldckpt.MAX_FILES - 1]))

    def test_another_views_file_survives_a_burst_of_code_versions(self):  # noqa: VACUOUS_ASSERTION — the other view's file is asserted to restore after the burst, and every file to be kept under MAX_FILES
        old = "a" * 64
        self.assertEqual(self.seat(self.Y, old)[0], "full")
        for n in range(9):
            self.assertEqual(self.seat(self.X, self.code(n + 1))[0], "full")
        self.assertEqual(self.seat(self.Y, old), ("restored", []))
        self.assertEqual(len(self.files()), 10, self.files())

    def test_dead_code_files_retire_past_their_age(self):  # noqa: VACUOUS_ASSERTION — the retired and the kept files are each asserted by exact name
        """A file of a code version nobody runs any more, under the key
        spelling or the one before it, goes once unused past RETIRE_S; a
        young one of another code stays."""
        root = self.store_dir()
        os.makedirs(root, exist_ok=True)
        old = foldckpt.RETIRE_S + 3600
        dead = [self.plant("0" * 16 + foldckpt.SUFFIX, old),
                self.plant("3" * 16 + ".v" + "d" * 16 + foldckpt.SUFFIX, old),
                self.plant(foldckpt.checkpoint_name("2" * 64, None,
                                                    "c" * 16), old)]
        live = self.plant(foldckpt.checkpoint_name("4" * 64, None,
                                                   "c" * 16), 3600)
        self.assertEqual(self.first("plain")[0], "full")
        left = self.files()
        for name in dead:
            self.assertNotIn(name, left, "a dead code file outlived RETIRE_S")
        self.assertIn(live, left)
