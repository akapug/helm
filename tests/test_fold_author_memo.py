#!/usr/bin/env python3
"""A replay's chain-author reads are LINEAR IN EVENTS, and no ledger write
lock outlives its deadline (task/3562).

THE DEFECT, MEASURED on the live hub 2026-09-28: auto-land's `fold_apply`
held the dispatch ledger WRITE lock for 14+ minutes. The py-spy stack ran
`_ledger_write -> snapshot_with_verdicts -> _fold_into -> _apply ->
_source_clean_landed_error -> source_clean_holder_error -> chain_authors ->
_contributor_chains -> tip_writers -> _first_bringers`: every source-clean
close event the replay validated rebuilt the WHOLE ledger's contributor join
over the fold prefix, so one cold replay cost events x ledger, and the tick's
replay was cold because the tick had fast-forwarded the checkout it runs from
(`foldckpt.policy()` names no code once its tree drifts, so no checkpoint is
read or written for the rest of that process).

THE ARMS COUNT, THEY DO NOT TIME. A replay of N source-clean closes is folded
cold and the per-row author derivations (`_brings`, `_chain_repo`) and the
whole-ledger joins it pays are counted, so the linearity proof is the same on
every host. The equivalence arm drives the incremental index through random
row changes and compares every answer with the whole join, including rows
the fold logged before the ledger opened them, so the index orders first
bringers by ledger position and never by catch-up order. A writer past its
lock deadline refuses in one line through the real CLI, and auto-land's
post-land close runs in a child of the installed helm, as its fold does.
"""
import contextlib
import io
import os
import random
import threading
import unittest
from unittest import mock

from helm import (autoland, dispatches, eventledger, landreq, projscope)
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_source_clean_landed as _sc


def setUpModule():
    from tests._tmphome import pin_live_seats
    pin_live_seats()


class _Counter(object):
    """Wrap `landreq.<name>` and count its calls."""

    def __init__(self, case, name):
        self.calls = 0
        real = getattr(landreq, name)

        def counted(*args, **kw):
            self.calls += 1
            return real(*args, **kw)
        patcher = mock.patch.object(landreq, name, counted)
        patcher.start()
        case.addCleanup(patcher.stop)


class ReplayAuthorCostIsLinearTest(_sc.SourceCleanBase):
    """N held source-clean rows and N closes that bind, folded from zero."""

    def ledger_of(self, n, prefix="memo"):
        """-> (events, ids): a template close through the real door, then `n`
        held rows each closed by the template re-aimed at it (the forgery
        `TheReplayTest` proves the fold accepts)."""
        token = self.mint(self.c)
        done = self.held(self.b, "lane/%s-template" % prefix)
        _out, err = self.close(done, token)
        self.assertIsNone(err, err)
        real = self.close_event(done["id"])
        ids = []
        for i in range(n):
            row = self.row(self.b, "lane/%s-%d" % (prefix, i))
            self.hold(row, self.b)
            ids.append(row["id"])
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        for rid in ids:
            state = rows[rid]
            event = dict(real, id=rid, seq=int(state["seq"]) + 1,
                         ts=dispatches.pk.now_ts(), reviewed_tip=self.b,
                         source_clean_hold_actor=state["hold_actor"],
                         source_clean_hold_ts=state["hold_ts"])
            event.pop("close_actor", None)
            event["source_clean_anchor"] = \
                dispatches_close_anchor(event)
            self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                               event))
        events = list(eventledger.events(dispatches.ledger_path()))
        return events, ids

    def cold_fold(self, events):
        """-> (state, brings, repos, joins): one fold from zero, counted."""
        brings = _Counter(self, "_brings")
        repos = _Counter(self, "_chain_repo")
        joins = _Counter(self, "_contributor_chains")
        out, _verdicts, _taken = dispatches._fold(events)
        return out, brings.calls, repos.calls, joins.calls

    def test_a_replay_of_N_source_clean_closes_derives_each_row_a_bounded_number_of_times(self):
        events, ids = self.ledger_of(12)
        out, brings, repos, joins = self.cold_fold(events)
        # THE CONTROL: every close bound, so the author rung was really asked.
        for rid in ids:
            self.assertEqual(out[rid]["status"], "closed", rid)
            self.assertEqual(out[rid]["close_reason"], _sc.REASON)
        rows = len(out)
        self.assertEqual(joins, 0, "the replay rebuilt the whole-ledger join "
                                   "%d times" % joins)
        self.assertLessEqual(
            brings, 3 * rows,
            "%d row-provenance derivations for %d rows and %d closes: the "
            "replay re-derives the ledger per close" % (brings, rows,
                                                        len(ids)))
        self.assertLessEqual(repos, rows + 1)

    def test_doubling_the_closes_at_most_doubles_the_derivations(self):
        small, _ids = self.ledger_of(6)
        _o, brings_small, _r, _j = self.cold_fold(small)
        big, _ids = self.ledger_of(6, "more")  # six more on the same ledger
        _o, brings_big, _r, _j = self.cold_fold(big)
        rows_small = len({e.get("id") for e in small})
        rows_big = len({e.get("id") for e in big})
        self.assertGreater(rows_big, rows_small)
        # LINEAR: the extra cost is bounded by the extra rows, never by the
        # extra rows times the ledger they were folded over.
        self.assertLessEqual(brings_big - brings_small,
                             3 * (rows_big - rows_small),
                             "small %d, big %d" % (brings_small, brings_big))


