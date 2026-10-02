#!/usr/bin/env python3
"""The pair meld: one persistent mixed-family pair room per task.

Store premise every-task-runs-as-one-mixed-family-pair-in-one-persistent-meld
made mechanical. A task's first dispatch opens its room between the row's
sender and reader, round one is the PLAN, and every later dispatch of the
chain opens the next ROUND of the same room. The row stays the ledger.

The arms are named for what they hold: P1-P6 are the shape, (a)-(g) the
falsifiers. `PairMeldMutationTest` plants the old or wrong behaviour into the
shipped function one line at a time and runs the arm that must kill it.

Hermetic: every chat and helm home here is a temp dir, and no row reaches a
live room.
"""
import contextlib
import inspect
import os
import threading
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E401,E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import chat, chatdebris, dispatches, meld, pk, seats, tasks  # noqa: E402
from helm import review_door as RD  # noqa: E402
from tests import test_dispatches as td  # noqa: E402
from tests import test_meld as tm  # noqa: E402
from tests import test_review_door as trd  # noqa: E402
from tests import test_stop_spiral as tss  # noqa: E402
from helm import seats_stop_spiral as spiral_mod  # noqa: E402

run = td.run
TIP = "c" * 40
OTHER = "d" * 40
ROOT = "a1" * 16
KID = "b2" * 16
RING = "dispatch row r1 (send)"


def outcome(word="AGREED", tip=TIP,
            falsifiers="a second room; a dark reader strands it"):
    fields = [("BAR", "the one harm")]
    if falsifiers is not None:
        fields.append(("FALSIFIERS", falsifiers))
    fields += [("FINDINGS", "F1=cured-in-patch"), ("TIP", tip),
               ("NEXT", "record it on the row")]
    return "MELD OUTCOME: %s | %s" % (word, " | ".join(
        "%s: %s" % kv for kv in fields))


def row(rid=ROOT, root=ROOT, lane="pair-meld-per-task-3112", note=None,
        repo="/x/helm/.git", **kw):
    out = {"id": rid, "chain_root": root, "lane": lane, "note": note,
           "repo_id": repo, "sender": "seat-a", "recipient": "seat-b",
           "kind": "review", "tip": TIP}
    out.update(kw)
    return out


class PairRoomNameTest(unittest.TestCase):
    """P2 and (b): the room is a function of the chain. A task's room is
    `<scope>-<N>`; tests/test_task_meld_name.py pins the legacy room a task
    keeps once it has opened."""

    def test_a_task_named_on_the_first_row_keys_the_room(self):
        r = row()
        self.assertEqual(RD.pair_room(r, {ROOT: r}),
                         ("helm-3112", "task/3112"))

    def test_a_chain_with_no_task_is_keyed_by_its_root(self):
        r = row(lane="fix-the-fold")
        self.assertEqual(RD.pair_room(r, {ROOT: r}),
                         ("meld-0-pair-helm-chain-" + ROOT[:12],
                          "chain/" + ROOT[:12]))

    def test_b_every_row_of_a_chain_names_its_first_rows_room(self):
        """A later round with a renamed lane, or one citing another task in
        passing, stays in the room its chain opened."""
        root = row()
        kid = row(rid=KID, lane="renamed-r2", note="cf task/2566")
        cur = {ROOT: root, KID: kid}
        self.assertEqual(RD.pair_room(kid, cur), RD.pair_room(root, cur))
        bare = row(lane="plain-lane")
        bare_kid = row(rid=KID, lane="now-names-task-99")
        cur = {ROOT: bare, KID: bare_kid}
        self.assertEqual(RD.pair_room(bare_kid, cur)[0],
                         "meld-0-pair-helm-chain-" + ROOT[:12])

    def test_two_tasks_on_the_first_row_fall_back_to_the_chain(self):
        r = row(lane="task-1-and-task-2")
        self.assertEqual(RD.pair_room(r, {ROOT: r})[1], "chain/" + ROOT[:12])

    def test_the_task_the_first_row_records_keys_the_room(self):
        """The stored key (task/3643: `--task`, or the lane's record) wins
        over the lane's literal, and a later round stays in its room."""
        root = row(lane="task-1-and-task-2", task="task/4100")
        kid = row(rid=KID, lane="now-names-task-99")
        cur = {ROOT: root, KID: kid}
        self.assertEqual(RD.pair_room(root, cur),
                         ("helm-4100", "task/4100"))
        self.assertEqual(RD.pair_room(kid, cur), RD.pair_room(root, cur))
        # a suffix is never a task: the chain root keys it
        bare = row(lane="canary-seeded-red-0926")
        self.assertEqual(RD.pair_room(bare, {ROOT: bare})[1],
                         "chain/" + ROOT[:12])

    def test_a_chain_keeps_its_room_in_the_owning_projects_scope(self):
        """A chain never leaves its repository, so its room is the owning
        project's, whoever sends into it. The same task number in two
        projects is two rooms."""
        other = row(repo="/x/adopter-project-dev/.git", lane="task-12",
                    sender="seat-a")
        kid = row(rid=KID, repo="/x/adopter-project-dev/.git",
                  lane="review-it", sender="seat-c")
        cur = {ROOT: other, KID: kid}
        self.assertEqual(RD.pair_room(kid, cur)[0], "adopter-afd5094f-12")
        helm = row(repo="/x/helm/.git", lane="task-12")
        self.assertEqual(RD.pair_room(helm, {ROOT: helm})[0], "helm-12")

    def test_long_project_names_with_one_prefix_keep_distinct_rooms(self):  # noqa: VACUOUS_ASSERTION — both generated room names are positive values and their inequality is the collision falsifier
        one = row(repo="/x/abcdefghijklmnop-one/.git", lane="task-12")
        two = row(repo="/x/abcdefghijklmnop-two/.git", lane="task-12")
        self.assertNotEqual(RD.pair_room(one, {ROOT: one})[0],
                            RD.pair_room(two, {ROOT: two})[0])

    def test_project_slug_normalization_cannot_merge_distinct_scopes(self):  # noqa: VACUOUS_ASSERTION — both fixed cases construct two positive room names and assert their collision falsifier directly
        for one, two in (("foo.bar", "foo-bar"),
                         ("abcdefghijklmnop.one",
                          "abcdefghijklmnop-one")):
            with self.subTest(one=one, two=two):
                left = row(repo="/x/%s/.git" % one, lane="task-12")
                right = row(repo="/x/%s/.git" % two, lane="task-12")
                self.assertNotEqual(RD.pair_room(left, {ROOT: left})[0],
                                    RD.pair_room(right, {ROOT: right})[0])

    def test_the_room_is_a_meld_room_and_its_name_is_stable(self):
        for r in (row(), row(lane="plain"),
                  row(repo="/x/" + "long-project-name" * 4 + "/.git")):
            with self.subTest(lane=r["lane"], repo=r["repo_id"]):
                room = RD.pair_room(r, {ROOT: r})[0]
                self.assertTrue(RD.is_meld_room(room), room)
                self.assertTrue(RD.is_pair_room(room), room)
                self.assertEqual(pk.slug(room), room)
                self.assertLessEqual(len(room), 60)

    def test_an_unknown_chain_names_no_room(self):
        r = row(rid=KID, root=dispatches.CHAIN_UNKNOWN)
        room, why = RD.pair_room(r, {KID: r})
        self.assertIsNone(room)
        self.assertIn("UNKNOWN", why)


