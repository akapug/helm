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
import io
import json
import marshal
import os
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock

from helm import (dispatches, eventledger, foldckpt, landreq, pk, projscope,
                  seats_room_advice, seats_stop_budget, seats_stop_guard,
                  seats_stop_timing, vcs)
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
    if _LIVE_SEATS_PATCH is not None:
        _LIVE_SEATS_PATCH.stop()


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
            self.assertIn("f" * 16 + foldckpt.SUFFIX, names)
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
        self.git("merge", "--no-ff", "-q", "-m",
                 "trunk merges the lane whole", "side")
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
        self.git("merge", "--no-ff", "-q", "-m",
                 "trunk merges the lane whole", "side")
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

        def broken(gd, *argv):
            if argv[:2] == ("merge-base", "--is-ancestor"):
                return 129, ""
            return real(gd, *argv)

        with mock.patch.object(rowworld, "_git", broken):
            why = dispatches._close_event_error(event, standing, current=pre)
        self.assertTrue(why, "an unreadable probe AFFIRMED the close, so the "
                             "gate authorized on a question it never asked")
        self.assertIn("probe did not run", why)
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
        and `KEEP` still bounds what one ledger leaves behind."""
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


class TheStopGuardRebuildsAMissingCheckpoint(FoldCheckpointBase):
    """task/2949: the Stop guard read every obligation as UNKNOWN because it
    could never rebuild a checkpoint that a deploy had invalidated.

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
        """The guard's own read: `seats_room_advice._ledger_snapshot` under
        `State.run("dispatch-ledger", ...)`, with the fallback the guard uses,
        inside an ambient scope of BUDGET_S. The clock moves only when a row
        is folded."""
        cost = self.ROW_COST_S if cost is None else cost
        clock = [1000.0]
        real = dispatches._new_state

        def slow(row):
            clock[0] += cost
            return real(row)

        # THE LADDER CAN ARM A REAL WALL-CLOCK TIMER. `State()` starts the
        # timing trace, whose one-shot flush is a daemon thread that prints to
        # stderr 12.25 real seconds later. tests/__init__.py switches the
        # trace off for the suite (HELM_STOP_TIMING_AFTER=off), so no timer is
        # armed here today. This is the belt for a run that turns it on: the
        # trace goes to this arm's own buffer, and the arm fails if a timer
        # outlives the read.
        self.addCleanup(seats_stop_timing.reset)
        with mock.patch("time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(dispatches, "_new_state", slow), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                budget = seats_stop_budget.State()
                with projscope.scope(
                        deadline=clock[0] + seats_stop_budget.BUDGET_S):
                    budget.bind_deadline()
                    clock[0] += self.BEGAN_S
                    snap = budget.run(
                        "dispatch-ledger", seats_room_advice._ledger_snapshot,
                        fallback=({}, seats_stop_guard.LEDGER_RESERVE_SPENT))
            finally:
                seats_stop_timing.settle()
        self.assertIsNone(seats_stop_timing._STATE.timer,
                          "the ladder's flush timer outlived this read")
        return snap, budget

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
        self.assertEqual(answers[0], seats_stop_guard.LEDGER_RESERVE_SPENT)
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
        (again, why), budget = self.guard_read()
        self.assertIsNone(why)
        self.assertEqual(repr(again), repr(out))
        self.assertEqual(budget.yielded, set())

    def test_a_read_that_cannot_finish_still_reports_unknown_with_its_reason(self):
        self.world()
        total = len(self.ledger_events())
        (rows, why), budget = self.guard_read()
        self.assertEqual((rows, why), ({}, seats_stop_guard.LEDGER_RESERVE_SPENT))
        self.assertEqual(budget.yielded, {"dispatch-ledger"})
        self.assertTrue(any("dispatch-ledger=UNFINISHED" in w
                            for w in budget.warns), budget.warns)
        # PROGRESS WAS BANKED, AND A BANKED PREFIX IS NOT AN ANSWER.
        self.assertTrue(0 < self.banked() < total, self.banked())
        # ONE ROW COSTS MORE THAN THE WHOLE SLICE: the check is cooperative,
        # so the first row finishes late and is banked, and the answer is
        # still the named UNKNOWN.
        shutil.rmtree(self.store_dir())
        (rows, why), budget = self.guard_read(cost=8.0)
        self.assertEqual((rows, why), ({}, seats_stop_guard.LEDGER_RESERVE_SPENT))
        self.assertEqual(budget.yielded, {"dispatch-ledger"})
        self.assertEqual(self.banked(), 1)

    def test_a_banked_prefix_resumes_exactly_and_a_stale_one_is_replayed(self):  # noqa: VACUOUS_ASSERTION — the positives are the byte-for-byte equality with a full replay on every read and the road each read took
        self.world()
        total = len(self.ledger_events())
        (_rows, why), _budget = self.guard_read()
        self.assertEqual(why, seats_stop_guard.LEDGER_RESERVE_SPENT)
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
    world = TheStopGuardRebuildsAMissingCheckpoint.world
    banked = TheStopGuardRebuildsAMissingCheckpoint.banked

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
    world = TheStopGuardRebuildsAMissingCheckpoint.world
    banked = TheStopGuardRebuildsAMissingCheckpoint.banked
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