def dispatches_close_anchor(event):
    from helm import dispatches_close
    return dispatches_close._source_clean_anchor(event)


class TheIndexAnswersWhatTheJoinAnswersTest(unittest.TestCase):
    """THE INCREMENTAL INDEX IS THE JOIN, ASKED CHEAPLY: every answer it
    gives inside a fold watch equals the whole join's over the same rows."""

    SEATS = ("s-a", "s-b", "s-c", "s-d")
    TIPS = tuple("%040x" % n for n in range(1, 9))

    def setUp(self):
        self.repo = "/fixture/repo-a"
        self.other = "/fixture/repo-b"
        patcher = mock.patch.object(dispatches, "home_repo_id",
                                    return_value=(self.repo, None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def random_row(self, rng, rid, ids):
        row = {"id": rid, "sender": rng.choice(self.SEATS),
               "recipient": rng.choice(self.SEATS),
               "kind": rng.choice(("review", "review", "build")),
               "tip": rng.choice(self.TIPS),
               "ts": "2026-09-%02dT00:00:00Z" % rng.randint(1, 28)}
        pick = rng.random()
        if pick < 0.1:
            pass                                      # a legacy row, no repo
        elif pick < 0.15:
            row["repo_id"] = ""                       # damaged identity
        else:
            row["repo_id"] = rng.choice((self.repo, self.repo, self.other))
        if ids and rng.random() < 0.6:
            parent = rng.choice(ids)
            row["supersedes"] = parent
            row["chain_root"] = rng.choice((parent, ids[0]))
        if rng.random() < 0.3:
            row["reviewed_tip"] = rng.choice(self.TIPS)
        if rng.random() < 0.2:
            row["patch_tip"] = rng.choice(self.TIPS)
            row["patch_author"] = rng.choice(self.SEATS)
            row["verdict_ts"] = "2026-09-%02dT01:00:00Z" % rng.randint(1, 28)
        if rng.random() < 0.2:
            row["retips"] = [{"tip": rng.choice(self.TIPS),
                              "old_tip": row["tip"],
                              "ts": "2026-09-%02dT02:00:00Z"
                              % rng.randint(1, 28)}]
        if rng.random() < 0.2:
            row["ref_branch"] = rng.choice(("refs/heads/main",
                                            "refs/heads/lane"))
        if rng.random() < 0.2:
            row["polarity"] = "approve"
        return row

    def whole(self, row, rows):
        chains, _broken = landreq._contributor_chains(rows, {})
        wrote, _approved, err = landreq.chain_contributors(row, index=chains)
        return wrote, err

    @staticmethod
    def births(watch):
        """The fold's `taken_at` as the watch holds it: {row id: [ledger
        index of its opening event, ...]}, which a fold keeps and a test
        standing in for one keeps too. {} where the watch holds none."""
        born = getattr(watch, "born", None)
        return {} if born is None else born

    def test_random_changes_inside_a_fold_watch_answer_as_the_whole_join(self):
        for seed in range(12):
            rng = random.Random(seed)
            rows, ids = {}, []
            fresh = []
            with dispatches._watching(rows) as watch:
                born = self.births(watch)
                for step in range(60):
                    if not fresh and rng.random() < 0.15:
                        # AN EVENT FOR A ROW THE LEDGER HAS NOT OPENED YET:
                        # the fold logs its id and creates nothing, so the
                        # index's catch-up meets the id before a row the
                        # ledger opened earlier (task/3562). The next rows
                        # opened share a tip no row brought before, so the
                        # two ids race to be its first bringer.
                        watch.log.append("r%02d" % (len(ids) + 1))
                        fresh = ["%040x" % (0x100 + step)] * 4
                    if ids and not fresh and rng.random() < 0.4:
                        rid = rng.choice(ids)         # an event on a row
                        rows[rid] = dict(self.random_row(rng, rid, ids))
                    else:
                        rid = "r%02d" % len(ids)      # a new row
                        rows[rid] = self.random_row(rng, rid, ids)
                        if fresh:
                            rows[rid]["tip"] = fresh.pop()
                        born[rid] = [step]
                        ids.append(rid)
                    watch.log.append(rid)
                    if rng.random() < 0.5:
                        ask = rows[rng.choice(ids)]
                        with self.subTest(seed=seed, step=step,
                                          row=ask["id"]):
                            self.assertEqual(
                                landreq.chain_authors(ask, rows),
                                self.whole(ask, rows))
                self.assertIsNotNone(watch.index,
                                     "no index answered inside the watch")
                self.assertFalse(watch.index.failed,
                                 "the index failed, so the join answered")

    def test_a_row_logged_before_the_ledger_opened_it_keeps_its_ledger_place(self):
        """THE FIRST BRINGER IS THE LEDGER'S FIRST, NOT THE CATCH-UP'S. An
        event naming `rx` before `rx` exists is logged and opens nothing;
        `ry` then brings tip T, `rx` brings it after, and a built chain
        carries T. T's writer is `ry`'s sender, and the chain names it."""
        tip = self.TIPS[0]

        def review(rid, sender, **kw):
            row = {"id": rid, "sender": sender, "recipient": "s-d",
                   "kind": "review", "tip": tip, "repo_id": self.repo,
                   "ts": "2026-09-01T00:00:00Z"}
            row.update(kw)
            return row
        rows = {}
        with dispatches._watching(rows) as watch:
            born = self.births(watch)

            def opened(index, row):
                rows[row["id"]] = row
                born[row["id"]] = [index]
                watch.log.append(row["id"])
            opened(0, review("a0", "s-a", tip=self.TIPS[5]))
            self.assertEqual(landreq.chain_authors(rows["a0"], rows),
                             (frozenset({"s-a"}), None))
            watch.log.append("rx")                   # 1: opens nothing
            opened(2, review("ry", "s-y"))
            opened(3, review("rx", "s-x"))
            opened(4, {"id": "rb", "sender": "s-c", "recipient": "s-b",
                       "kind": "build", "tip": self.TIPS[6],
                       "repo_id": self.repo, "ts": "2026-09-01T00:00:00Z"})
            opened(5, review("rr", "s-c", supersedes="rb", chain_root="rb"))
            got = landreq.chain_authors(rows["rr"], rows)
            self.assertIsNotNone(watch.index)
            self.assertFalse(watch.index.failed)
        self.assertEqual(got, self.whole(rows["rr"], rows))
        self.assertEqual(got, (frozenset({"s-b", "s-y"}), None),
                         "the chain lost T's true first bringer")

    def test_a_real_fold_hands_its_watch_the_ledger_positions(self):
        seen = []
        real = dispatches._fold_events

        def spy(acc, *args, **kw):
            watch = dispatches._fold_watch(acc.out)
            seen.append(getattr(watch, "born", None) is acc.taken_at)
            return real(acc, *args, **kw)
        with mock.patch.object(dispatches, "_fold_events", spy):
            dispatches._fold([])
        self.assertEqual(seen, [True])

    def test_outside_a_fold_watch_the_join_is_the_whole_join(self):
        rows = {"r1": {"id": "r1", "repo_id": self.repo, "sender": "s-a"}}
        joins = []
        real = landreq._contributor_chains

        def counted(*a, **kw):
            joins.append(1)
            return real(*a, **kw)
        with mock.patch.object(landreq, "_contributor_chains", counted):
            wrote, err = landreq.chain_authors(rows["r1"], rows)
        self.assertEqual((set(wrote), err), ({"s-a"}, None))
        self.assertEqual(len(joins), 1)


class ATipManyRowsBringIsOrderedWithoutRecursionTest(unittest.TestCase):
    """FOUND BY THE SCALING ARM: `_first_bringers` orders one tip's
    candidates with a recursive walk as deep as the candidates are long, so
    a tip about a thousand rows brought raised RecursionError, and every
    source-clean close whose chain read it was refused by the accident, not
    by the rule. The order is the same at any length."""

    def test_two_thousand_sends_of_one_tip_name_the_first(self):
        found = [(None, i, False, "s-%d" % (i % 3)) for i in range(2000)]
        self.assertEqual(landreq._first_bringers(found),
                         (frozenset({"s-0"}), True))
        self.assertEqual(landreq._first_bringers(list(reversed(found))),
                         (frozenset({"s-0"}), True))

    def test_a_cycle_is_still_every_bringer_unsettled(self):
        # A wall-clock rollback: X's send precedes its own later event Y, Y's
        # stamp precedes W's, and W's precedes X's. A cycle orders nothing.
        found = [("2026-09-10T00:00:00Z", 1, False, "s-x"),
                 ("2026-09-01T00:00:00Z", 1, True, "s-y"),
                 ("2026-09-05T00:00:00Z", 0, True, "s-w")]
        self.assertEqual(landreq._first_bringers(found),
                         (frozenset({"s-x", "s-y", "s-w"}), False))
        # THE CONTROL: without the rollback the first bringer is settled.
        found[2] = ("2026-09-20T00:00:00Z", 0, True, "s-w")
        self.assertEqual(landreq._first_bringers(found),
                         (frozenset({"s-x"}), True))


class NoLockOutlivesItsDeadlineTest(unittest.TestCase):
    """(c) THE LOCKED TRY READS UNDER A DEADLINE (task/3006): a full replay
    under the dispatch ledger's write lock is bounded, and a try that runs
    out lets the lock go and says so in one line."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="helm-test-lockdl-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "dispatches.jsonl")

    def fold_reads(self, seen, spend=False):
        """A `_ledger_fold_scoped` stand-in: records the budget the fold
        read runs under, and spends it when `spend`."""
        def scoped(*_a, **_kw):
            seen.append(projscope.remaining())
            if spend:
                projscope.spend_or_raise("dispatch ledger row 0")
            return None, None, "stand-in"
        return mock.patch.object(dispatches, "_ledger_fold_scoped", scoped)

    def test_a_fold_under_the_lock_carries_a_deadline_and_an_unlocked_one_does_not(self):
        seen = []

        def body(txn):
            dispatches._ledger_fold()
            return txn.last
        with mock.patch.dict(os.environ, {"HELM_LEDGER_LOCK_FOLD_S": "30"}), \
                self.fold_reads(seen):
            self.assertTrue(dispatches._ledger_write(body, self.path,
                                                     tries=1))
            self.assertFalse(dispatches._ledger_write(body, self.path,
                                                      tries=2))
        self.assertEqual(len(seen), 2)
        self.assertIsNotNone(seen[0], "the locked read ran unbudgeted")
        self.assertLessEqual(seen[0], 30)
        self.assertGreater(seen[0], 0)
        self.assertIsNone(seen[1], "an unlocked read was budgeted")

    def test_a_locked_read_past_its_deadline_lets_the_lock_go_and_refuses(self):
        seen = []

        def body(txn):
            dispatches._ledger_fold()
            raise AssertionError("the budget did not expire")
        with mock.patch.dict(os.environ, {"HELM_LEDGER_LOCK_FOLD_S": "0"}), \
                self.fold_reads(seen, spend=True):
            with self.assertRaises(dispatches.LedgerLockDeadline) as caught:
                dispatches._ledger_write(body, self.path, tries=1)
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(caught.exception, OSError)
        self.assertIsInstance(caught.exception, projscope.Expired)
        self.assertIn("HELM_LEDGER_LOCK_FOLD_S", str(caught.exception))
        self.assertFalse(os.path.exists(self.path), "something was written")
        # THE LOCK IS FREE: another thread takes it at once.
        got = []
        t = threading.Thread(target=lambda: got.append(
            dispatches._ledger_write(lambda txn: txn.held, self.path,
                                     tries=1)))
        t.start()
        t.join(10)
        self.assertEqual(got, [True])

    def test_the_callers_own_spent_budget_stays_its_own(self):
        def body(txn):
            projscope.spend_or_raise("the caller's work")
        with projscope.scope(deadline=0.0):
            with self.assertRaises(projscope.Expired) as caught:
                dispatches._ledger_write(body, self.path, tries=1)
        self.assertNotIsInstance(caught.exception,
                                 dispatches.LedgerLockDeadline)

    def test_a_bad_value_falls_back_to_the_default(self):
        with mock.patch.dict(os.environ, {"HELM_LEDGER_LOCK_FOLD_S": "soon"}):
            self.assertEqual(dispatches.ledger_lock_fold_s(),
                             dispatches.LEDGER_LOCK_FOLD_S)


class ALockDeadlineIsOneRefusalLineTest(_sc.SourceCleanBase):
    """(c) A WRITER PAST ITS LOCK DEADLINE REFUSES IN ONE LINE through the
    real CLI: rc != 0, the lock and the knob named, no traceback, nothing
    appended. Every writer raises `LedgerLockDeadline` out of
    `_ledger_write`, so every verb that reaches one must say so plainly."""

    def cli(self, argv):
        from helm import cli
        out, err = io.StringIO(), io.StringIO()
        real = dispatches._ledger_fold_scoped

        def spent(*args, **kw):
            # THE LOCKED READ RUNS OUT, whatever the fold cache holds; an
            # unlocked read is left alone, and the one try is the locked one.
            if getattr(dispatches._LEDGER_HELD, "depth", 0) > 0:
                projscope.spend_or_raise("dispatch ledger row 0")
            return real(*args, **kw)
        with mock.patch.dict(os.environ, {"HELM_LEDGER_LOCK_FOLD_S": "0",
                                          "HELM_NO_TREE_WARNING": "1"}), \
                mock.patch.object(dispatches, "_ledger_fold_scoped", spent), \
                mock.patch.object(dispatches, "LEDGER_WRITE_TRIES", 1), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue() + err.getvalue()

    def assertOneRefusal(self, rid, argv):
        before = len(dispatches.history(rid))
        rc, text = self.cli(argv)
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertNotIn(rc, (0, None), text)
        self.assertEqual(len(lines), 1, text)
        self.assertNotIn("Traceback", text)
        self.assertIn("HELM_LEDGER_LOCK_FOLD_S", lines[0])
        self.assertIn("write lock", lines[0])
        self.assertEqual(len(dispatches.history(rid)), before,
                         "a refused writer appended")

    def test_dispatch_hold_refuses_in_one_line(self):
        row = self.dispatch(lane="lane/deadline-hold")
        self.assertOneRefusal(row["id"], ["dispatch", "hold", row["id"][:12],
                                          "waiting", "on", "deps"])

    def test_dispatch_cancel_refuses_in_one_line(self):
        row = self.dispatch(lane="lane/deadline-cancel")
        self.assertOneRefusal(row["id"], ["dispatch", "cancel",
                                          row["id"][:12], "moot", "now"])

    def test_lr_close_refuses_in_one_line(self):
        row = self.dispatch(lane="lane/deadline-close", kind="build")
        self.assertOneRefusal(row["id"], [
            "lr", "close", row["id"][:12], "--reason", "delivered-report",
            "--artifact-ref",
            "artifact:reports/x.json#blake2b:0123456789abcdef",
            "--report-ref", "a1b2c3d4e5f6", "--evidence", "report handed"])


class TheLandedFoldRunsTheLandedCodeTest(unittest.TestCase):
    """(a) AUTO-LAND'S FOLD RUNS IN A FRESH PROCESS. The tick fast-forwards
    the checkout it runs from before `fold_apply`, and a process whose tree
    changed under it names no code (`foldckpt.policy`), so an in-process
    foldcheck folds cold on every read and saves no checkpoint. A child of
    the landed tree names its code, restores or saves the checkpoint once,
    and every later read in it folds only its tail."""

    def test_fold_apply_runs_foldcheck_in_a_child_of_the_installed_helm(self):
        calls = []

        class Done(object):
            returncode, stdout, stderr = 0, "  CLOSED abc\n", ""

        def run(argv, **kw):
            calls.append((argv, kw))
            return Done()
        ops = autoland.Ops()
        with mock.patch.object(ops, "declared",
                               return_value=("refs/heads/main", "origin",
                                             "main")), \
                mock.patch.object(autoland.subprocess, "run", run), \
                mock.patch.object(landreq, "cmd_lr",
                                  side_effect=AssertionError("in-process")):
            rc, text = ops.fold_apply("/fixture/root", "a" * 40, "f" * 16)
        self.assertEqual((rc, text), (0, "  CLOSED abc\n"))
        self.assertEqual(len(calls), 1)
        argv, kw = calls[0]
        pkg = os.path.dirname(os.path.dirname(os.path.abspath(
            autoland.__file__)))
        self.assertEqual(argv[:2], [autoland.sys.executable,
                                    os.path.join(pkg, "bin", "helm")])
        self.assertEqual(argv[2:], ["lr", "foldcheck", "a" * 40, "--gate",
                                    "gate:" + "f" * 16, "--repo",
                                    "/fixture/root", "--remote", "origin",
                                    "--branch", "main", "--apply"])
        self.assertIsNotNone(kw.get("timeout"))

    def test_a_child_that_cannot_run_is_one_honest_line(self):
        ops = autoland.Ops()
        with mock.patch.object(ops, "declared",
                               return_value=("refs/heads/main", "origin",
                                             "main")), \
                mock.patch.object(autoland.subprocess, "run",
                                  side_effect=OSError("no such file")), \
                mock.patch.object(landreq, "cmd_lr",
                                  side_effect=AssertionError("in-process")):
            rc, text = ops.fold_apply("/fixture/root", "a" * 40, "f" * 16)
        self.assertNotEqual(rc, 0)
        self.assertIn("no such file", text)

    def lr_close(self, run, live=True, restart=None):
        """Ops.lr_close with `subprocess.run` as `run` and every in-process
        close refused."""
        ops = autoland.Ops()
        with mock.patch.object(autoland.subprocess, "run", run), \
                mock.patch.object(landreq, "close",
                                  side_effect=AssertionError("in-process")):
            return ops.lr_close("b" * 32, live, restart)

    def test_lr_close_runs_in_a_child_of_the_installed_helm(self):
        """The post-land close runs after `ff` too, so it reads the same
        drifted process: it runs in a child, as `fold_apply` does."""
        calls = []

        class Done(object):
            returncode, stdout, stderr = 0, '{"id": "%s"}' % ("b" * 32), ""

        def run(argv, **kw):
            calls.append((argv, kw))
            return Done()
        self.assertEqual(self.lr_close(run), ({"id": "b" * 32}, None))
        self.assertEqual(self.lr_close(run, False, "the web; a beacon"),
                         ({"id": "b" * 32}, None))
        pkg = os.path.dirname(os.path.dirname(os.path.abspath(
            autoland.__file__)))
        head = [autoland.sys.executable, os.path.join(pkg, "bin", "helm"),
                "lr", "close", "b" * 32, "--reason", "landed", "--json"]
        self.assertEqual([argv for argv, _kw in calls],
                         [head + ["--live"],
                          head + ["--needs-restart", "the web; a beacon"]])
        self.assertEqual(calls[0][1].get("timeout"), autoland.FOLD_APPLY_S)

    def test_a_close_the_child_refuses_is_its_refusal(self):
        class Done(object):
            returncode, stderr = 1, ""
            stdout = '{"closed": false, "dry_run": false, "reason": "no"}'
        row, err = self.lr_close(lambda argv, **kw: Done())
        self.assertIsNone(row)
        self.assertEqual(err, "no")

    def test_a_close_child_that_cannot_run_or_ends_late_is_one_honest_line(self):
        def cannot(argv, **kw):
            raise OSError("no such file")

        def late(argv, **kw):
            raise autoland.subprocess.TimeoutExpired(argv, kw.get("timeout"))

        class Lock(object):
            returncode, stdout = 1, ""
            stderr = "helm: lr refused: the dispatch ledger's locked read " \
                     "ran past HELM_LEDGER_LOCK_FOLD_S=60s\n"
        def locked(argv, **kw):
            return Lock()
        # None of these is the close's ANSWER: an in-process close that met
        # them raised, and the tick retried it, so the child's does too.
        for run, words in ((cannot, "no such file"), (late, "ran past"),
                           (locked, "HELM_LEDGER_LOCK_FOLD_S")):
            with self.assertRaises(autoland.CloseFailed) as got:
                self.lr_close(run)
            self.assertIn(words, str(got.exception))
            self.assertEqual(len(str(got.exception).splitlines()), 1,
                             got.exception)

    def test_a_close_child_that_crashes_is_quoted_whole_and_not_an_answer(
            self):
        """THE REVIEWER'S REPRO (REREAD 3562 at f6192ac780c): the REAL child
        of bin/helm, its `landreq.close` forced to raise KeyError. The
        failure quotes the exception, never only "Traceback (most recent
        call last):", and it raises `CloseFailed`, so the car is retried."""
        real = autoland.subprocess.run
        crash = ("import runpy, sys\n"
                 "from helm import landreq\n"
                 "def close(*a, **kw):\n"
                 "    raise KeyError('forced-in-the-child')\n"
                 "landreq.close = close\n"
                 "sys.argv = sys.argv[1:]\n"
                 "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            # the same child, with its close forced to raise
            return real(argv[:1] + ["-c", crash] + argv[1:], **kw)
        pkg = os.path.dirname(os.path.dirname(os.path.abspath(
            autoland.__file__)))
        with mock.patch.dict(os.environ, {"HELM_NO_TREE_WARNING": "1",
                                          "PYTHONPATH": pkg}):
            with self.assertRaises(autoland.CloseFailed) as got:
                self.lr_close(run)
        said = str(got.exception)
        self.assertEqual(seen[0][1], os.path.join(pkg, "bin", "helm"))
        self.assertIn("exited 1", said)
        self.assertIn("Traceback (most recent call last):", said)
        self.assertIn("KeyError: 'forced-in-the-child'", said)


if __name__ == "__main__":
    unittest.main()