class PairRoundTest(tm.MeldBase):
    """P3 and (f), on the meld verbs: a persistent room holds rounds."""

    ROOM = "meld-0-pair-helm-task-3112"

    def round(self, peer="seat-b", convener="seat-a", topic="a round"):
        meld.invite(peer, topic, seat=convener, room=self.ROOM, ring=RING)
        return meld.state(self.ROOM, convener)["epoch"]

    def test_P3_the_exchange_cap_counts_one_round(self):
        cap = mock.patch.dict(os.environ, {"HELM_MELD_CAP": "2"})
        cap.start()
        self.addCleanup(cap.stop)
        self.round()
        meld.join(self.ROOM, seat="seat-b")
        meld.recv(self.ROOM, timeout=0, seat="seat-a", poll=0.01)   # READY
        meld.say(self.ROOM, "YIELD", "one", seat="seat-a")
        self.assertEqual(meld.recv(self.ROOM, timeout=0, seat="seat-b",
                                   poll=0.01)[0], 0)                # seed
        self.assertEqual(meld.recv(self.ROOM, timeout=0, seat="seat-b",
                                   poll=0.01)[0], 0)                # one
        meld.say(self.ROOM, "YIELD", "two", seat="seat-a")
        code, lines = meld.recv(self.ROOM, timeout=0, seat="seat-b",
                                poll=0.01)
        self.assertEqual(code, meld.EXIT_BOUND)
        self.assertIn("reason=cap", lines[0])
        # the next round, same room: a fresh cap
        self.round(topic="the next round")
        meld.join(self.ROOM, seat="seat-b")
        code, lines = meld.recv(self.ROOM, timeout=0, seat="seat-b",
                                poll=0.01)
        self.assertEqual(code, 0, lines)
        self.assertIn("the next round", lines[0])
        st = meld.state(self.ROOM, "seat-b")
        self.assertEqual((st["exchanges"], st["cap"]), (1, 2))

    def test_rounds_are_strictly_newer_epochs_in_one_room(self):  # noqa: VACUOUS_ASSERTION — the room list is asserted EQUAL to the one pair room and the seed count to two, both positive on the same observables
        first = self.round()
        second = self.round(topic="again")
        self.assertGreater(second, first)
        self.assertEqual([r for r in chat.list_rooms()
                          if r.startswith("meld-")], [self.ROOM])
        self.assertEqual(len(meld.seeds(chat.read(self.ROOM)[0])), 2)

    def test_concurrent_openings_serialize_epoch_and_publication(self):  # noqa: VACUOUS_ASSERTION — two published epochs and a measured max concurrency of one are unconditional positive controls
        gate = threading.Barrier(3)
        active = {"n": 0, "max": 0}
        lock = threading.Lock()
        real = meld._room_epoch_max
        errors = []

        def measured(room):
            with lock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            try:
                time.sleep(0.05)
                return real(room)
            finally:
                with lock:
                    active["n"] -= 1

        def open_one(peer):
            gate.wait()
            try:
                self.round(peer=peer, topic="concurrent " + peer)
            except Exception as exc:                         # noqa: BLE001
                errors.append(exc)

        with mock.patch.object(meld, "_room_epoch_max", side_effect=measured):
            threads = [threading.Thread(target=open_one, args=(peer,))
                       for peer in ("seat-b", "seat-c")]
            for thread in threads:
                thread.start()
            gate.wait()
            for thread in threads:
                thread.join(5)
        self.assertFalse(errors)
        self.assertFalse([t for t in threads if t.is_alive()])
        epochs = [s[0] for s in meld.seeds(chat.read(self.ROOM)[0])]
        self.assertEqual(len(epochs), 2)
        self.assertEqual(len(set(epochs)), 2)
        self.assertEqual(active["max"], 1)

    def test_pair_open_returns_the_epoch_and_ordinal_of_its_own_row(self):  # noqa: VACUOUS_ASSERTION — two returned ordinals, two seed row ids and each row-to-epoch equality are unconditional positive controls
        gate = threading.Barrier(3)
        rows = [row(recipient="seat-b"),
                row(rid=KID, root=ROOT, recipient="seat-c")]
        current = {r["id"]: r for r in rows}
        opened = []

        def open_one(r):
            gate.wait()
            opened.append(RD.open_pair_round(r, current=current))

        threads = [threading.Thread(target=open_one, args=(r,)) for r in rows]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(5)
        self.assertFalse([t for t in threads if t.is_alive()])
        self.assertEqual(sorted(x["round"] for x in opened), [1, 2])
        room = RD.pair_room(rows[0], current)[0]
        self.assertEqual([x["room"] for x in opened], [room, room])
        seeds = {RD._seed_row(text): (epoch, text)
                 for epoch, _convener, text, _i in
                 meld.seeds(chat.read(room)[0])}
        self.assertEqual(set(seeds), {ROOT[:12], KID[:12]})
        for got in opened:
            rid = RD._seed_row(got["topic"])
            self.assertEqual(got["epoch"], seeds[rid][0])
            self.assertIn(RD.PAIR_PLAN if got["round"] == 1
                          else RD.PAIR_NEXT_ROUND, got["topic"])

    def test_one_dispatch_row_opens_exactly_one_round_across_retries(self):  # noqa: VACUOUS_ASSERTION — both concurrent calls return the one positive seed's epoch and ordinal
        r = row(recipient="seat-b")
        current = {r["id"]: r}
        gate = threading.Barrier(3)
        opened, errors = [], []

        def retry():
            gate.wait()
            try:
                opened.append(RD.open_pair_round(r, current=current))
            except Exception as exc:                         # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=retry) for _i in range(2)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(5)
        self.assertFalse(errors)
        self.assertFalse([t for t in threads if t.is_alive()])
        self.assertEqual([got["round"] for got in opened], [1, 1])
        self.assertEqual(len({got["epoch"] for got in opened}), 1)
        self.assertEqual(len({got["topic"] for got in opened}), 1)
        self.assertEqual(len(meld.seeds(chat.read(opened[0]["room"])[0])), 1)

    def test_retry_repairs_a_seed_only_partial_opening(self):  # noqa: VACUOUS_ASSERTION — the durable seed is asserted before retry repairs the missing invite and lifecycle exactly once
        r = row(recipient="seat-b")
        current = {r["id"]: r}
        room = RD.pair_room(r, current)[0]
        real = meld._post
        calls = {"n": 0}

        def crash_after_first_post(*args, **kwargs):
            calls["n"] += 1
            out = real(*args, **kwargs)
            if calls["n"] == 1:
                raise RuntimeError("fixture crash after seed")
            return out

        with mock.patch.object(meld, "_post", side_effect=crash_after_first_post):
            failed = RD.open_pair_round(r, current=current)
        self.assertIn("fixture crash after seed", failed["error"])
        self.assertEqual(len(meld.seeds(chat.read(room)[0])), 1)
        self.assertIsNone(meld.state(room, "seat-a"))

        opened = RD.open_pair_round(r, current=current)
        rows = chat.read(room)[0]
        invites = [m for m in rows if "[MELD-INVITE e:" in
                   str(m.get("text") or "")]
        self.assertEqual(len(meld.seeds(rows)), 1)
        self.assertEqual(len(invites), 1)
        self.assertEqual(meld.state(room, "seat-a")["epoch"],
                         opened["epoch"])

    def test_retry_repairs_an_opening_with_no_lifecycle_transition(self):  # noqa: VACUOUS_ASSERTION — seed and invite are positive controls before retry adds the missing state without duplicating either row
        r = row(recipient="seat-b")
        current = {r["id"]: r}
        room = RD.pair_room(r, current)[0]
        with mock.patch.object(meld, "_transition",
                               side_effect=RuntimeError(
                                   "fixture crash before transition")):
            failed = RD.open_pair_round(r, current=current)
        self.assertIn("fixture crash before transition", failed["error"])
        before = chat.read(room)[0]
        self.assertEqual(len(meld.seeds(before)), 1)
        self.assertEqual(len([m for m in before if "[MELD-INVITE e:" in
                              str(m.get("text") or "")]), 1)
        self.assertIsNone(meld.state(room, "seat-a"))

        opened = RD.open_pair_round(r, current=current)
        after = chat.read(room)[0]
        self.assertEqual(meld.state(room, "seat-a")["epoch"],
                         opened["epoch"])
        self.assertEqual(len(meld.seeds(after)), 1)
        self.assertEqual(len([m for m in after if "[MELD-INVITE e:" in
                              str(m.get("text") or "")]), 1)

    def test_an_unavailable_opening_lock_refuses_before_any_room_write(self):  # noqa: VACUOUS_ASSERTION — a normal opening writes one seed first, controlling the refused path's zero rows
        r = row(recipient="seat-b")
        current = {r["id"]: r}
        control = RD.open_pair_round(r, current=current)
        self.assertEqual(len(meld.seeds(chat.read(control["room"])[0])), 1)
        other = row(rid=KID, root=ROOT, recipient="seat-c")
        real = chat._room_lock

        @contextlib.contextmanager
        def unavailable(name, timeout_s=None):
            if str(name).startswith("meld-open-"):
                yield False
                return
            with real(name, timeout_s=timeout_s) as held:
                yield held

        with mock.patch.object(chat, "_room_lock", side_effect=unavailable):
            failed = RD.open_pair_round(other, current={r["id"]: r,
                                                        other["id"]: other})
        self.assertIn("opening lock unavailable", failed["error"])
        self.assertEqual(len(meld.seeds(chat.read(control["room"])[0])), 1)

    def test_a_max_length_room_uses_a_distinct_opening_lock(self):  # noqa: VACUOUS_ASSERTION — the positive seed count proves the nested room posts completed under the non-recursive lock double
        room = "meld-0-" + "x" * 53
        self.assertEqual(len(room), 60)
        self.assertNotEqual(pk.slug(meld._opening_lock_name(room)),
                            pk.slug(room))
        active = set()

        @contextlib.contextmanager
        def nonrecursive(name, timeout_s=None):
            key = pk.slug(name)
            if key in active:
                raise AssertionError("opening lock aliases room write lock")
            active.add(key)
            try:
                yield True
            finally:
                active.remove(key)

        with mock.patch.object(chat, "_room_lock", side_effect=nonrecursive):
            meld.invite("seat-b", "max-length room", seat="seat-a",
                        room=room)
        self.assertEqual(len(meld.seeds(chat.read(room)[0])), 1)

    def test_join_reads_the_newest_round(self):
        self.round(peer="seat-b")
        second = self.round(peer="seat-c", topic="rebound")
        lines = meld.join(self.ROOM, seat="seat-c")
        self.assertIn("epoch=%d" % second, lines[0])
        with self.assertRaises(SystemExit) as cm:
            meld.join(self.ROOM, seat="seat-b")
        self.assertIn("convened for {seat-c}", str(cm.exception))

    def test_the_lifecycle_journal_folds_every_round(self):  # noqa: VACUOUS_ASSERTION — each seat's state is asserted to carry the second round's epoch, the positive control on the same fold
        self.round()
        meld.join(self.ROOM, seat="seat-b")
        meld.recv(self.ROOM, timeout=0, seat="seat-a", poll=0.01)
        meld.say(self.ROOM, "DONE", outcome(), seat="seat-a")
        second = self.round(topic="round two")
        meld.join(self.ROOM, seat="seat-b")
        events, _unavailable = meld._events(meld.lifecycle_path(self.ROOM))
        _states, _unique, why, _rp = meld._reduce(events, self.ROOM)
        self.assertIsNone(why)
        for seat in ("seat-a", "seat-b"):
            st = meld.state(self.ROOM, seat)
            self.assertNotEqual(st.get("status"), "UNKNOWN", st)
            self.assertEqual(st["epoch"], second)

    def test_P6_a_rung_round_posts_no_mention(self):  # noqa: VACUOUS_ASSERTION — the plain invite below is the positive control on the same observable
        _room, lines = meld.invite("seat-b", "rung", seat="seat-a",
                                   room=self.ROOM, ring=RING)
        rows = chat.read(self.ROOM)[0]
        self.assertFalse([m for m in rows if "@seat-b" in m["text"]])
        self.assertIn("no @mention posted", "\n".join(lines))
        meld.invite("seat-b", "plain", seat="seat-a", room=self.ROOM)
        rows = chat.read(self.ROOM)[0]
        self.assertTrue([m for m in rows if "@seat-b" in m["text"]])

    def test_f_a_joiner_gets_a_digest_bounded_to_its_window_never_the_log(self):
        body = "x" * 3000
        for n in range(12):
            epoch = self.round(topic="round %d of the task: %s" % (n, "y" * 120))
            chat.post("[MELD e:%d] %s [YIELD]" % (epoch, body),
                      room=self.ROOM, who="seat-a", sign=False)
        self.round(peer="seat-c", topic="the rebound reader")
        whole = sum(len(m["text"].encode()) for m in chat.read(self.ROOM)[0])
        for window, budget in ((115072, 4602), (25000, 1024)):
            with self.subTest(window=window), mock.patch.dict(
                    os.environ,
                    {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(window)}):
                self.assertEqual(meld.digest_budget()[0], budget)
                lines = meld.history_digest(
                    self.ROOM, chat.read(self.ROOM)[0],
                    meld.latest_seed(chat.read(self.ROOM)[0])[0])
                rounds = [x for x in lines if x.startswith("  round e:")]
                self.assertTrue(rounds)
                self.assertLessEqual(len("\n".join(lines).encode()), budget)
                self.assertIn("helm chat read --room %s" % self.ROOM,
                              lines[-1])
                self.assertNotIn("x" * 100, "\n".join(lines))
                self.assertLess(len("\n".join(lines).encode()), whole // 5)
        with mock.patch.dict(os.environ,
                             {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "25000"}):
            lines = meld.join(self.ROOM, seat="seat-c")
        self.assertIn("12 earlier round(s) in this room", "\n".join(lines))
        self.assertIn("not shown here", "\n".join(lines))


class PairOutcomeTest(tm.MeldBase):
    """(d) and P4: what a round hands back to the row."""

    ROOM = "meld-0-pair-helm-task-3112"

    def seed(self, epoch, convener, invited,
             topic="task/3112 row %s at %s (chain %s)"
             % (ROOT[:12], TIP[:12], ROOT[:12])):
        chat.post("[MELD e:%d] PROBLEM: %s | round 1 | pairing: mixed | "
                  "convener=%s invited=%s cap=5 recv-timeout=90s | d [HOLD]"
                  % (epoch, topic, convener, invited),
                  room=self.ROOM, who=convener, sign=False)

    def done(self, epoch, who, text):
        chat.post("[MELD e:%d] %s [DONE]" % (epoch, text), room=self.ROOM,
                  who=who, sign=False)

    def test_d_an_AGREED_block_with_no_falsifier_set_has_not_converged(self):
        for blank in (None, "none", " - ", ";"):
            with self.subTest(falsifiers=blank):
                got, why = RD.parse_outcome(outcome(falsifiers=blank))
                self.assertIsNone(got)
                self.assertIn("FALSIFIERS", why)
        got, why = RD.parse_outcome(outcome("SPLIT", falsifiers=None))
        self.assertIsNone(why, "only AGREED owes a falsifier set")
        self.seed(1790000000, "author", "reader")
        for who in ("author", "reader"):
            self.done(1790000000, who, outcome(falsifiers=None))
        got = RD.room_outcome(self.ROOM)
        self.assertFalse(got["agreed"])
        self.assertIsNone(got["outcome"])
        fields, why = RD.meld_citation(
            self.ROOM, {"id": ROOT, "sender": "author", "recipient": "reader",
                        "chain_root": ROOT}, [TIP])
        self.assertIsNone(fields)
        self.assertIn("no readable MELD OUTCOME from every party", why)
        # the control: the same round, with its falsifier set, converges
        for who in ("author", "reader"):
            self.done(1790000000, who, outcome())
        self.assertTrue(RD.room_outcome(self.ROOM)["agreed"])

    def test_d_finding_dispositions_use_the_closed_vocabulary(self):  # noqa: VACUOUS_ASSERTION — both fixed non-empty case tables execute; every admitted disposition is asserted equal on the same parser output
        for findings in ("F1=banana", "F1", "=note", "F1="):
            with self.subTest(findings=findings):
                text = outcome().replace("F1=cured-in-patch", findings)
                got, why = RD.parse_outcome(text)
                self.assertIsNone(got)
                self.assertIn("FINDINGS", why)
                self.assertIn(RD.OUTCOME_LINE, why)
        for disposition in sorted(RD.FINDING_DISPOSITIONS):
            text = outcome().replace("cured-in-patch", disposition)
            got, why = RD.parse_outcome(text)
            self.assertIsNone(why)
            self.assertEqual(got["findings"], "F1=" + disposition)

    def test_d_a_round_with_no_open_finding_still_agrees(self):  # noqa: VACUOUS_ASSERTION — every placeholder parses to the one empty set and the two-placeholder round is asserted AGREED before the bare-id refusal
        """The plan round and a clean focused check name no finding, and
        FINDINGS is still a required field: a placeholder is the empty set,
        so two sides spelling it differently agree, and a bare id without
        a disposition is still refused."""
        for blank in ("none", "-", "n/a", "nil", " None ", "none;"):
            with self.subTest(findings=blank):
                text = outcome().replace("F1=cured-in-patch", blank)
                got, why = RD.parse_outcome(text)
                self.assertIsNone(why, why)
                self.assertEqual(RD._finding_pairs(got["findings"])[0], {})
        # a trailing separator is not a finding either, as before the
        # closed vocabulary; an empty id WITH a disposition still is refused
        got, why = RD.parse_outcome(
            outcome().replace("F1=cured-in-patch", "F1=cured-in-patch;"))
        self.assertIsNone(why, why)
        self.assertEqual(RD._finding_pairs(got["findings"])[0],
                         {"f1": "cured-in-patch"})
        self.seed(1790000000, "author", "reader")
        self.done(1790000000, "author",
                  outcome().replace("F1=cured-in-patch", "none"))
        self.done(1790000000, "reader",
                  outcome().replace("F1=cured-in-patch", "-"))
        got = RD.room_outcome(self.ROOM)
        self.assertTrue(got["agreed"], got["why"])
        fields, why = RD.meld_citation(
            self.ROOM, {"id": ROOT, "sender": "author", "recipient": "reader",
                        "chain_root": ROOT}, [TIP])
        self.assertIsNone(why, why)
        self.assertEqual(fields["meld_outcome"], "agreed")
        for bare in ("none; F1", "tbd", "?"):
            with self.subTest(findings=bare):
                got, why = RD.parse_outcome(
                    outcome().replace("F1=cured-in-patch", bare))
                self.assertIsNone(got)
                self.assertIn("FINDINGS", why)

    def test_d_two_falsifier_sets_are_two_bars(self):
        self.seed(1790000000, "author", "reader")
        self.done(1790000000, "author", outcome(falsifiers="x; y"))
        self.done(1790000000, "reader", outcome(falsifiers="x"))
        got = RD.room_outcome(self.ROOM)
        self.assertFalse(got["agreed"])
        self.assertIn("differ on FALSIFIERS", got["why"])

    def test_P4_the_newest_round_is_the_one_a_citation_reads(self):
        """Round one agreed with one reader; round two (a rebind) is with
        another. The room's answer is round two's, and round one stays
        readable by its epoch."""
        self.seed(1790000000, "author", "reader-1")
        for who in ("author", "reader-1"):
            self.done(1790000000, who, outcome())
        self.seed(1790000100, "author", "reader-2")
        got = RD.room_outcome(self.ROOM)
        self.assertEqual(got["parties"], ["author", "reader-2"])
        self.assertFalse(got["agreed"])
        _f, why = RD.meld_citation(
            self.ROOM, {"id": ROOT, "sender": "author", "recipient": "reader-1",
                        "chain_root": ROOT}, [TIP])
        self.assertIn("reader-2", why)
        for who in ("author", "reader-2"):
            self.done(1790000100, who, outcome(tip=OTHER))
        got = RD.room_outcome(self.ROOM)
        self.assertTrue(got["agreed"], got["why"])
        self.assertEqual((got["epoch"], got["tip"]), (1790000100, OTHER))
        old = RD.room_outcome(self.ROOM, epoch=1790000000)
        self.assertTrue(old["agreed"])
        self.assertEqual(old["tip"], TIP)

    def test_h_an_epochless_DONE_cannot_close_any_round(self):  # noqa: VACUOUS_ASSERTION — two positive seeds and two fixed epoch reads execute before each refusal assertion
        self.seed(1790000000, "author", "reader")
        self.seed(1790000100, "author", "reader")
        for who in ("author", "reader"):
            chat.post("%s [DONE]" % outcome(), room=self.ROOM, who=who,
                      sign=False)
        for epoch in (1790000000, 1790000100):
            with self.subTest(epoch=epoch):
                got = RD.room_outcome(self.ROOM, epoch=epoch)
                self.assertFalse(got["agreed"])
                self.assertIn("no [DONE]", got["why"])

    def test_h_a_round_the_room_moved_past_vouches_for_nothing(self):
        """EXACT-ROUND AUTHORITY at the rung: round one's AGREED converges
        while round one is the room's newest round, and exempts nothing once
        round two has opened, whoever it is with."""
        self.seed(1790000000, "author", "reader-1")
        for who in ("author", "reader-1"):
            self.done(1790000000, who, outcome())
        st = {"room": self.ROOM, "status": "done-mutual", "role": "joiner",
              "self": "reader-1", "peer": "author", "peers": ["author"],
              "epoch": 1790000000, "exchanges": 2, "spoke_peers": ["author"],
              "done_peers": ["author"]}
        self.assertTrue(RD.converged(st, "author", chain=ROOT, tips=[TIP]))
        self.seed(1790000100, "author", "reader-2")
        self.assertFalse(RD.converged(st, "author", chain=ROOT, tips=[TIP]))
        # round two agreeing too, on the same tip, does not make round one's
        # state speak for it
        for who in ("author", "reader-2"):
            self.done(1790000100, who, outcome())
        self.assertTrue(RD.room_outcome(self.ROOM)["agreed"])
        self.assertFalse(RD.converged(st, "author", chain=ROOT, tips=[TIP]))


class PairCensusTest(tm.MeldBase):
    """(a) and (g), measured off planted rows and rooms."""

    def plant(self, n, agreed, opened=True, recorded=True):
        rid = ("%032x" % (n + 1))
        r = {"id": rid, "chain_root": rid, "lane": "task-%d" % (9000 + n),
             "repo_id": "/x/helm/.git", "sender": "a", "recipient": "b",
             "ts": pk.now_ts(), "brief_bytes": 100}
        room = RD.pair_room(r, {rid: r})[0]
        if opened:
            chat.post("[MELD e:1790000000] PROBLEM: task/%d | pairing: mixed "
                      "(claude + codex) | convener=a invited=b cap=5 "
                      "recv-timeout=90s | d [HOLD]" % (9000 + n), room=room,
                      who="a", sign=False)
        if agreed and recorded:
            r.update(meld_room=room, meld_outcome="agreed", meld_bytes=500)
        return r

    def census(self, rows):
        return RD.pair_census(rows, {r["id"]: r for r in rows})

    def test_a_the_bar_is_UNMEASURED_under_20_tasks_and_judged_at_20(self):
        rows = [self.plant(n, agreed=n < 12) for n in range(19)]
        got = self.census(rows)
        self.assertEqual((got["tasks"], got["agreed"]), (19, 12))
        self.assertTrue(got["reading"].startswith("UNMEASURED"))
        rows.append(self.plant(19, agreed=False))
        got = self.census(rows)
        self.assertEqual((got["tasks"], got["agreed"]), (20, 12))
        self.assertEqual(got["reading"], "HOLDS")
        self.assertEqual(got["pairings"], {"mixed": 20})
        rows[0].pop("meld_outcome")          # 11 of 20
        got = self.census(rows)
        self.assertTrue(got["reading"].startswith("FALSIFIED"), got)

    def test_a_only_an_opened_room_and_a_recorded_agreement_count(self):  # noqa: VACUOUS_ASSERTION — the census counts are asserted EQUAL to (2, 1), positive values on the census observable
        rows = [self.plant(0, agreed=True),
                self.plant(1, agreed=False, opened=False),
                self.plant(2, agreed=True, recorded=False)]
        got = self.census(rows)
        self.assertEqual((got["tasks"], got["agreed"]), (2, 1))

    def test_g_cost_compares_pair_converged_with_rows_alone(self):  # noqa: VACUOUS_ASSERTION — every cost field is asserted EQUAL to a planted positive value
        pair = self.plant(0, agreed=True)
        alone = {"id": "f" * 32, "chain_root": "f" * 32, "lane": "no-task",
                 "repo_id": "/x/helm/.git", "polarity": "approve",
                 "brief_bytes": 300, "ts": pk.now_ts()}
        got = self.census([pair, alone])["cost"]
        self.assertEqual((got["pair_converged"], got["pair_median_bytes"]),
                         (1, 600))
        self.assertEqual((got["row_only_converged"],
                          got["row_only_median_bytes"]), (1, 300))


class _PairDoorBase(trd.DoorBase):
    """The review door's CLI fixture, with the READER holding its own rows
    (the source-clean door admits only the recipient)."""

    def setUp(self):
        super().setUp()
        self.holder = "seat-b"
        real = dispatches._acting_author

        def acting(action="author this dispatch"):
            if action == "hold this row":
                return self.holder, None
            return real(action)
        patch = mock.patch.object(dispatches, "_acting_author", acting)
        patch.start()
        self.addCleanup(patch.stop)

    def numbered_task(self, n):
        self.review_task, err = tasks.add(
            "fixture pair %d" % n, "author", tid=n,
            project="helm-test", force_new=True)
        self.assertIsNone(err, err)

    def seeds(self, room):
        return meld.seeds(chat.read(room)[0])

    def delivery(self, sent):
        return dispatches.snapshot()[0][sent["id"]].get("delivery")


class PairDispatchTest(_PairDoorBase):
    """P1, P2, P4, P6, (b), (c), (e), (g) through the real CLI, ledger and
    fold. The author is `integrator`; the reader is `seat-b`."""

    def test_P1_the_first_dispatch_opens_the_plan_round(self):
        self.numbered_task(7001)
        self.lane = "pair-meld-per-task-7001"
        rc, out, err, sent = self.send(None, self.a, kind="build",
                                       body="build the pair meld")
        self.assertEqual(rc, 0, err)
        room = self.pair_room(sent)
        self.assertEqual(room, RD.pair_scope(sent) + "-7001")
        self.assertIn("your pair meld for this task: %s (task/7001, round 1; "
                      "you and @seat-b" % room, out)
        self.assertIn("helm chat meld recv %s" % room, out)
        seed = self.latest_seed(room)
        for part in ("ROUND 1 IS THE PLAN", "INVARIANTS", "ACCEPTANCE CHECKS",
                     "SPLIT", "OWNS the combined result",
                     "convener=integrator invited=seat-b",
                     "(chain %s)" % sent["chain_root"][:12]):
            self.assertIn(part, seed)

    def test_P1_a_same_family_pair_says_so_and_a_mixed_one_is_named(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a fixed two-case tuple and each case asserts assertIn on the printed and seeded pairing
        cases = (("pair-meld-per-task-7011", lambda s: {"claude"},
                  "SAME FAMILY (claude): this is NOT a mixed-family pair"),
                 ("pair-meld-per-task-7012",
                  lambda s: {"integrator": {"claude"},
                             "seat-b": {"codex"}}.get(s, set()),
                  "mixed (claude + codex)"))
        for lane, families, said in cases:
            with self.subTest(lane=lane), mock.patch.object(
                    dispatches, "_pair_families",
                    side_effect=lambda seat, families=families:
                    (set(families(seat) or ()), "PROVEN")):
                self.numbered_task(int(lane.rsplit("-", 1)[1]))
                self.lane = lane
                rc, out, err, sent = self.send(None, self.a)
                self.assertEqual(rc, 0, err)
                self.assertIn("pairing: " + said, out)
                self.assertIn("pairing: " + said,
                              self.latest_seed(self.pair_room(sent)))

    def test_P1_an_unproven_runtime_uses_and_labels_its_durable_declaration(self):
        self.numbered_task(7013)
        self.lane = "pair-meld-per-task-7013"
        answers = {"integrator": ({"claude"}, "PROVEN"),
                   "seat-b": ({"codex"}, "DECLARED")}
        with mock.patch.object(dispatches, "_pair_families",
                               side_effect=lambda seat: answers[seat]):
            rc, out, err, sent = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        said = "mixed (claude + codex DECLARED; runtime unproven for seat-b)"
        self.assertIn("pairing: " + said, out)
        self.assertIn("pairing: " + said,
                      self.latest_seed(self.pair_room(sent)))

    def test_P1_a_runtime_disagreement_never_falls_back_to_declared(self):
        answers = {"integrator": ({"claude"}, "PROVEN"),
                   "seat-b": (set(), "DISAGREEMENT")}
        with mock.patch.object(dispatches, "_pair_families",
                               side_effect=lambda seat: answers[seat]):
            label, text = RD.pairing("integrator", "seat-b")
        self.assertEqual(label, "unknown")
        self.assertEqual(text, "family DISAGREEMENT for seat-b")

    def test_P1_only_a_verified_durable_roster_runtime_is_declared(self):
        runtime = {"family": "codex", "backend": "proxy"}
        roster = {"Seat-B": {"session": "sid", "runtime": runtime,
                              "runtime_verified": True}}
        with mock.patch.object(seats, "roster_checked",
                               return_value=(roster, False)):
            self.assertEqual(dispatches._declared_pair_family("seat-b"),
                             "codex")
            roster["Seat-B"]["runtime_verified"] = False
            self.assertIsNone(dispatches._declared_pair_family("seat-b"))

    def test_P1_the_declared_fallback_stops_at_measured_disagreement(self):
        dark = dispatches.TierUnknown(dispatches.TIER_DARK, "no live proof")
        with mock.patch.object(
                dispatches, "_approval_identity_family_evidence",
                return_value=(None, None, None, dark)), mock.patch.object(
                    dispatches, "_declared_pair_family", return_value="codex"):
            self.assertEqual(dispatches._pair_families("seat-b"),
                             ({"codex"}, "DECLARED"))
        damaged = dispatches.TierUnknown(dispatches.TIER_DAMAGED,
                                         "runtime contradicts roster")
        with mock.patch.object(
                dispatches, "_approval_identity_family_evidence",
                return_value=(None, None, None, damaged)), mock.patch.object(
                    dispatches, "_declared_pair_family", return_value="codex"):
            self.assertEqual(dispatches._pair_families("seat-b"),
                             (set(), "DISAGREEMENT"))

    def test_P2_b_every_round_of_a_chain_opens_in_its_one_room(self):  # noqa: VACUOUS_ASSERTION — the three rows' rooms are asserted EQUAL to one named room and its seed count to three; the empty other-room list is the falsifier (b) absence beside them
        self.numbered_task(7002)
        self.lane = "pair-meld-per-task-7002"
        rc, _out, err, one = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        room = self.pair_room(one)
        self.fix(one)
        self.lane = "renamed-lane-r2"
        rc, _out, err, two = self.send(one, self.b)
        self.assertEqual(rc, 0, err)
        self.fix(two, path="helm/b.py")
        self.beacon = True
        rc, out, err, three = self.send(two, self.c)
        self.assertEqual(rc, 0, err)
        self.assertEqual(three["meld_door"]["action"], "auto-open")
        self.assertEqual([self.pair_room(r) for r in (one, two, three)],
                         [room] * 3)
        self.assertEqual(len(self.seeds(room)), 3)
        self.assertIn("agree the bar", self.latest_seed(room))
        self.assertIn("MELD OPENED %s" % room, out)
        self.assertEqual(self.rooms(), [], "a second room for one chain")
        mine = [f for f in os.listdir(chat.chat_dir()) if ".meld." in f
                and RD.is_pair_room(f.split(".meld.", 1)[0])]
        self.assertEqual(mine, [f for f in mine
                                if f.startswith(room + ".meld.")])
        self.assertEqual(len(mine), 1, mine)

    def test_P6_the_dispatch_DM_is_the_only_ring(self):
        self.numbered_task(7003)
        self.lane = "pair-meld-per-task-7003"
        from helm import seats
        with mock.patch.object(seats, "dm",
                               return_value=({"id": "dm-1"}, None)) as dm:
            rc, _out, err, sent = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        room = self.pair_room(sent)
        dm.assert_called_once()
        text = dm.call_args[0][1]
        # the DM opens with the row's own header — its full id and lane —
        # ahead of the brief (task/3300); the body is what follows
        first = text.split("\n", 1)[0]
        self.assertTrue(first.startswith("YOUR ROW: " + sent["id"]),
                        text[:60])
        self.assertIn(self.lane, first)
        self.assertTrue(text.startswith(first + "\n\nread this tip"),
                        text[:80])
        self.assertIn("PAIR MELD for task/7003: %s" % room, text)
        self.assertIn("helm chat meld join %s" % room, text)
        rows = chat.read(room)[0]
        self.assertTrue(rows)
        self.assertFalse([m for m in rows if "@seat-b" in m["text"]],
                         "the pair room rang the reader a second time")

    def test_c_a_reader_with_no_live_beacon_is_never_blocked_or_waited_on(self):  # noqa: VACUOUS_ASSERTION — absence of an owed turn IS falsifier (c); delivery is asserted observed and the reader's FIX recorded on the same rows
        self.beacon = False
        self.lane = "pair-meld-per-task-7004"
        rc, out, err, sent = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.delivery(sent), "observed")
        self.assertEqual(RD.pair_turns_owed("seat-b"), [])
        self.assertEqual(RD.pair_turns_owed("integrator"), [])
        self.fix(sent)        # the reader answers on the row, never joining
        self.assertEqual(dispatches.snapshot()[0][sent["id"]]["polarity"],
                         "fix")
        # a round that cannot open leaves the row as the conversation
        self.lane = "pair-meld-per-task-7005"
        with mock.patch.object(meld, "invite",
                               side_effect=RuntimeError("chat dir gone")):
            rc, out, err, sent = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        self.assertIn("pair meld round could not open", out)
        self.assertEqual(self.delivery(sent), "observed")
        self.assertEqual(sent["status"], "open")

    def test_e_a_rebind_invites_the_new_reader_into_the_same_room(self):  # noqa: VACUOUS_ASSERTION — the room's seeds are asserted to grow to two then three naming the new readers; the old reader's empty owed list is the superseded-round absence
        self.numbered_task(7006)
        self.lane = "pair-meld-per-task-7006"
        rc, _out, err, one = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        room = self.pair_room(one)
        # the rebind door moves work only to a seat proven joined
        from helm import seats
        seats.write_roster("seat-c")
        seats.write_roster("seat-d")
        rc, out, err = run(dispatches.cmd_dispatch, [
            "rebind", one["id"], "--to", "seat-c", "--force", "--reason",
            "seat-b went dark"])
        self.assertEqual(rc, 0, err)
        self.assertIn("your pair meld for this task: %s" % room, out)
        seeds = self.seeds(room)
        self.assertEqual(len(seeds), 2)
        self.assertIn("convener=integrator invited=seat-c", seeds[-1][2])
        snap = dispatches.snapshot()[0]
        new = [r for r in snap.values() if r.get("supersedes") == one["id"]]
        self.assertEqual(len(new), 1)
        self.assertEqual((new[0]["recipient"], new[0]["status"]),
                         ("seat-c", "open"), "the row stays owed")
        lines = meld.join(room, seat="seat-c")
        self.assertIn("1 earlier round(s) in this room", "\n".join(lines))
        self.assertEqual(RD.pair_turns_owed("seat-b"), [])
        # the automated reassign path continues the open conversation too
        out, why = dispatches.rebind(new[0]["id"], "seat-d",
                                     reason="seat-c went dark", force=True)
        self.assertIsNone(why)
        self.assertEqual(len(self.seeds(room)), 3)
        self.assertIn("invited=seat-d", self.seeds(room)[-1][2])

    def test_P4_g_an_AGREED_round_lands_on_the_row_with_its_cost(self):
        self.lane = "pair-meld-per-task-7007"
        rc, _out, err, sent = self.send(None, self.b)
        self.assertEqual(rc, 0, err)
        room = self.pair_room(sent)
        epoch = meld.latest_seed(chat.read(room)[0])[0]
        for who in ("integrator", "seat-b"):
            chat.post("[MELD e:%d] %s [DONE]" % (epoch, outcome(tip=self.b)),
                      room=room, who=who, sign=False)
        size = sum(len(m["text"].encode()) for m in chat.read(room)[0])
        rc, out, err = run(dispatches.cmd_dispatch, [
            "hold", sent["id"], "read clean; fab Ran 5 tests OK", "--source-clean", self.b,
            "--meld", room])
        self.assertEqual(rc, 0, err)
        self.assertIn("meld %s — AGREED" % room, out)
        held = dispatches.snapshot()[0][sent["id"]]
        self.assertEqual((held["meld_room"], held["meld_outcome"],
                          held["meld_bytes"]), (room, "agreed", size))
        got = RD.census()["pair"]
        self.assertEqual((got["tasks"], got["agreed"]), (1, 1))
        self.assertEqual(got["cost"]["pair_converged"], 1)
        self.assertGreaterEqual(got["cost"]["pair_median_bytes"], size)
        rc, out, err = run(dispatches.cmd_dispatch, ["melds"])
        self.assertEqual(rc, 0, err)
        self.assertIn("(a) tasks whose pair meld reached AGREED on a row: "
                      "1 of 1", out)
        self.assertIn("(g) bytes per converged chain", out)


class PairLifecycleWalkTest(_PairDoorBase):
    """ONE CHAIN, ITS WHOLE LIFE, one room: the first dispatch, a FIX, the
    cure, the re-read at the reviewer's patch, a rebind off a reader that
    went dark, the new reader's join with the history, the AGREED round the
    reader records on the row, the census, the room's archival, and a round
    after it reborn under the same name."""

    def test_the_room_lives_as_long_as_the_chain(self):  # noqa: VACUOUS_ASSERTION — every stage asserts a positive value on the room or row it moves; the empty other-room list is falsifier (b)
        self.numbered_task(7100)
        self.lane = "pair-meld-per-task-7100"
        rc, _out, err, one = self.send(None, self.a)           # first
        self.assertEqual(rc, 0, err)
        room = self.pair_room(one)
        self.fix(one)                                           # FIX
        rc, _out, err, two = self.send(one, self.b)             # the cure
        self.assertEqual(rc, 0, err)
        self.fix(two, path="helm/b.py", patch_tip=self.c)
        rc, _out, err, three = self.send(two, self.c)           # re-read
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.seeds(room)), 3)
        # reader dark: the automated reassign path, which continues the
        # chain's open pair meld without being asked
        moved, why = dispatches.rebind(three["id"], "seat-c",
                                       reason="seat-b went dark", force=True)
        self.assertIsNone(why)
        four = moved["new"]
        self.assertEqual(four["status"], "open")
        self.assertEqual(self.rooms(), [])
        joined = "\n".join(meld.join(room, seat="seat-c"))
        self.assertIn("3 earlier round(s) in this room", joined)
        epoch = meld.state(room, "seat-c")["epoch"]
        for who in ("integrator", "seat-c"):                    # converge
            chat.post("[MELD e:%d] %s [DONE]" % (epoch, outcome(tip=self.c)),
                      room=room, who=who, sign=False)
        self.holder = "seat-c"
        rc, out, err = run(dispatches.cmd_dispatch, [           # the row
            "hold", four["id"], "read clean; fab Ran 5 tests OK", "--source-clean", self.c,
            "--meld", room])
        self.assertEqual(rc, 0, err)
        self.assertEqual(dispatches.snapshot()[0][four["id"]]["meld_outcome"],
                         "agreed")
        got = RD.census()["pair"]
        self.assertEqual((got["tasks"], got["agreed"]), (1, 1))
        report, why = chatdebris.retire_room(                   # archival
            room, idle_days=0, now=time.time() + 60)
        self.assertIsNone(why, report)
        self.assertNotIn(room, chat.list_rooms())
        self.assertEqual(RD.census()["pair"]["tasks"], 1,
                         "a retired room is still a task that opened")
        four = dispatches.snapshot()[0][four["id"]]
        # reborn, on a tip that still carries the reviewer's cure (task/3288)
        rc, _out, err, five = self.send(four, self.branch("reborn", self.c))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.pair_room(five), room)
        self.assertIn(room, chat.list_rooms())


class PairTurnRungTest(tss.SpiralBase):
    """P5: the stop guard names a pair round whose floor is the stopping
    seat's, and nothing else."""

    ROOM = "meld-0-pair-helm-task-3112"

    def open_round(self, peer="seat-b"):
        meld.invite(peer, "the plan", seat="seat-a", room=self.ROOM,
                    ring=RING)

    def test_P5_the_stop_guard_names_a_round_whose_floor_is_yours(self):  # noqa: VACUOUS_ASSERTION — the block is asserted to name the room and the turn twice before the latched and HOLD absences
        self.open_round()
        meld.join(self.ROOM, seat="seat-b")
        block, _warn = self.text(seat="seat-b", session="s-b")
        self.assertNotIn(self.ROOM, block, "the convener still holds it")
        block, _warn = self.text(seat="seat-a", session="s-a")
        self.assertIn(self.ROOM, block)
        self.assertIn("joined and is waiting for your first chunk", block)
        meld.recv(self.ROOM, timeout=0, seat="seat-a", poll=0.01)
        meld.say(self.ROOM, "YIELD", "the plan: problem, invariants",
                 seat="seat-a")
        block, _warn = self.text(seat="seat-b", session="s-b")
        self.assertIn("yielded the floor to you", block)
        self.assertIn("helm chat meld recv %s" % self.ROOM, block)
        again, _warn = self.text(seat="seat-b", session="s-b")
        self.assertNotIn(self.ROOM, again, "one block per set of turns")
        meld.recv(self.ROOM, timeout=0, seat="seat-b", poll=0.01)
        meld.recv(self.ROOM, timeout=0, seat="seat-b", poll=0.01)
        meld.say(self.ROOM, "YIELD", "agreed on one to three",
                 seat="seat-b")
        meld.recv(self.ROOM, timeout=0, seat="seat-a", poll=0.01)
        meld.say(self.ROOM, "HOLD", "more coming", seat="seat-a")
        self.assertEqual(RD.pair_turns_owed("seat-b"), [])
        self.assertEqual(RD.pair_turns_owed("seat-a"), [])

    def test_a_round_the_room_moved_past_owes_nothing(self):
        self.open_round()
        meld.join(self.ROOM, seat="seat-b")
        meld.recv(self.ROOM, timeout=0, seat="seat-a", poll=0.01)
        meld.say(self.ROOM, "YIELD", "the plan", seat="seat-a")
        self.assertEqual([o["peer"] for o in RD.pair_turns_owed("seat-b")],
                         ["seat-a"])
        self.open_round(peer="seat-c")       # the rebind's round
        self.assertEqual(RD.pair_turns_owed("seat-b"), [])

    def test_c_a_seat_that_never_joined_owes_nothing(self):  # noqa: VACUOUS_ASSERTION — test_P5_the_stop_guard_names_a_round_whose_floor_is_yours is the positive control on the same rung
        self.open_round()
        for seat in ("seat-a", "seat-b"):
            self.assertEqual(RD.pair_turns_owed(seat), [])
            block, _warn = self.text(seat=seat, session="s-" + seat)
            self.assertNotIn(self.ROOM, block)

    def test_the_switch_disables_it(self):  # noqa: VACUOUS_ASSERTION — test_P5_the_stop_guard_names_a_round_whose_floor_is_yours is the positive control on the same rung
        self.open_round()
        meld.join(self.ROOM, seat="seat-b")
        with mock.patch.dict(os.environ, {"HELM_STOP_GUARD_PAIR": "0"}):
            block, _warn = self.text(seat="seat-a", session="s-a")
        self.assertNotIn(self.ROOM, block)


CHAIN = "c0" * 16
SPIRAL_ROOM = "meld-0-pair-helm-chain-" + CHAIN[:12]


class RulingSpiralWindowTest(tss.SpiralBase):
    """Ruling 2: a pair round counts toward the spiral rung only when the
    room holds a turn from BOTH the author and the CURRENT reader inside the
    rung's window, and that reader's beacon is live. A quiet auto-opened round
    never silences the rung, however long it has sat."""

    def open_round(self, peer="codex"):
        meld.invite(peer, "the task (chain %s): a round" % CHAIN[:12],
                    seat=tss.SEAT, room=SPIRAL_ROOM, ring=RING)

    def exchange(self, peer="codex", reader_speaks=True):
        self.open_round(peer)
        meld.join(SPIRAL_ROOM, seat=peer)
        meld.recv(SPIRAL_ROOM, timeout=0, seat=tss.SEAT, poll=0.01)
        meld.say(SPIRAL_ROOM, "YIELD", "the plan", seat=tss.SEAT)
        if reader_speaks:
            self.reader_turn(peer)

    def reader_turn(self, peer="codex"):
        meld.recv(SPIRAL_ROOM, timeout=0, seat=peer, poll=0.01)
        meld.recv(SPIRAL_ROOM, timeout=0, seat=peer, poll=0.01)
        meld.say(SPIRAL_ROOM, "YIELD", "agreed on one to three", seat=peer)

    def gate(self, session, live):
        """The spiral rung alone, as the stopping author, with the reader's
        beacon answered by the test. A fresh session per call, because the
        rung latches per (chain, rounds, prescription) and session."""
        from helm import seats_stop_spiral as spiral
        with mock.patch.object(RD, "live_beacon", return_value=live):
            return spiral._spiral_gate(session, "main", tss.SEAT)

    def test_R2_a_quiet_auto_opened_round_still_walls(self):
        """The dispatch opened the round and nobody spoke in it: past the
        entry window it is still no conversation, so the block stands."""
        self.rounds(3, chain_root=CHAIN)
        self.open_round()
        with mock.patch.dict(os.environ, {"HELM_MELD_ENTRY_WINDOW_S": "0"}):
            block, _warn = self.gate("s-quiet", live=True)
        self.assertIn("review spiral", block or "")
        self.assertIn(SPIRAL_ROOM, block)

    def test_R2_a_round_with_both_turns_inside_the_window_stops_walling(self):  # noqa: VACUOUS_ASSERTION — the absent block is the ruling's contract; the warn is asserted to name the room and the reader on the same call
        self.rounds(3, chain_root=CHAIN)
        self.exchange()
        block, warn = self.gate("s-talking", live=True)
        self.assertIsNone(block)
        self.assertIn("holds turns from you and codex", warn or "")
        self.assertIn(SPIRAL_ROOM, warn)

    def test_R2_a_reader_without_a_turn_or_a_live_beacon_never_silences(self):  # noqa: VACUOUS_ASSERTION — the last call on the same room is the positive control: the same exchange, reader live, does silence
        self.rounds(3, chain_root=CHAIN)
        self.exchange(reader_speaks=False)
        block, _warn = self.gate("s-mute", live=True)
        self.assertIn("review spiral", block or "", "the author alone")
        self.reader_turn()
        for session, live in (("s-dark", False), ("s-unread", None)):
            with self.subTest(beacon=live):
                block, _warn = self.gate(session, live=live)
                self.assertIn("review spiral", block or "")
        block, _warn = self.gate("s-lit", live=True)
        self.assertIsNone(block)

    def test_R2_an_agreed_pair_round_whose_reader_is_dark_never_silences(self):  # noqa: VACUOUS_ASSERTION — the live-reader call on the same agreed round is the positive control
        """The round converged AGREED, and the reader has since gone dark:
        a pair round whose reader has no live beacon never silences the
        rung, whatever it agreed."""
        self.rounds(3, chain_root=CHAIN)
        self.exchange()
        tips = [r["tip"] for r in dispatches.snapshot()[0].values()]
        agreed = outcome(tip=tips[0])
        meld.recv(SPIRAL_ROOM, timeout=0, seat=tss.SEAT, poll=0.01)
        meld.say(SPIRAL_ROOM, "DONE", agreed, seat=tss.SEAT)
        meld.recv(SPIRAL_ROOM, timeout=0, seat="codex", poll=0.01)
        meld.say(SPIRAL_ROOM, "DONE", agreed, seat="codex")
        meld.recv(SPIRAL_ROOM, timeout=0, seat=tss.SEAT, poll=0.01)
        self.assertEqual(meld.state(SPIRAL_ROOM, tss.SEAT)["status"],
                         "done-mutual")
        self.assertTrue(RD.room_outcome(SPIRAL_ROOM)["agreed"])
        block, _warn = self.gate("s-agreed-dark", live=False)
        self.assertIn("review spiral", block or "")
        block, _warn = self.gate("s-agreed-live", live=True)
        self.assertIsNone(block)

    def test_R2_a_turn_from_a_reader_who_is_not_the_current_one_never_silences(self):
        """The chain's current reader is `codex`; the room's exchange was
        with the reader before it."""
        self.rounds(3, chain_root=CHAIN)
        self.exchange(peer="kimi")
        block, _warn = self.gate("s-old-reader", live=True)
        self.assertIn("review spiral", block or "")

    def test_R2_an_exchange_for_another_chain_in_the_task_room_is_not_mine(self):  # noqa: VACUOUS_ASSERTION — the other chain's positive room result controls the same call before this chain's absence is asserted
        room = "helm-3112"
        other = "d0" * 16
        other_row = row(rid=other, root=other,
                        lane="task-3112 inject (chain %s)" % CHAIN[:12],
                        sender=tss.SEAT, recipient="codex")
        opened = RD.open_pair_round(other_row, current={other: other_row},
                                    families=lambda _seat: {"fixture"})
        self.assertEqual(opened["room"], room)
        self.assertEqual(RD._seed_chain(opened["topic"]), other[:12])
        meld.join(room, seat="codex")
        meld.recv(room, timeout=0, seat=tss.SEAT, poll=0.01)
        meld.say(room, "YIELD", "author turn", seat=tss.SEAT)
        meld.recv(room, timeout=0, seat="codex", poll=0.01)
        meld.recv(room, timeout=0, seat="codex", poll=0.01)
        meld.say(room, "YIELD", "reader turn", seat="codex")
        self.assertEqual(RD.pair_exchange(tss.SEAT, "codex", other, 1), room)
        self.assertIsNone(RD.pair_exchange(tss.SEAT, "codex", CHAIN, 1))

    def test_R2_turns_from_two_different_rounds_are_not_one_exchange(self):
        self.rounds(3, chain_root=CHAIN)
        self.open_round()
        meld.join(SPIRAL_ROOM, seat="codex")
        meld.recv(SPIRAL_ROOM, timeout=0, seat=tss.SEAT, poll=0.01)
        meld.say(SPIRAL_ROOM, "YIELD", "author spoke only here", seat=tss.SEAT)
        self.open_round()
        epoch = meld.state(SPIRAL_ROOM, tss.SEAT)["epoch"]
        chat.post("[MELD e:%d] reader spoke only here [YIELD]" % epoch,
                  room=SPIRAL_ROOM, who="codex", sign=False)
        block, _warn = self.gate("s-split-rounds", live=True)
        self.assertIn("review spiral", block or "")


class RulingExactRoundTest(_PairDoorBase):
    """Falsifier (h), EXACT-ROUND AUTHORITY: a round's MELD OUTCOME closes and
    exempts only its own round. `--meld ROOM@EPOCH` cites a named round, and
    the unqualified `--meld ROOM` keeps reading the newest."""

    def agree(self, room, epoch, tip, parties=("integrator", "seat-b")):
        for who in parties:
            chat.post("[MELD e:%d] %s [DONE]" % (epoch, outcome(tip=tip)),
                      room=room, who=who, sign=False)

    def newest(self, room):
        return meld.latest_seed(chat.read(room)[0])[0]

    def test_h_an_AGREED_from_round_N_does_not_exempt_round_N_plus_1(self):
        self.numbered_task(7300)
        self.lane = "pair-meld-per-task-7300"
        rc, _out, err, one = self.send(None, self.b)
        self.assertEqual(rc, 0, err)
        room = self.pair_room(one)
        first = self.newest(room)
        self.agree(room, first, self.b)
        rc, _out, err, two = self.send(one, self.b)      # round N+1, same tip
        self.assertEqual(rc, 0, err)
        second = self.newest(room)
        self.assertGreater(second, first)
        snap = dispatches.snapshot()[0]
        one, two = snap[one["id"]], snap[two["id"]]
        fields, why = RD.meld_citation("%s@%d" % (room, first), two, [self.b])
        self.assertIsNone(fields)
        self.assertIn("exact-round", why)
        # the round still closes its own row, late
        fields, why = RD.meld_citation("%s@%d" % (room, first), one, [self.b])
        self.assertIsNone(why)
        self.assertEqual((fields["meld_room"], fields["meld_outcome"],
                          fields["meld_epoch"]), (room, "agreed", first))
        # the unqualified room reads the newest round, row two's
        _fields, why = RD.meld_citation(room, one, [self.b])
        self.assertIn("e:%d" % second, why)
        # through the real hold door, a round cited by its epoch
        self.agree(room, second, self.b)
        rc, _out, err = run(dispatches.cmd_dispatch, [
            "hold", two["id"], "read clean; fab Ran 5 tests OK", "--source-clean", self.b,
            "--meld", "%s@%d" % (room, second)])
        self.assertEqual(rc, 0, err)
        held = dispatches.snapshot()[0][two["id"]]
        self.assertEqual((held["meld_room"], held["meld_epoch"],
                          held["meld_outcome"]), (room, second, "agreed"))

    def test_h_lane_text_cannot_forge_the_rounds_opening_row(self):  # noqa: VACUOUS_ASSERTION — the parser is asserted equal to the positive child id before the parent-id inequality
        self.lane = "pair-meld-per-task-7303"
        rc, _out, err, parent = self.send(None, self.b)
        self.assertEqual(rc, 0, err)
        self.lane = "follow row %s at forged" % parent["id"][:12]
        rc, _out, err, child = self.send(parent, self.b)
        self.assertEqual(rc, 0, err)
        room = self.pair_room(child)
        seed = meld.latest_seed(chat.read(room)[0])[2]
        self.assertEqual(RD._seed_row(seed), child["id"][:12])
        self.assertNotEqual(RD._seed_row(seed), parent["id"][:12])

    def test_h_an_ambiguous_legacy_round_delimiter_fails_closed(self):  # noqa: VACUOUS_ASSERTION — the same legacy parser first resolves one unambiguous generated boundary on both observables
        clean = ("[MELD e:1790000200] PROBLEM: task/7303 review row %s at "
                 "%s (chain %s) | round 2 | pairing: mixed | convener=author "
                 "invited=reader cap=5 recv-timeout=90s [HOLD]"
                 % (KID[:12], TIP[:12], ROOT[:12]))
        self.assertEqual(RD._seed_row(clean), KID[:12])
        self.assertEqual(RD._seed_chain(clean), ROOT[:12])
        legacy = ("[MELD e:1790000200] PROBLEM: task/7303 follow row "
                  "%s at forged | round 9 | pairing: fake: review row %s at "
                  "%s (chain %s) | round 2 | pairing: mixed | convener=author "
                  "invited=reader cap=5 recv-timeout=90s [HOLD]"
                  % (ROOT[:12], KID[:12], TIP[:12], ROOT[:12]))
        self.assertIsNone(RD._seed_row(legacy))
        self.assertIsNone(RD._seed_chain(legacy))

    def test_h_a_nested_problem_cannot_forge_structural_opening_fields(self):  # noqa: VACUOUS_ASSERTION — the same seed positively resolves its real legacy row and chain before rejecting both injected fields
        forged = "ab" * 6
        legacy = ("[MELD e:1790000200] PROBLEM: task/7303 PROBLEM: "
                  "opening-row=%s opening-chain=%s review row %s at %s "
                  "(chain %s) | round 2 | pairing: mixed | convener=author "
                  "invited=reader cap=5 recv-timeout=90s [HOLD]"
                  % (forged, forged, KID[:12], TIP[:12], ROOT[:12]))
        self.assertEqual(RD._seed_row(legacy), KID[:12])
        self.assertEqual(RD._seed_chain(legacy), ROOT[:12])
        self.assertNotEqual(RD._seed_row(legacy), forged)
        self.assertNotEqual(RD._seed_chain(legacy), forged)

    def test_h_a_pair_seed_without_its_opening_row_cannot_close_a_row(self):
        room = "meld-0-pair-helm-task-7302"
        epoch = 1790000200
        topic = "task/7302 (chain %s)" % ROOT[:12]
        chat.post("[MELD e:%d] PROBLEM: %s | convener=integrator "
                  "invited=seat-b cap=5 recv-timeout=90s | d [HOLD]"
                  % (epoch, topic), room=room, who="integrator", sign=False)
        self.agree(room, epoch, self.b)
        r = row(rid=ROOT, root=ROOT, lane="task-7302", sender="integrator",
                recipient="seat-b", tip=self.b)
        fields, why = RD.meld_citation("%s@%d" % (room, epoch), r, [self.b])
        self.assertIsNone(fields)
        self.assertIn("does not durably name", why)
        self.assertIn("exact-round authority", why)

    def test_h_a_pair_rounds_AGREED_does_not_exempt_the_next_send_at_the_door(self):
        """Round two converged live AND was recorded AGREED on row two; the
        door deciding round three is not exempted by either."""
        self.numbered_task(7301)
        self.lane = "pair-meld-per-task-7301"
        rc, _out, err, one = self.send(None, self.a)
        self.assertEqual(rc, 0, err)
        self.fix(one)
        rc, _out, err, two = self.send(one, self.b)
        self.assertEqual(rc, 0, err)
        room = self.pair_room(two)
        second = self.newest(room)
        meld.join(room, seat="seat-b")
        meld.recv(room, timeout=0, seat="integrator", poll=0.01)
        meld.say(room, "YIELD", "the bar", seat="integrator")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
        meld.say(room, "DONE", outcome(tip=self.b), seat="seat-b")
        meld.recv(room, timeout=0, seat="integrator", poll=0.01)
        meld.say(room, "DONE", outcome(tip=self.b), seat="integrator")
        self.assertTrue(RD.room_outcome(room)["agreed"])
        self.fix(two, path="helm/b.py", patch_tip=self.c,
                 meld_room="%s@%d" % (room, second))
        self.assertEqual(dispatches.snapshot()[0][two["id"]]["meld_outcome"],
                         "agreed")
        nxt = self.commit("round-three")
        door = RD.plan("review", "integrator", self.READER, self.lane, nxt,
                       supersedes=two["id"], beacon=lambda peer: True)
        self.assertEqual(door["trigger"], "T2")
        self.assertEqual(door["action"], "auto-open",
                         "an AGREED from round N exempted round N+1")


class RulingOutcomeLineTest(unittest.TestCase):
    """Ruling 5: every AGREED names its FALSIFIERS, and a refused block names
    what it lacks and prints the five-field line that fixes it."""

    def test_R5_a_refused_block_names_the_field_and_prints_the_five_field_fix(self):
        for text, field in ((outcome(falsifiers=None), "FALSIFIERS"),
                            (outcome().replace("BAR: the one harm | ", ""),
                             "BAR")):
            with self.subTest(field=field):
                got, why = RD.parse_outcome(text)
                self.assertIsNone(got)
                self.assertIn(field, why)
                self.assertIn(RD.OUTCOME_LINE, why)
        for part in ("MELD OUTCOME:", "BAR:", "FALSIFIERS:", "FINDINGS:",
                     "TIP:", "NEXT:"):
            self.assertIn(part, RD.OUTCOME_LINE)
        self.assertIn(RD.OUTCOME_LINE, RD.BAR_TEMPLATE)
        self.assertIn(RD.OUTCOME_LINE, meld.usage())
        guide = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs", "NEW_AGENT_GUIDE.md")
        with open(guide, encoding="utf-8") as f:
            self.assertIn(RD.OUTCOME_LINE, f.read())


class RulingCensusKeysTest(tm.MeldBase):
    """Ruling 7: the census says how many tasks' rooms fell back to the
    chain root, and why, so the 20-task run measures what the task/N parse
    misses."""

    def plant(self, n, lane):
        rid = "%012x" % (n + 100) + "0" * 20     # distinct chain id12s
        r = {"id": rid, "chain_root": rid, "lane": lane,
             "repo_id": "/x/helm/.git", "sender": "a", "recipient": "b",
             "ts": pk.now_ts()}
        room = RD.pair_room(r, {rid: r})[0]
        chat.post("[MELD e:1790000000] PROBLEM: %s | pairing: mixed (claude + "
                  "codex) | convener=a invited=b cap=5 recv-timeout=90s | d "
                  "[HOLD]" % lane, room=room, who="a", sign=False)
        return r

    def test_R7_the_census_counts_rooms_keyed_by_the_chain_fallback(self):
        rows = [self.plant(0, "task-9100"), self.plant(1, "fix-task-9101"),
                self.plant(2, "fix-the-fold"),
                self.plant(3, "task-1-and-task-2")]
        got = RD.pair_census(rows, {r["id"]: r for r in rows})
        self.assertEqual(got["keys"], {"task": 2, "chain_fallback": 2,
                                       "no_task": 1, "two_tasks": 1})


class RulingDigestTest(tm.MeldBase):
    """Ruling 8: a seat with no window stamp gets the digest and the pointer,
    sized as the smallest window, never a guessed large one and never the
    whole log."""

    ROOM = "meld-0-pair-helm-task-3112"

    def test_R8_a_seat_with_no_window_stamp_gets_the_smallest_digest(self):
        for n in range(12):
            meld.invite("seat-b", "round %d: %s" % (n, "y" * 120),
                        seat="seat-a", room=self.ROOM, ring=RING)
            epoch = meld.state(self.ROOM, "seat-a")["epoch"]
            chat.post("[MELD e:%d] %s [YIELD]" % (epoch, "x" * 3000),
                      room=self.ROOM, who="seat-a", sign=False)
        meld.invite("seat-c", "the rebound reader", seat="seat-a",
                    room=self.ROOM, ring=RING)
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_CODE_MAX_CONTEXT_TOKENS", None)
            self.assertEqual(meld.digest_budget(),
                             (meld.DIGEST_FLOOR, None))
            lines = meld.join(self.ROOM, seat="seat-c")
        text = "\n".join(lines)
        rounds = [x for x in lines if x.startswith("  round e:")]
        self.assertTrue(rounds)
        self.assertLessEqual(len("\n".join(lines[2:]).encode()),
                             meld.DIGEST_FLOOR)
        self.assertIn("no window stamp", text)
        self.assertIn("not shown here", text)
        self.assertIn("helm chat read --room %s" % self.ROOM, lines[-1])
        self.assertNotIn("x" * 100, text)


#: The measured shape: a closing line that says AGREED and carries none of
#: the fields the door reads.
INFORMAL = ("MELD OUTCOME: AGREED | hold usage names the parser; RED pins "
            "the text | TIP fa5493a8911 | NEXT source-clean hold, gate")


class _SayTimeBase(tm.MeldBase):
    """A pair round or a design meld between seat-a and seat-b, joined and
    READY, for the closing check's arms."""

    PAIR = "meld-0-pair-helm-task-3223"

    def open(self, room=None, topic="a round"):
        if room:
            meld.invite("seat-b", topic, seat="seat-a", room=room, ring=RING)
        else:
            room, _lines = meld.invite("seat-b", topic, seat="seat-a")
        meld.join(room, seat="seat-b")
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)       # READY
        return room

    def dones(self, room, seat):
        return [m["text"] for m in chat.read(room)[0]
                if m.get("from") == seat and m["text"].endswith("[DONE]")]


class SayTimeOutcomeTest(_SayTimeBase):
    """task/3223: a [DONE] the review door will read is read by the door's
    own parser before it posts. A round seals on its second [DONE] and a
    sealed room takes nothing further, so a block the door refuses, said
    after the seal, cost every row a fresh round."""

    def test_an_informal_DONE_in_a_door_meld_is_refused_and_seals_nothing(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed two-room tuple; the strict close on the same room is asserted done-mutual and AGREED after the refused one posted nothing
        """Arm 1: the peer closed with a strict block, so this [DONE] is the
        one that seals. It is refused, names every field the block lacks,
        prints the one-line block, and the round stays open: a strict close
        still seals it. A pair room and a room whose problem statement
        binds a chain are both rooms the door reads."""
        marked = "lane-3223: agree the bar (chain %s)" % ROOT[:12]
        for label, into, topic in (("pair room", self.PAIR, "a round"),
                                   ("chain-marked room", None, marked)):
            with self.subTest(room=label):
                room = self.open(into, topic)
                meld.say(room, "DONE", outcome(), seat="seat-b")
                meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
                self.assertEqual(meld.state(room, "seat-a")["status"],
                                 "peer-done")
                with self.assertRaises(SystemExit) as cm:
                    meld.say(room, "DONE", INFORMAL, seat="seat-a")
                said = str(cm.exception)
                self.assertIn("lacks BAR, FALSIFIERS, FINDINGS, TIP, NEXT",
                              said)
                self.assertIn(RD.OUTCOME_LINE, said)
                self.assertIn("not sealed", said)
                self.assertEqual(meld.state(room, "seat-a")["status"],
                                 "peer-done")
                self.assertEqual(self.dones(room, "seat-a"), [])
                meld.say(room, "DONE", outcome(), seat="seat-a")
                self.assertEqual(meld.state(room, "seat-a")["status"],
                                 "done-mutual")
                self.assertTrue(RD.room_outcome(room)["agreed"])

    def test_a_strict_DONE_in_a_pair_meld_seals_as_before(self):
        """Arm 2: the control. Both sides close with the block; the round
        seals and the door reads one agreement."""
        room = self.open(self.PAIR)
        meld.say(room, "DONE", outcome(), seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)       # seed
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)       # DONE
        meld.say(room, "DONE", outcome(), seat="seat-b")
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        for seat in ("seat-a", "seat-b"):
            self.assertEqual(meld.state(room, seat)["status"], "done-mutual")
        self.assertTrue(RD.room_outcome(room)["agreed"])

    def test_an_informal_DONE_in_a_plain_meld_seals_as_before(self):  # noqa: VACUOUS_ASSERTION — both seats are asserted done-mutual on the same room; the door reading no agreement there is the product law
        """Arm 3: a design meld the door does not read (no pair room, no
        chain in its problem statement) closes informally, as it always
        has, with a closing line that carries no MELD OUTCOME block."""
        room = self.open(None, "converge the wire format")
        self.assertFalse(RD.is_pair_room(room))
        meld.say(room, "DONE", "closing: the wire format is settled; b "
                 "lands it", seat="seat-a")
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)       # seed
        meld.recv(room, timeout=0, seat="seat-b", poll=0.01)       # DONE
        meld.say(room, "DONE", "agreed; b lands it", seat="seat-b")
        meld.recv(room, timeout=0, seat="seat-a", poll=0.01)
        for seat in ("seat-a", "seat-b"):
            self.assertEqual(meld.state(room, seat)["status"], "done-mutual")
        self.assertFalse(RD.room_outcome(room)["agreed"])

    def test_a_short_TIP_is_refused_as_the_door_refuses_it(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted to carry the door's sentence and the block line; nothing posting is the product law
        """Arm 4: every field present, the tip abbreviated. The refusal is
        the door's own sentence, and nothing posts."""
        room = self.open(self.PAIR)
        text = outcome(tip=TIP[:12])
        _got, door = RD.parse_outcome(text)
        self.assertIn("full commit id", door)
        before = meld.state(room, "seat-a")["status"]
        with self.assertRaises(SystemExit) as cm:
            meld.say(room, "DONE", text, seat="seat-a")
        said = str(cm.exception)
        self.assertIn(door, said)
        self.assertIn(RD.OUTCOME_LINE, said)
        self.assertEqual(meld.state(room, "seat-a")["status"], before)
        self.assertEqual(self.dones(room, "seat-a"), [])


#: The measured block (a pair round, both sides): every field is present,
#: and FINDINGS is prose where the door reads `none` or id=disposition.
FREE_TEXT_FINDINGS = (
    "MELD OUTCOME: AGREED | BAR: dry-run leaves ledger and events unchanged "
    "| FALSIFIERS: dry-run appends; announces cancellation as done | "
    "FINDINGS: none in focused successor; source-clean, builder FAB "
    "unverified here | TIP: %s | NEXT: exact-tip source-clean hold" % TIP)

#: A first build row's design meld (T0): the citation that reads it has no
#: row id and no chain yet, so any room it names is read for its block.
T0_ROW = {"sender": "seat-a", "recipient": "seat-b", "lane": "design-3223",
          "chain_root": None}


def block_cases():
    """(label, text, refusal site, the site's words): one row for every
    refusal `parse_outcome` returns, the one validator the citation reads a
    block with (room_outcome). `site` names the refusal, so a new one added
    to the parser with no row here fails the sweep."""
    good = outcome()
    return (
        ("no block", "closing: agreed, b lands it", "no-block",
         "no MELD OUTCOME block"),
        ("two blocks", good + " " + good, "two-blocks",
         "two MELD OUTCOME blocks"),
        ("an open outcome word", outcome("DONE"), "word",
         "outcome word is not one of"),
        ("a field named twice", good + " | BAR: another harm", "twice",
         "block names BAR twice"),
        ("no BAR", good.replace("BAR: the one harm | ", ""), "lacks",
         "block lacks BAR"),
        ("no FINDINGS", good.replace(" | FINDINGS: F1=cured-in-patch", ""),
         "lacks", "block lacks FINDINGS"),
        ("no TIP", good.replace(" | TIP: " + TIP, ""), "lacks",
         "block lacks TIP"),
        ("no NEXT", good.replace(" | NEXT: record it on the row", ""),
         "lacks", "block lacks NEXT"),
        ("free-text FINDINGS", FREE_TEXT_FINDINGS, "findings",
         "FINDINGS item 'none in focused successor' lacks '='"),
        ("an empty finding id", good.replace("F1=cured-in-patch", "=note"),
         "findings", "FINDINGS contains an empty finding id"),
        ("an open disposition", good.replace("cured-in-patch", "banana"),
         "findings", "has disposition 'banana'"),
        ("a finding named twice",
         good.replace("F1=cured-in-patch", "F1=note; f1=refuted"),
         "finding-twice", "FINDINGS names finding f1 twice"),
        ("a short TIP", outcome(tip=TIP[:12]), "tip",
         "TIP is not one full commit id"),
        ("AGREED with no FALSIFIERS", outcome(falsifiers=None), "falsifiers",
         "AGREED names no FALSIFIERS"),
        ("AGREED with a placeholder falsifier set",
         outcome(falsifiers="none"), "falsifiers",
         "AGREED names no FALSIFIERS"))


class SayTimeBlockParityTest(_SayTimeBase):
    """Any block that seals is citable: `meld say --marker DONE` refuses
    every block the citation refuses, for the same reason, because both read
    it with `parse_outcome`. A block the door would read wherever it is
    cited (a T0 design meld's room carries no pair name and no chain marker)
    is checked in every room; a closing line with no block closes a design
    meld as before."""

    def refused_like_the_citation(self, room, text):
        """Say `text` as seat-a's [DONE]: refused, nothing posted; then post
        both sides' [DONE] raw and cite the round as T0 does. -> (what the
        say refused with, what the citation refused with)."""
        with self.assertRaises(SystemExit) as cm:
            meld.say(room, "DONE", text, seat="seat-a")
        said = str(cm.exception)
        self.assertIn("not sealed", said)
        self.assertEqual(self.dones(room, "seat-a"), [])
        epoch = meld.state(room, "seat-a")["epoch"]
        for who in ("seat-a", "seat-b"):
            chat.post("[MELD e:%d] %s [DONE]" % (epoch, text), room=room,
                      who=who, sign=False)
        fields, cited = RD.meld_citation(room, T0_ROW, (TIP,))
        self.assertIsNone(fields)
        return said, cited

    def test_a_free_text_FINDINGS_block_is_refused_in_every_room(self):  # noqa: VACUOUS_ASSERTION — the say refusal and the citation refusal are each asserted to carry the parser's sentence; nothing posting is the product law
        """The measured block: every field present, FINDINGS in prose. The
        citation refused it after both sides sealed. It is refused before
        the seal in a pair round and in a design meld the door cites."""
        _got, door = RD.parse_outcome(FREE_TEXT_FINDINGS)
        door = door.split(" — the block is one line")[0]
        self.assertIn("none in focused successor", door)
        for label, into, topic in (
                ("pair room", self.PAIR, "a round"),
                ("design meld", None, "design-3223: before the build row")):
            with self.subTest(room=label):
                room = self.open(into, topic)
                said, cited = self.refused_like_the_citation(
                    room, FREE_TEXT_FINDINGS)
                self.assertIn(door, said)
                self.assertIn(door, cited)

    def test_every_block_the_citation_refuses_is_refused_before_the_seal(self):  # noqa: VACUOUS_ASSERTION — every row asserts the parser's sentence in both the say refusal and the citation refusal; the site count is asserted equal to the parser's refusal count
        cases = block_cases()
        self.assertEqual(
            len({site for _l, _t, site, _w in cases}),
            inspect.getsource(RD.parse_outcome).count("return None,"),
            "a parse_outcome refusal with no row in block_cases")
        for n, (label, text, site, words) in enumerate(cases):
            _got, door = RD.parse_outcome(text)
            self.assertIn(words, door or "", label)
            door = door.split(" — the block is one line")[0]
            rooms = [("pair room", "meld-0-pair-helm-task-%d" % (9000 + n))]
            # a design meld with no block closes as before (arm 3)
            rooms += [("design meld", None)] if site != "no-block" else []
            for kind, into in rooms:
                with self.subTest(case=label, room=kind):
                    room = self.open(into, "design-3223: case %d" % n)
                    said, cited = self.refused_like_the_citation(room, text)
                    self.assertIn(door, said)
                    self.assertIn(door, cited)


class SayTimeBindingTest(_PairDoorBase):
    """task/3223, the door's second requirement: a block that names a
    dispatched row's tip claims that row, and the door counts a round for
    the row only when its problem statement carries the row's `(chain
    <id12>)` marker (`off_chain`, which the citation and the stop rung both
    read). A strict block in a round that cannot bind is refused before it
    seals, in the door's words, with the invite that opens a round that
    binds."""

    def close(self, room, text):
        """The reader closes, the convener reads it and countersigns."""
        meld.recv(room, timeout=0, seat="integrator", poll=0.01)   # READY
        meld.say(room, "DONE", text, seat="seat-b")
        meld.recv(room, timeout=0, seat="integrator", poll=0.01)   # its DONE
        meld.say(room, "DONE", text, seat="integrator")

    def test_a_strict_DONE_in_a_round_the_door_cannot_bind_is_refused(self):
        self.lane = "meld-done-grammar-3223"
        rc, _out, err, sent = self.send(None, self.b)
        self.assertEqual(rc, 0, err)
        pair = self.pair_room(sent)
        strict = outcome(tip=self.b)
        # the control: the round the dispatch opened binds, and seals
        meld.join(pair, seat="seat-b")
        self.close(pair, strict)
        self.assertEqual(meld.state(pair, "integrator")["status"],
                         "done-mutual")
        fields, why = RD.meld_citation(pair, sent, [self.b])
        self.assertIsNone(why, why)
        chain12 = sent["chain_root"][:12]
        invite = RD.meld_invite("integrator", self.lane, "UNDER-ARMED",
                                sent["chain_root"])
        door_word = "the marker is the literal token (chain %s)" % chain12
        cases = (
            # the measured shape: a fresh repair meld, no chain marker
            ("fresh room", None, "repair strict meld citation row %s tip %s"
             % (sent["id"][:12], self.b[:12])),
            # a round of the pair room opened by hand, no chain marker
            ("hand round in the pair room", pair, "a repair round"))
        for label, into, topic in cases:
            with self.subTest(case=label):
                room, _lines = meld.invite("seat-b", topic, seat="integrator",
                                           room=into)
                meld.join(room, seat="seat-b")
                meld.recv(room, timeout=0, seat="integrator", poll=0.01)
                meld.recv(room, timeout=0, seat="seat-b", poll=0.01)
                before = meld.state(room, "seat-b")["status"]
                with self.assertRaises(SystemExit) as cm:
                    meld.say(room, "DONE", strict, seat="seat-b")
                said = str(cm.exception)
                self.assertIn(door_word, said)
                self.assertIn(invite, said)
                self.assertIn("not sealed", said)
                self.assertEqual(meld.state(room, "seat-b")["status"], before)
                # the door refuses this round for the same reason
                epoch = meld.state(room, "seat-b")["epoch"]
                for who in ("seat-b", "integrator"):
                    chat.post("[MELD e:%d] %s [DONE]" % (epoch, strict),
                              room=room, who=who, sign=False)
                got = RD.room_outcome(room, epoch=epoch)
                self.assertTrue(got["agreed"], got["why"])
                proof, door = RD.off_chain(got, sent["chain_root"],
                                           sent["lane"], {self.b})
                self.assertEqual(proof, "subject")
                self.assertIn(door_word, door)
                self.assertIn(door, said)
                _fields, cited = RD.meld_citation("%s@%d" % (room, epoch),
                                                  sent, [self.b])
                self.assertTrue(cited)
        # the cure the refusal prints: a round whose statement binds the
        # chain seals, and the door records it
        room, _lines = meld.invite(
            "seat-b", RD.bar_topic(self.lane) + RD.chain_mark(
                sent["chain_root"]), seat="integrator")
        meld.join(room, seat="seat-b")
        self.close(room, strict)
        self.assertEqual(meld.state(room, "integrator")["status"],
                         "done-mutual")
        fields, why = RD.meld_citation(room, sent, [self.b])
        self.assertIsNone(why, why)
        self.assertEqual(fields["meld_outcome"], "agreed")

    def test_a_tip_the_bound_row_is_not_about_is_refused_before_the_seal(self):
        """A round the door binds to a row (the pair round its dispatch
        opened, or a statement carrying its chain marker) closes only on a
        tip that row's record is about: its dispatched tip, or a commit that
        descends from it (a reviewer's patch, a source-clean cure). Any
        other full tip seals a round the citation then refuses."""
        self.lane = "meld-done-grammar-3223"
        rc, _out, err, sent = self.send(None, self.b)
        self.assertEqual(rc, 0, err)
        pair = self.pair_room(sent)
        meld.join(pair, seat="seat-b")
        meld.recv(pair, timeout=0, seat="integrator", poll=0.01)   # READY
        for label, tip in (("a full tip no row carries", "e" * 40),
                           ("a commit off another branch", self.side),
                           ("the row tip's ancestor", self.a)):
            with self.subTest(tip=label):
                before = meld.state(pair, "seat-b")["status"]
                with self.assertRaises(SystemExit) as cm:
                    meld.say(pair, "DONE", outcome(tip=tip), seat="seat-b")
                said = str(cm.exception)
                self.assertIn("which this record is not about", said)
                self.assertIn(sent["id"][:12], said)
                self.assertIn("not sealed", said)
                self.assertEqual(meld.state(pair, "seat-b")["status"], before)
                self.assertFalse([m for m in chat.read(pair)[0]
                                  if m["text"].endswith("[DONE]")])
        # the corrected tip closes the round, and the door records it
        self.close(pair, outcome(tip=self.b))
        self.assertEqual(meld.state(pair, "integrator")["status"],
                         "done-mutual")
        fields, why = RD.meld_citation(pair, sent, [self.b])
        self.assertIsNone(why, why)
        # a round bound by the chain marker closes on a patch that descends
        # from the row's tip, and a verdict carrying that patch cites it
        room, _lines = meld.invite(
            "seat-b", RD.bar_topic(self.lane) + RD.chain_mark(
                sent["chain_root"]), seat="integrator")
        meld.join(room, seat="seat-b")
        self.close(room, outcome(tip=self.c))
        self.assertEqual(meld.state(room, "integrator")["status"],
                         "done-mutual")
        fields, why = RD.meld_citation(room, sent, [self.b, self.c])
        self.assertIsNone(why, why)
        self.assertEqual(fields["meld_outcome"], "agreed")


class PairMeldMutationTest(unittest.TestCase):
    """Each arm, pointed at the old or a wrong behaviour, fails. A case is
    one or more one-line rewrites of shipped functions and the arm that must
    kill them."""

    CASES = (
        # (b) the room keyed by the row in hand, not the chain's first row
        ([(RD, "pair_key", "    root = _chain_root_row(row, current)\n",
           "    root = row or {}\n")],
         PairDispatchTest, "test_P2_b_every_round_of_a_chain_opens_in_its_one_room"),
        ([(RD, "pair_key", "    root = _chain_root_row(row, current)\n",
           "    root = row or {}\n")],
         PairRoomNameTest, "test_b_every_row_of_a_chain_names_its_first_rows_room"),
        # the owning project read off somewhere other than the chain
        ([(RD, "pair_scope",
           "        token = dispatches._repo_project(repo_id) or "
           "_lanes.project_token(\n"
           "            os.path.dirname(repo_id.rstrip(os.sep)))\n",
           "        token = \"helm\"\n")],
         PairRoomNameTest,
         "test_a_chain_keeps_its_room_in_the_owning_projects_scope"),
        # P3: a persistent room cannot start a second round
        ([(meld, "_opens_round", "    return (kind in _ROUND_OPENERS",
           "    return False and (kind in _ROUND_OPENERS")],
         PairRoundTest, "test_P3_the_exchange_cap_counts_one_round"),
        # the lifecycle fold refuses a second round's epoch
        ([(meld, "_reduce",
           "            if not _opens_round(ev[\"transition\"], "
           "before[\"epoch\"],\n"
           "                                ev[\"epoch\"]):\n",
           "            if True:\n")],
         PairRoundTest, "test_the_lifecycle_journal_folds_every_round"),
        # join reads the room's FIRST round, as before
        ([(meld, "join", "    epoch, convener, seedtext = latest_seed(rows)\n",
           "    epoch, convener, seedtext = seeds(rows)[0][:3]\n")],
         PairRoundTest, "test_join_reads_the_newest_round"),
        # (f) the whole history carried, whatever the window
        ([(meld, "history_digest",
           "        if reserve + used + size > budget:\n",
           "        if used + size > budget:\n")],
         PairRoundTest,
         "test_f_a_joiner_gets_a_digest_bounded_to_its_window_never_the_log"),
        # P4: the citation reads the room's first round
        ([(RD, "room_outcome",
           "        epoch, convener, seedtext = meld.latest_seed(rows)\n",
           "        epoch, convener, seedtext = meld.seeds(rows)[0][:3]\n")],
         PairOutcomeTest, "test_P4_the_newest_round_is_the_one_a_citation_reads"),
        # (d) an AGREED block with no falsifier set counted as converged
        ([(RD, "parse_outcome",
           "    if word == \"AGREED\" and not falsifier_set("
           "out.get(\"falsifiers\")):\n", "    if False:\n")],
         PairOutcomeTest,
         "test_d_an_AGREED_block_with_no_falsifier_set_has_not_converged"),
        ([(RD, "room_outcome",
           "            if o[\"outcome\"] == \"AGREED\"}) > 1:\n",
           "            if o[\"outcome\"] == \"AGREED\"}) > 99:\n")],
         PairOutcomeTest, "test_d_two_falsifier_sets_are_two_bars"),
        # P1: the first round is not the plan
        ([(RD, "pair_topic",
           "    parts.append(PAIR_PLAN if not rounds else PAIR_NEXT_ROUND)\n",
           "    parts.append(PAIR_NEXT_ROUND)\n")],
         PairDispatchTest, "test_P1_the_first_dispatch_opens_the_plan_round"),
        # P1: a same-family pair dressed as a mixed one
        ([(RD, "pairing", "    if mine & theirs:\n", "    if False:\n")],
         PairDispatchTest,
         "test_P1_a_same_family_pair_says_so_and_a_mixed_one_is_named"),
        # P6: the room rings the reader a second time
        ([(RD, "open_pair_round",
           "        meld.invite(reader, \"pending locked round topic\", "
           "seat=sender,\n                    room=room, ring=ring, "
           "_round_topic=topic_for,\n                    _opened=opened, "
           "_round_marker=\"opening-row=%s \" % row12)\n",
           "        meld.invite(reader, \"pending locked round topic\", "
           "seat=sender,\n                    room=room, "
           "_round_topic=topic_for,\n                    _opened=opened, "
           "_round_marker=\"opening-row=%s \" % row12)\n")],
         PairDispatchTest, "test_P6_the_dispatch_DM_is_the_only_ring"),
        # (c) a round that cannot open takes the send down with it
        ([(RD, "open_pair_round",
           "    except (SystemExit, Exception) as exc:            "
           "# noqa: BLE001\n",
           "    except SystemExit as exc:\n"),
          (dispatches, "_open_pair",
           "    except Exception as exc:                            "
           "# noqa: BLE001\n",
           "    except ImportError as exc:\n")],
         PairDispatchTest,
         "test_c_a_reader_with_no_live_beacon_is_never_blocked_or_waited_on"),
        # (e) a rebind that leaves the new reader outside the room
        ([(dispatches, "rebind",
           "        _ref_branch=row.get(\"ref_branch\"), "
           "_preserve_origin=dispatches._MOVE_MINT,\n"
           "        pair_meld=pair_meld)\n",
           "        _ref_branch=row.get(\"ref_branch\"), "
           "_preserve_origin=dispatches._MOVE_MINT)\n")],
         PairDispatchTest,
         "test_e_a_rebind_invites_the_new_reader_into_the_same_room"),
        # (g) the agreed row records no cost
        ([(RD, "meld_citation",
           "            \"meld_bytes\": int(got[\"bytes\"]),\n", "")],
         PairDispatchTest,
         "test_P4_g_an_AGREED_round_lands_on_the_row_with_its_cost"),
        # (a) a room's own opening counted as the row's agreement
        ([(RD, "pair_census",
           "        hits = [r for r in mine if r.get(\"meld_outcome\") == "
           "\"agreed\"\n                and r.get(\"meld_room\") == room]\n",
           "        hits = mine\n")],
         PairCensusTest,
         "test_a_only_an_opened_room_and_a_recorded_agreement_count"),
        # ruling 2: a quiet auto-opened pair round lapses like a door meld
        ([(RD, "entry_lapsed", "    if is_pair_room(room):\n        return False\n",
           "    if False:\n        return False\n")],
         RulingSpiralWindowTest, "test_R2_a_quiet_auto_opened_round_still_walls"),
        # ruling 2: the author's turn alone reads as a conversation
        ([(RD, "pair_exchange",
           "        if any({seat, peer} <= turns for turns in spoke.values()):\n",
           "        if any(seat in turns for turns in spoke.values()):\n")],
         RulingSpiralWindowTest,
         "test_R2_a_reader_without_a_turn_or_a_live_beacon_never_silences"),
        # ruling 2: a dark reader's past turn silences the rung
        ([(spiral_mod, "_spiral_gate",
           "    if talking and review_door.live_beacon(peer) is True:\n",
           "    if talking:\n")],
         RulingSpiralWindowTest,
         "test_R2_a_reader_without_a_turn_or_a_live_beacon_never_silences"),
        # ruling 2: any reader's turn stands in for the current reader's
        ([(RD, "pair_exchange",
           "                  if (room_is_chain or _seed_chain(seed) == chain12)\n"
           "                  and {seat, peer} <= ({convener} |\n"
           "                      set(meld._invited_seats(seed)))}\n",
           "                  if (room_is_chain or _seed_chain(seed) == chain12)\n"
           "                  and seat in ({convener} |\n"
           "                      set(meld._invited_seats(seed)))}\n"),
          (RD, "pair_exchange",
           "            if m.get(\"react\") or frm not in (seat, peer) \\\n",
           "            if m.get(\"react\") or not frm \\\n"),
          (RD, "pair_exchange",
           "        if any({seat, peer} <= turns for turns in spoke.values()):\n",
           "        if any(seat in turns and len(turns) > 1 "
           "for turns in spoke.values()):\n")],
         RulingSpiralWindowTest,
         "test_R2_a_turn_from_a_reader_who_is_not_the_current_one_never_silences"),
        # ruling 2: another chain's round in one task room stands in for ours
        ([(RD, "pair_exchange",
           "(room_is_chain or _seed_chain(seed) == chain12)", "True")],
         RulingSpiralWindowTest,
         "test_R2_an_exchange_for_another_chain_in_the_task_room_is_not_mine"),
        # ruling 2: an agreed pair round silences with its reader dark
        ([(spiral_mod, "_spiral_gate",
           "    if melded and review_door.is_pair_room(why) \\\n",
           "    if False \\\n")],
         RulingSpiralWindowTest,
         "test_R2_an_agreed_pair_round_whose_reader_is_dark_never_silences"),
        # (h) a round's outcome closes another round's row
        ([(RD, "meld_citation", "        if named and named != mine:\n",
           "        if False:\n")],
         RulingExactRoundTest,
         "test_h_an_AGREED_from_round_N_does_not_exempt_round_N_plus_1"),
        # (h) at the door, a recorded pair-round AGREED exempts the next round
        ([(RD, "plan", "    if melded and is_pair_room(melded):\n",
           "    if False:\n")],
         RulingExactRoundTest,
         "test_h_a_pair_rounds_AGREED_does_not_exempt_the_next_send_at_the_door"),
        # (h) at the rung, a round the room moved past still vouches
        ([(RD, "converged",
           "    if is_pair_room(room) and st.get(\"epoch\") != got[\"epoch\"]:\n",
           "    if False:\n")],
         PairOutcomeTest, "test_h_a_round_the_room_moved_past_vouches_for_nothing"),
        # ruling 5: the refusal without its fix line
        ([(RD, "_outcome_fix", "    return \"%s — the block is one line: %s\" % (why, OUTCOME_LINE)\n",
           "    return why\n")],
         RulingOutcomeLineTest,
         "test_R5_a_refused_block_names_the_field_and_prints_the_five_field_fix"),
        # ruling 7: the fallback rooms uncounted
        ([(RD, "pair_census", "            keys[\"chain_fallback\"] += 1\n",
           "            pass\n")],
         RulingCensusKeysTest,
         "test_R7_the_census_counts_rooms_keyed_by_the_chain_fallback"),
        # ruling 8: an unstamped window guessed large
        ([(meld, "digest_budget", "            return DIGEST_FLOOR, None\n",
           "            window = 200000\n")],
         RulingDigestTest,
         "test_R8_a_seat_with_no_window_stamp_gets_the_smallest_digest"),
        # P5: a round the room moved past still owed
        ([(RD, "pair_turns_owed",
           "        if latest is None or latest != epoch:\n",
           "        if latest is None:\n")],
         PairTurnRungTest, "test_a_round_the_room_moved_past_owes_nothing"),
        # P5: a HOLD read as handing the floor over
        ([(RD, "pair_turns_owed",
           "last[2] in (\"YIELD\", \"READY\", \"DONE\"):",
           "last[2] in (\"YIELD\", \"READY\", \"DONE\", \"HOLD\"):")],
         PairTurnRungTest,
         "test_P5_the_stop_guard_names_a_round_whose_floor_is_yours"),
        # (d) a round with no open finding can never AGREE
        ([(RD, "_finding_error",
           "        if not sep and (not key or key.casefold() in "
           "_NO_FINDING):\n"
           "            continue                      # a blank item is not "
           "a finding\n",
           "        if False:\n            continue\n")],
         PairOutcomeTest, "test_d_a_round_with_no_open_finding_still_agrees"),
        # (d) two spellings of the empty finding set read as two bars
        ([(RD, "_finding_pairs",
           "        if not key or (not _sep and key in _NO_FINDING):\n",
           "        if not key:\n")],
         PairOutcomeTest, "test_d_a_round_with_no_open_finding_still_agrees"),
    )

    def test_each_pair_mutant_is_killed_by_its_arm(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a fixed non-empty CASES tuple and asserts testsRun == 1 and at least one failure on every pass
        for edits, owner, arm in self.CASES:
            with self.subTest(arm=arm, mutant=edits[0][1]):
                # every edit to one symbol applies to its ORIGINAL source,
                # so a two-line mutant is one function, not a mutant of one
                sources = {}
                for module, symbol, before, after in edits:
                    key = (module, symbol)
                    source = sources.get(key) or inspect.getsource(
                        getattr(module, symbol))
                    self.assertEqual(source.count(before), 1,
                                     "%s: %r" % (symbol, before))
                    sources[key] = source.replace(before, after)
                with contextlib.ExitStack() as stack:
                    for (module, symbol), source in sources.items():
                        # the function's own namespace (task/3407): a name
                        # moved to a ledger satellite runs there
                        namespace = dict(getattr(module, symbol).__globals__)
                        exec(compile(source, "pair-mutant", "exec"),
                             namespace)
                        stack.enter_context(mock.patch.object(
                            module, symbol, namespace[symbol]))
                    result = unittest.TestResult()
                    owner(arm).run(result)
                self.assertEqual(result.testsRun, 1)
                # an arm with subtests reports each one it loses
                self.assertGreaterEqual(
                    len(result.failures) + len(result.errors), 1,
                    "arm %s did not kill its mutant" % arm)


def setUpModule():
    """No dispatch row this module writes walks the host's process table."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()
