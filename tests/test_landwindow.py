#!/usr/bin/env python3
"""`helm train` — DOES THE LANDING WINDOW COMPOSE ITSELF, BY MERGE, THROUGH THE
DOOR, AND DOES EVERY REFUSAL FIRE FOR ITS OWN REASON?

Every arm drives the shipped verb against a REAL temp repository, REAL dispatch
rows with REAL approves in a temp helm home, and the REAL landing-window door.
The only substitutes are the ones the door's own arms use, for the things this
box must never actually do: the `fab gate` dispatch, the `fab kill`, the
authority read and the detached client. They are THE SAME SPIES, shared by
reference from tests/test_gatewindow.py, so an arm here observes what the door
was handed and never a reconstruction of it.

THE FIXTURE IS REUSED BY REFERENCE TOO: `LandReqBase` is the object
tests/test_landreq.py defines (temp HELM_HOME, temp repo, dispatch writes that
pass the real door, verdicts from the real producer), reached through its
module so its collectable name is not republished here.

EACH ARM CARRIES ITS CONTROL ON THE SAME OBSERVABLE: a refusal is paired with
the one changed fact that makes the same verb compose, merge or dispatch.
"""
import contextlib
import io
import os
import shutil
import subprocess
import unittest
from unittest import mock

from helm import gatewindow, landreq, landwindow, vcs
from tests import test_gatewindow as _gw
from tests import test_landreq as _landreq


# THIS MODULE DOES NOT READ HOST LIVENESS. The same declaration, for the same
# measured reason, as tests/test_landreq.py and tests/test_lr_compose.py: every
# dispatch write reaches a census of the host's whole process table that no arm
# here asserts on. Module scope, so every class is covered whatever its base.
_LIVE_SEATS_PATCH = None


def setUpModule():
    global _LIVE_SEATS_PATCH
    from helm import proxywatch
    _LIVE_SEATS_PATCH = mock.patch.object(
        proxywatch, "_live_seats", lambda: (set(), None, {}))
    _LIVE_SEATS_PATCH.start()


def tearDownModule():
    if _LIVE_SEATS_PATCH is not None:
        _LIVE_SEATS_PATCH.stop()


def _rc(*argv):
    return subprocess.run(("git",) + argv, capture_output=True).returncode


class TrainBase(_landreq.LandReqBase):
    # THE DOOR'S OWN SPIES, bound by reference. Each reads and writes the
    # attributes setUp gives it (spawns, measures, observes, kills, gens).
    generation = _gw.WindowBase.generation
    fab = _gw.WindowBase.fab
    observe = _gw.WindowBase.observe
    kill = _gw.WindowBase.kill
    no_fab_status = _gw.WindowBase.no_fab_status

    def setUp(self):
        super().setUp()
        self.spawns, self.measures, self.observes = [], [], []
        self.kills, self.gens, self.detached = [], {}, []
        self.store = gatewindow.runs_path(
            os.path.join(self.tmp, "helm", "_global"))
        # THIS FIXTURE DECLARES ITS TRUNK AUTHORITY, the way the helm checkout
        # does: this repository's own main, with no remote. `--apply` refuses
        # an undeclared or unreadable authority, so an arm about anything else
        # must stand on a declared one; the arms about the authority change it.
        self.git("config", "helm.trunkRef", "refs/heads/" + self.main)

    def lane(self, name, path, base=None):
        self.git("checkout", "-q", "-b", name, base or self.a)
        tip = self.commit(name, path=path)
        self.git("checkout", "-q", self.main)
        return tip

    def ready(self, name, path, base=None):
        """A lane with a real dispatch row and a real APPROVE on its tip."""
        tip = self.lane(name, path, base)
        row = self.dispatch(ref=tip, lane=name)
        _row, err = self.mark_verdict(row["id"], tip, "ok", polarity="approve")
        self.assertIsNone(err, err)
        return row, tip

    def door(self, run_id="r1", live=()):
        def detach(argv, log):
            self.detached.append((list(argv), log))
            return type("P", (), {"pid": 4242})()
        return {"fab": self.fab(run_id, "snoozy"), "observe": self.observe(live),
                "inflight": self.no_fab_status(), "kill": self.kill(),
                "detach": detach, "path": self.store,
                "pid_alive": lambda pid: False}

    def train(self, apply=True, name=None, door=None, trunk=True, **cap):
        """`trunk` True is this fixture's local main; None is the verb's own
        default, the remote-tracking trunk when a remote exists. `cap` is
        `max_behind=N` or nothing, so the default arms call the verb as its
        CLI does without the flag."""
        out = io.StringIO()
        rc = landwindow.compose(self.repo,
                                trunk=self.main if trunk is True else trunk,
                                name=name, apply=apply,
                                door=door or self.door(), out=out, **cap)
        return rc, out.getvalue()

    def room(self, name="train1"):
        return os.path.join(os.path.realpath(self.repo) + "-wt", "compose",
                            name)

    def merges(self, room):
        """[(merge, first parent, second parent)] from trunk to the room's head,
        in the order they were made."""
        lines = self.git("rev-list", "--reverse", "--merges", "--parents",
                         self.main + "..HEAD", cwd=room).splitlines()
        return [tuple(line.split()) for line in lines]

    def ready_all(self):
        return [self.ready("one", "g"), self.ready("two", "h"),
                self.ready("three", "i")]


class ComposesByMergeThroughTheDoor(TrainBase):

    def test_three_approve_ready_rows_give_one_room_three_merges_and_one_launch(self):  # noqa: VACUOUS_ASSERTION — the only absence is the detached-HEAD rc; every other assertion is a positive count or an exact value (three merges on the three tips, one launch, one record at the room's head)
        rows = self.ready_all()
        # PRECONDITION, on the real projection: all three rows are READY. An
        # arm that composed rows the projection never called approve-ready
        # would be testing a fixture, not the listing the verb reuses.
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertEqual([lrs[row["id"]]["state"] for row, _tip in rows],
                         ["READY"] * 3)
        with mock.patch.object(gatewindow, "launch",
                               wraps=gatewindow.launch) as door:
            rc, text = self.train()
        self.assertEqual(rc, 0, text)
        room = self.room()
        # ONE ROOM, detached, under <repo>-wt/compose/.
        self.assertEqual(os.listdir(os.path.dirname(room)), ["train1"])
        self.assertNotEqual(_rc("-C", room, "symbolic-ref", "-q", "HEAD"), 0)
        # THREE MERGES, each on EXACTLY one reviewed sha, each no-ff onto the
        # previous head, with the ruling's message.
        merges = self.merges(room)
        self.assertEqual(len(merges), 3, text)
        self.assertEqual(sorted(m[2] for m in merges),
                         sorted(tip for _row, tip in rows))
        self.assertEqual(merges[0][1], self.git("rev-parse", self.main))
        self.assertEqual([m[1] for m in merges[1:]],
                         [m[0] for m in merges[:-1]])
        subjects = self.git("log", "--reverse", "--first-parent",
                            "--format=%s", self.main + "..HEAD",
                            cwd=room).splitlines()
        self.assertEqual(sorted(subjects),
                         ["train1: merge lane one", "train1: merge lane three",
                          "train1: merge lane two"])
        # THE MERGE ORDER IS THE ONE THE VERB PRINTED.
        listed = [line.split("lane ")[1].split()[0]
                  for line in text.splitlines()
                  if line.strip()[:2] in ("1.", "2.", "3.")]
        self.assertEqual(["train1: merge lane " + n for n in listed], subjects)
        # ONE LAUNCH, through the door, of this room, as this train.
        self.assertEqual(door.call_count, 1)
        self.assertEqual(os.path.realpath(door.call_args[0][0]), room)
        self.assertEqual(door.call_args[1]["label"], "train1")
        self.assertEqual(len(self.spawns), 1, self.spawns)
        self.assertEqual(self.spawns[0][:5],
                         ["fab", "gate", "submit", "--repo", room])
        records = gatewindow.read_runs(self.store)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["head"],
                         self.git("rev-parse", "HEAD", cwd=room))
        self.assertEqual(records[0]["trunk"], self.git("rev-parse", self.main))
        self.assertEqual(records[0]["label"], "train1")
        self.assertIn("DISPATCHED", text)

    def test_every_reviewed_tip_is_an_ANCESTOR_of_the_composed_head(self):  # noqa: VACUOUS_ASSERTION — ANCESTOR is asserted positively for every tip, before and after the land; the one NOT_ANCESTOR is the control, paired with the picked copy's file being present
        """The rung the ruling rests on: a merge keeps the reviewed sha, so the
        reviewed commit is an ancestor of the composed head and, once the train
        lands, of trunk. Answered through the seam foldcheck itself asks."""
        rows = self.ready_all()
        rc, text = self.train()
        self.assertEqual(rc, 0, text)
        room = self.room()
        head = self.git("rev-parse", "HEAD", cwd=room)
        be = vcs.backend(self.repo)
        for _row, tip in rows:
            self.assertEqual(be.ancestry(self.repo, tip, head), vcs.ANCESTOR,
                             tip)
        # LANDED: trunk fast-forwards to the composed head, and every reviewed
        # sha is then an ancestor of trunk itself.
        self.git("merge", "-q", "--ff-only", head)
        for _row, tip in rows:
            self.assertEqual(be.ancestry(self.repo, tip, self.main),
                             vcs.ANCESTOR, tip)
        # CONTROL, on the same question: the SAME content composed by
        # cherry-pick carries the change and is NOT an answer to it — the
        # reviewed sha is no ancestor of a picked copy. That is the one-hop
        # weaker proof the ruling chose merge to avoid.
        picked = os.path.join(self.tmp, "picked")
        self.git("worktree", "add", "-q", "--detach", picked, self.c)
        _row, tip = rows[0]
        self.git("cherry-pick", tip, cwd=picked)
        copy = self.git("rev-parse", "HEAD", cwd=picked)
        self.assertTrue(os.path.exists(os.path.join(picked, "g")))
        self.assertEqual(be.ancestry(self.repo, tip, copy), vcs.NOT_ANCESTOR)

    def test_the_default_is_a_dry_run_that_lists_rows_tips_and_order(self):  # noqa: VACUOUS_ASSERTION — the dry run's absences (no room, no record) are paired with the exact listing lines and with the --apply control that mints, merges twice and dispatches once
        rows = [self.ready("one", "g"), self.ready("two", "h")]
        worktrees = self.git("worktree", "list")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landwindow.cmd_train(["--repo", self.repo, "--trunk",
                                       self.main])
        text = out.getvalue()
        self.assertEqual(rc, 0, text + err.getvalue())
        self.assertIn("merge order, 2 approve-ready rows", text)
        for row, tip in rows:
            self.assertIn("%s  lane %s  reviewed tip %s"
                          % (row["id"][:12], row["lane"], tip[:12]), text)
        self.assertIn("dry run: nothing minted, merged or launched", text)
        # NOTHING WAS MINTED, MERGED OR LAUNCHED.
        self.assertFalse(os.path.exists(os.path.realpath(self.repo) + "-wt"))
        self.assertEqual(self.git("worktree", "list"), worktrees)
        self.assertEqual(gatewindow.read_runs(self.store), [])
        # CONTROL: the same listing with --apply does all three.
        rc, text = self.train()
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.merges(self.room())), 2)
        self.assertEqual(len(self.spawns), 1)

    def test_the_train_is_numbered_one_past_the_last_train_on_trunk(self):
        self.ready("one", "g")
        rc, text = self.train(apply=False)
        self.assertEqual(rc, 0, text)
        self.assertIn("helm train: train1 over", text)
        self.git("commit", "-q", "--allow-empty", "-m",
                 "train41: merge lane somebody")
        self.git("commit", "-q", "--allow-empty", "-m",
                 "train7: merge lane somebody else")
        rc, text = self.train(apply=False)
        self.assertEqual(rc, 0, text)
        self.assertIn("helm train: train42 over", text)
        rc, text = self.train()
        self.assertEqual(rc, 0, text)
        self.assertTrue(os.path.isdir(self.room("train42")))


class RefusesWithoutResolving(TrainBase):

    def test_a_conflicting_row_is_refused_by_name_and_leaves_no_half_resolved_merge(self):
        one, one_tip = self.ready("one", "g")
        # `state` is the file trunk's own b and c commits append to, so a lane
        # off `a` that appends to it is a genuine textual conflict.
        clash, clash_tip = self.ready("clash", "state")
        two, two_tip = self.ready("two", "h")
        rc, text = self.train()
        self.assertEqual(rc, 1, text)
        self.assertIn("REFUSED %s (lane clash): conflict in state"
                      % clash["id"][:12], text)
        self.assertIn("aborted and NOT resolved", text)
        room = self.room()
        # NO HALF-RESOLVED MERGE: no merge in progress, a clean room, no
        # conflict markers, and the conflicting sha nowhere in the head.
        self.assertNotEqual(_rc("-C", room, "rev-parse", "-q", "--verify",
                                "MERGE_HEAD"), 0)
        self.assertEqual(self.git("status", "--porcelain", cwd=room), "")
        with open(os.path.join(room, "state"), encoding="utf-8") as fh:
            self.assertNotIn("<<<<<<<", fh.read())
        self.assertEqual(_rc("-C", room, "merge-base", "--is-ancestor",
                             clash_tip, "HEAD"), 1)
        # CONTROL: the other two rows composed, and the door still gated them.
        self.assertEqual(sorted(m[2] for m in self.merges(room)),
                         sorted((one_tip, two_tip)))
        self.assertEqual(len(self.spawns), 1, text)
        self.assertEqual(gatewindow.read_runs(self.store)[0]["head"],
                         self.git("rev-parse", "HEAD", cwd=room))

    def test_rerere_is_off_for_every_merge(self):
        # THE REPOSITORY ASKS FOR RERERE. The verb must override it per merge.
        self.git("config", "rerere.enabled", "true")
        self.ready("one", "g")
        _clash, clash_tip = self.ready("clash", "state")
        self.ready("two", "h")
        seen = []
        real = vcs.backend

        def spying(root):
            be = real(root)

            class Spy:
                def __getattr__(self, name):
                    return getattr(be, name)

                def text(self, cwd, *args, **kw):
                    seen.append((os.path.realpath(cwd), list(args)))
                    return be.text(cwd, *args, **kw)
            return Spy()
        with mock.patch.object(vcs, "backend", spying):
            rc, text = self.train()
        self.assertEqual(rc, 1, text)
        merges = [a for cwd, a in seen if cwd == self.room() and "merge" in a]
        # three merges and the one abort, every one with rerere forced off
        # BEFORE the verb, where git reads it as configuration.
        self.assertEqual(len(merges), 4, merges)
        for argv in merges:
            self.assertEqual(argv[:3], ["-c", "rerere.enabled=false", "merge"],
                             argv)
        cache = os.path.join(self.repo, ".git", "rr-cache")
        self.assertEqual(os.listdir(cache) if os.path.isdir(cache) else [], [])
        # CONTROL, on the same observable: the same conflicting merge under the
        # repository's own setting DOES record a preimage, so the empty cache
        # above is the verb's doing and not a fixture that cannot record one.
        probe = os.path.join(self.tmp, "probe")
        self.git("worktree", "add", "-q", "--detach", probe, self.main)
        self.assertNotEqual(_rc("-C", probe, "merge", "--no-ff", "--no-edit",
                                clash_tip), 0)
        self.assertTrue(os.listdir(cache))

    def test_a_row_not_yet_approve_ready_is_excluded(self):
        one, one_tip = self.ready("one", "g")
        two, two_tip = self.ready("two", "h")
        pending_tip = self.lane("pending", "i")
        pending = self.dispatch(ref=pending_tip, lane="pending")
        # CONTROL: the projection DOES carry the row, in a state that is not
        # READY — the exclusion is the verb reading that word, not the row
        # being invisible.
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertIn(pending["id"], lrs)
        self.assertNotEqual(lrs[pending["id"]]["state"], "READY")
        rc, dry = self.train(apply=False)
        self.assertEqual(rc, 0, dry)
        self.assertIn(one["id"][:12], dry)
        self.assertIn(two["id"][:12], dry)
        self.assertNotIn(pending["id"][:12], dry)
        rc, text = self.train()
        self.assertEqual(rc, 0, text)
        room = self.room()
        self.assertEqual(sorted(m[2] for m in self.merges(room)),
                         sorted((one_tip, two_tip)))
        self.assertEqual(_rc("-C", room, "merge-base", "--is-ancestor",
                             pending_tip, "HEAD"), 1)


class OnlyLiveApproveReadyRowsAreCars(TrainBase):

    def test_a_closed_row_the_projection_still_calls_READY_is_not_a_car(self):  # noqa: VACUOUS_ASSERTION — the loop is a two-row precondition on the real projection; the merged list equal to exactly [one_tip] is the unconditional positive control on the SAME observable as the closed row's absence
        """THE FIRST LIVE DRY RUN FOUND THIS. The projection keeps the stored
        state READY on a row that has since closed and marks it `terminal`;
        457 of 459 READY rows on the real ledger were closed as landed, and
        214 of them had landed rebased, so their tips were no ancestors of
        trunk and a train keyed on the word alone would merge them back in."""
        one, one_tip = self.ready("one", "g")
        two, two_tip = self.ready("two", "h")
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        # CONTROL, on the real projection: both are live READY rows.
        for row in (one, two):
            self.assertEqual(lrs[row["id"]]["state"], "READY")
            self.assertFalse(lrs[row["id"]]["terminal"])
        # THE SHAPE THE LIVE LEDGER CARRIES: stored READY, closed as landed.
        closed = dict(lrs)
        closed[two["id"]] = dict(lrs[two["id"]], terminal=True,
                                 close_reason="landed")
        with mock.patch.object(landreq, "project",
                               lambda now=None, selector=None: (closed, None)):
            rc, text = self.train()
        self.assertEqual(rc, 0, text)
        self.assertEqual([m[2] for m in self.merges(self.room())], [one_tip])
        self.assertNotIn(two["id"][:12], text)
        self.assertEqual(_rc("-C", self.room(), "merge-base", "--is-ancestor",
                             two_tip, "HEAD"), 1)

    def test_a_door_caution_row_is_excluded_by_name(self):
        """SELF-REVIEW and CONTESTED are the READY word's own door-caution
        rungs (`landreq.ready_rung`: nobody independent looked, or an
        unanswered FIX stands on this tip). No land verb enforces them, so this
        verb does. The word is the ladder's own; this arm fixes it per row
        because the rung's derivation is tests/test_landreq.py's subject and
        this verb's subject is what it does with the word."""
        one, one_tip = self.ready("one", "g")
        two, _two_tip = self.ready("two", "h")
        three, _three_tip = self.ready("three", "i")
        real = landreq.ready_word
        words = {two["id"]: "READY-CONTESTED",
                 three["id"]: "READY-SELF-REVIEW"}

        def word(lr, *args, **kw):
            return words.get(lr.get("id")) or real(lr, *args, **kw)
        with mock.patch.object(landreq, "ready_word", word):
            rc, text = self.train()
        self.assertEqual(rc, 0, text)
        self.assertIn("EXCLUDED %s (lane two): is READY-CONTESTED: an "
                      "unanswered FIX stands on this tip. That is a "
                      "door-caution rung of the READY word, and no land verb "
                      "enforces it, so this verb does" % two["id"][:12], text)
        self.assertIn("EXCLUDED %s (lane three): is READY-SELF-REVIEW: nobody "
                      "independent looked. That is a door-caution rung"
                      % three["id"][:12], text)
        # CONTROL: the row with the ladder's own word is a car and merged.
        self.assertEqual([m[2] for m in self.merges(self.room())], [one_tip])
        self.assertEqual(len(self.spawns), 1)


class UnverifiedIsSortedByItsCause(TrainBase):
    """READY-UNVERIFIED FOLDS SEVERAL UNKNOWNS, and this train's gate answers
    only some of them. An unreadable contributor chain leaves independence
    UNKNOWN, which no suite answers, so that row is excluded; a row that is
    UNVERIFIED for its receipt alone stays a car, with the rung's reason."""

    def test_an_unreadable_contributor_chain_is_excluded_by_name(self):
        one, one_tip = self.ready("one", "g")
        two, two_tip = self.ready("two", "h")
        three, three_tip = self.ready("three", "i")
        real = landreq.chain_contributors

        def unreadable(lr, index=None):
            if lr.get("id") == two["id"]:
                return frozenset(), (), "the chain rows could not be read"
            return real(lr, index=index)
        with mock.patch.object(landreq, "chain_contributors", unreadable):
            # PRECONDITION, on the real projection and the real ladder: row
            # two is still a LIVE READY row, its word is READY-UNVERIFIED, and
            # the cause is its independence reading UNKNOWN. Row one, beside
            # it, reads independent.
            lrs, unavailable = landreq.project()
            self.assertIsNone(unavailable)
            self.assertEqual(lrs[two["id"]]["state"], "READY")
            self.assertFalse(lrs[two["id"]]["terminal"])
            self.assertEqual(landreq.ready_word(lrs[two["id"]]),
                             "READY-UNVERIFIED")
            self.assertIsNone(landreq.independent_review(lrs[two["id"]])[0])
            self.assertIs(landreq.independent_review(lrs[one["id"]])[0], True)
            rc, text = self.train()
        self.assertEqual(rc, 0, text)
        self.assertIn("EXCLUDED %s (lane two): is READY-UNVERIFIED: "
                      "independence UNKNOWN, which this train's gate cannot "
                      "answer" % two["id"][:12], text)
        self.assertIn("the chain rows could not be read", text)
        # CONTROL, on the same observable: the other two rows merged, and the
        # door gated them.
        room = self.room()
        self.assertEqual(sorted(m[2] for m in self.merges(room)),
                         sorted((one_tip, three_tip)))
        self.assertEqual(_rc("-C", room, "merge-base", "--is-ancestor",
                             two_tip, "HEAD"), 1)
        self.assertEqual(len(self.spawns), 1, text)

    def test_a_row_UNVERIFIED_for_its_receipt_alone_is_a_car_with_its_reason(self):  # noqa: VACUOUS_ASSERTION — the one absence (no EXCLUDED line) follows the exact car line with its reason and the merged tip, both positive
        one, one_tip = self.ready("one", "g")
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        lr = lrs[one["id"]]
        # PRECONDITION: READY-UNVERIFIED for the receipt alone. Independence
        # reads True, the row is observable, and its APPROVE carries no gate
        # token, because this fixture's writer is not gate-capable.
        self.assertEqual(landreq.ready_word(lr), "READY-UNVERIFIED")
        self.assertIs(landreq.independent_review(lr)[0], True)
        self.assertIsNot(lr.get("observable"), False)
        self.assertFalse(lr.get("gate"))
        rc, text = self.train()
        self.assertEqual(rc, 0, text)
        # THE RUNG'S REASON RIDES BESIDE THE WORD, on the car's own line.
        self.assertIn("%s  lane one  reviewed tip %s  READY-UNVERIFIED — "
                      "APPROVE has no gate token to check"
                      % (one["id"][:12], one_tip[:12]), text)
        self.assertEqual([m[2] for m in self.merges(self.room())], [one_tip])
        self.assertNotIn("EXCLUDED", text)
        self.assertEqual(len(self.spawns), 1, text)


class TheTrunkIsTheAuthority(TrainBase):
    """THE PLAN STANDS ON A LOCAL SNAPSHOT of trunk. The header prints the
    declared authority, observed now, beside it; `--apply` refuses when they
    differ or the authority is UNKNOWN, and mints and launches nothing."""

    def origin(self):
        """A real origin with trunk pushed, DECLARED as the authority. The
        local snapshot `origin/<main>` then stands at trunk's head."""
        self.add_origin()
        self.git("fetch", "-q", "origin")
        self.git("config", "helm.trunkRemote", "origin")
        return os.path.join(self.tmp, "origin.git")

    def advance(self, bare):
        """Move the remote's trunk from ANOTHER clone, so this checkout's
        snapshot is left behind exactly as a missed fetch leaves it."""
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "-q", "-b", self.main, bare, other],
                       check=True, capture_output=True)
        with open(os.path.join(other, "k"), "w", encoding="utf-8") as fh:
            fh.write("landed elsewhere\n")
        self.git("add", "k", cwd=other)
        self.git("-c", "user.email=other@example.com", "-c", "user.name=Other",
                 "commit", "-q", "-m", "landed elsewhere", cwd=other)
        self.git("push", "-q", "origin", self.main, cwd=other)
        return self.git("rev-parse", "HEAD", cwd=other)

    def nothing_launched(self, text):
        self.assertFalse(os.path.exists(os.path.realpath(self.repo) + "-wt"),
                         text)
        self.assertEqual(self.spawns, [], text)
        self.assertEqual(gatewindow.read_runs(self.store), [])

    def test_a_remote_ahead_of_the_local_snapshot_refuses_apply(self):  # noqa: VACUOUS_ASSERTION — nothing minted or launched is the contract; the refusal naming both shas is positive, and the control after a fetch dispatches once over the remote's head
        rows = self.ready_all()
        ahead = self.advance(self.origin())
        snapshot = self.git("rev-parse", "origin/" + self.main)
        self.assertNotEqual(snapshot, ahead)
        rc, text = self.train(trunk=None)
        self.assertEqual(rc, 1, text)
        self.assertIn("trunk authority: refs/heads/%s on origin at %s "
                      "(DIFFERS FROM the local snapshot)"
                      % (self.main, ahead[:12]), text)
        self.assertIn("this plan stands on origin/%s at %s, but the trunk "
                      "authority refs/heads/%s on origin holds %s: fetch first"
                      % (self.main, snapshot[:12], self.main, ahead[:12]),
                      text)
        self.nothing_launched(text)
        # CONTROL, one fact changed: fetched, the snapshot IS the authority,
        # and the same verb composes over the remote's head and dispatches.
        self.git("fetch", "-q", "origin")
        rc, text = self.train(trunk=None)
        self.assertEqual(rc, 0, text)
        self.assertIn("(AGREES WITH the local snapshot)", text)
        merges = self.merges(self.room())
        self.assertEqual(len(merges), 3, text)
        self.assertEqual(merges[0][1], ahead)
        self.assertEqual(sorted(m[2] for m in merges),
                         sorted(tip for _row, tip in rows))
        self.assertEqual(len(self.spawns), 1, text)

    def test_an_unreadable_authority_refuses_apply_and_the_dry_run_prints_it(self):  # noqa: VACUOUS_ASSERTION — nothing minted or launched is the contract; the UNKNOWN line, the refusal and the reachable-again control that dispatches once are positive
        self.ready_all()
        bare = self.origin()
        shutil.rmtree(bare)
        rc, dry = self.train(apply=False, trunk=None)
        self.assertEqual(rc, 0, dry)
        self.assertIn("trunk authority: UNKNOWN — observing refs/heads/%s on "
                      "remote origin failed" % self.main, dry)
        rc, text = self.train(trunk=None)
        self.assertEqual(rc, 1, text)
        self.assertIn("REFUSED — the trunk authority is UNKNOWN (", text)
        self.assertIn("UNKNOWN is not agreement", text)
        self.nothing_launched(text)
        # CONTROL, one fact changed: the same declared remote, reachable
        # again at the same head, and the same verb dispatches.
        subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        self.git("push", "-q", "origin", self.main)
        rc, text = self.train(trunk=None)
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.spawns), 1, text)

    def test_an_undeclared_authority_refuses_apply(self):  # noqa: VACUOUS_ASSERTION — nothing minted or launched is the contract; the refusal naming the declaration and the re-declared control that dispatches once are positive
        self.ready_all()
        self.git("config", "--unset", "helm.trunkRef")
        rc, text = self.train()
        self.assertEqual(rc, 1, text)
        self.assertIn("trunk authority: UNKNOWN — no trunk authority is "
                      "declared here", text)
        self.assertIn("config helm.trunkRef refs/heads/<branch>", text)
        self.nothing_launched(text)
        # CONTROL: declared again, the same verb dispatches.
        self.git("config", "helm.trunkRef", "refs/heads/" + self.main)
        rc, text = self.train()
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.spawns), 1, text)


class TheDriftCap(TrainBase):
    """A CAR MORE THAN --max-behind COMMITS BEHIND THE TRUNK THE TRAIN STANDS
    ON is skipped and named with its count, "rebase it or close it". The count
    is `rev-list --count <tip>..<trunk>`, and the fixture makes it real: trunk
    moves by genuine commits, never by a mocked count."""

    def trunk_moves(self, n):
        """Move trunk by `n` real empty commits in ONE git process."""
        head = self.git("rev-parse", self.main)
        stream = "".join(
            "commit refs/heads/%s\ncommitter T <t@example.com> %d +0000\n"
            "data 5\nmove\n%s\n"
            % (self.main, 1700000000 + i, "from %s\n" % head if i == 0 else "")
            for i in range(n))
        subprocess.run(["git", "-C", self.repo, "fast-import", "--quiet"],
                       input=stream, text=True, check=True,
                       capture_output=True)
        self.assertEqual(self.git("rev-list", "--count",
                                  "%s..%s" % (head, self.main)), str(n))

    def behind(self, tip):
        return int(self.git("rev-list", "--count", "%s..%s" % (tip, self.main)))

    def drifted(self):
        """(old row, old tip, fresh row, fresh tip): `old` exactly 250
        commits behind trunk, `fresh` exactly 10."""
        old, old_tip = self.ready("old", "g")
        self.trunk_moves(240 - self.behind(old_tip))
        fresh, fresh_tip = self.ready("fresh", "h",
                                      base=self.git("rev-parse", self.main))
        self.trunk_moves(10)
        # PRECONDITION, measured by git and not by the verb.
        self.assertEqual(self.behind(old_tip), 250)
        self.assertEqual(self.behind(fresh_tip), 10)
        return old, old_tip, fresh, fresh_tip

    def test_a_car_250_behind_is_skipped_by_name_at_the_default(self):
        old, old_tip, fresh, fresh_tip = self.drifted()
        trunk = self.git("rev-parse", self.main)
        rc, text = self.train()
        self.assertEqual(rc, 0, text)
        # THE ROOM HOLDS ONE MERGE, the car 10 behind, and never the car 250
        # behind: that is the skip, on the object the gate is spent on.
        room = self.room()
        self.assertEqual([m[2] for m in self.merges(room)], [fresh_tip], text)
        self.assertEqual(_rc("-C", room, "merge-base", "--is-ancestor",
                             old_tip, "HEAD"), 1)
        self.assertEqual(len(self.spawns), 1, text)
        # NAMED, with its count and the cure.
        self.assertIn("EXCLUDED %s (lane old): reviewed tip %s is 250 commits "
                      "behind %s at %s, over the drift cap of 200 "
                      "(--max-behind): rebase it or close it"
                      % (old["id"][:12], old_tip[:12], self.main, trunk[:12]),
                      text)
        self.assertIn("drift cap: a car more than 200 commits behind %s is "
                      "skipped (--max-behind)" % self.main, text)
        # CONTROL, on the same listing: the car 10 behind is a car, with its
        # count on its line.
        self.assertIn("%s  lane fresh  reviewed tip %s"
                      % (fresh["id"][:12], fresh_tip[:12]), text)
        self.assertIn("(10 behind)", text)

    def test_the_same_car_is_a_car_at_max_behind_300(self):  # noqa: VACUOUS_ASSERTION — the one absence (no EXCLUDED line) follows the car's own line with its count and both tips merged, all positive on the same run
        old, old_tip, _fresh, fresh_tip = self.drifted()
        rc, text = self.train(max_behind=300)
        self.assertEqual(rc, 0, text)
        self.assertIn("drift cap: a car more than 300 commits behind", text)
        self.assertIn("%s  lane old  reviewed tip %s"
                      % (old["id"][:12], old_tip[:12]), text)
        self.assertIn("(250 behind)", text)
        self.assertNotIn("EXCLUDED", text)
        self.assertEqual(sorted(m[2] for m in self.merges(self.room())),
                         sorted((old_tip, fresh_tip)))
        # THE BOUNDARY: "more than N" skips N+1 and keeps N.
        rc, dry = self.train(apply=False, name="t250", max_behind=250)
        self.assertEqual(rc, 0, dry)
        self.assertIn("lane old  reviewed tip %s" % old_tip[:12], dry)
        rc, dry = self.train(apply=False, name="t249", max_behind=249)
        self.assertEqual(rc, 0, dry)
        self.assertIn("EXCLUDED %s (lane old): reviewed tip %s is 250 commits "
                      "behind" % (old["id"][:12], old_tip[:12]), dry)
        # AND THROUGH THE CLI: the flag reaches the plan.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landwindow.cmd_train(["--repo", self.repo, "--trunk",
                                       self.main, "--max-behind", "300"])
        self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
        self.assertIn("lane old  reviewed tip %s" % old_tip[:12],
                      out.getvalue())

    def test_a_car_10_behind_rides(self):
        fresh, fresh_tip = self.ready("fresh", "h",
                                      base=self.git("rev-parse", self.main))
        self.trunk_moves(10)
        self.assertEqual(self.behind(fresh_tip), 10)
        rc, text = self.train()
        self.assertEqual(rc, 0, text)
        self.assertIn("%s  lane fresh  reviewed tip %s"
                      % (fresh["id"][:12], fresh_tip[:12]), text)
        self.assertEqual([m[2] for m in self.merges(self.room())], [fresh_tip])
        self.assertEqual(len(self.spawns), 1, text)

    def test_a_drift_git_cannot_count_is_skipped_as_UNKNOWN(self):
        one, one_tip = self.ready("one", "g")
        two, two_tip = self.ready("two", "h")
        real = vcs.backend

        def blind(root):
            be = real(root)

            class Spy:
                def __getattr__(self, name):
                    return getattr(be, name)

                def text(self, cwd, *args, **kw):
                    if args[:2] == ("rev-list", "--count") \
                            and len(args) > 2 and args[2].startswith(two_tip):
                        return 128, "", "fatal: forced count failure (test)"
                    return be.text(cwd, *args, **kw)
            return Spy()
        with mock.patch.object(vcs, "backend", blind):
            rc, text = self.train()
        self.assertEqual(rc, 0, text)
        self.assertIn("EXCLUDED %s (lane two): git could not count how far %s "
                      "is behind %s (drift UNKNOWN)"
                      % (two["id"][:12], two_tip[:12], self.main), text)
        # CONTROL: the row git could count rode.
        self.assertEqual([m[2] for m in self.merges(self.room())], [one_tip])


class TheRoomIsTheRoom(TrainBase):

    def car(self):
        row, tip = self.ready("one", "g")
        return {"id": row["id"], "lane": "one", "tip": tip}

    def test_the_room_assertion_refuses_a_wrong_cwd(self):
        car = self.car()
        be = vcs.backend(self.repo)
        identity = os.path.realpath(os.path.join(self.repo, ".git"))
        before = self.git("rev-parse", "HEAD")
        # (1) A DIRECTORY INSIDE THE SHARED CHECKOUT: git discovers the
        # checkout upward, and a merge would land in it.
        inside = os.path.join(self.repo, "sub")
        os.makedirs(inside)
        outcome, detail, _head = landwindow.merge_car(be, inside, car,
                                                      "train1", identity)
        self.assertEqual(outcome, landwindow.STUCK)
        self.assertIn("to the checkout %s" % os.path.realpath(self.repo),
                      detail)
        # (2) THE SHARED CHECKOUT ITSELF.
        outcome, detail, _head = landwindow.merge_car(be, self.repo, car,
                                                      "train1", identity)
        self.assertEqual(outcome, landwindow.STUCK)
        self.assertIn("not a compose room", detail)
        # (3) A LINKED WORKTREE ON A BRANCH: a merge would move the branch.
        lane_room = os.path.join(self.tmp, "on-a-branch")
        self.git("worktree", "add", "-q", "-b", "somebody", lane_room,
                 self.main)
        outcome, detail, _head = landwindow.merge_car(be, lane_room, car,
                                                      "train1", identity)
        self.assertEqual(outcome, landwindow.STUCK)
        self.assertIn("refs/heads/somebody checked out", detail)
        # NOTHING MOVED, anywhere.
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("rev-parse", "somebody"), before)
        self.assertEqual(self.git("rev-list", "--merges", "--count", "--all"),
                         "0")
        self.assertNotEqual(_rc("-C", self.repo, "rev-parse", "-q", "--verify",
                                "MERGE_HEAD"), 0)
        # CONTROL: the same call on a real detached room of this project merges
        # exactly the reviewed sha onto the room's head.
        room = os.path.join(self.tmp, "room")
        self.git("worktree", "add", "-q", "--detach", room, self.main)
        outcome, detail, head = landwindow.merge_car(be, room, car, "train1",
                                                     identity)
        self.assertEqual(outcome, landwindow.MERGED, detail)
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", head,
                                  cwd=room).split()[1:],
                         [before, car["tip"]])

    def test_a_room_that_stops_being_the_room_stops_the_train_and_launches_nothing(self):  # noqa: VACUOUS_ASSERTION — nothing launched is the contract; the refusal text and the two merge calls are asserted positively first
        self.ready_all()
        real = landwindow.merge_car
        calls = []

        def lose_the_room(be, room, car, train, identity=None):
            calls.append(room)
            if len(calls) == 2:
                # The room's pointer to its repository is gone: git now
                # resolves the path somewhere else, or nowhere.
                os.unlink(os.path.join(room, ".git"))
            return real(be, room, car, train, identity=identity)
        with mock.patch.object(landwindow, "merge_car", lose_the_room):
            rc, text = self.train()
        self.assertEqual(rc, 1, text)
        self.assertIn("STOPPED at", text)
        self.assertIn("the room assertion refused the merge", text)
        self.assertEqual(len(calls), 2)
        # NOTHING LAUNCHED over a room nobody can describe.
        self.assertEqual(self.spawns, [])
        self.assertEqual(gatewindow.read_runs(self.store), [])


class TheDoorDecides(TrainBase):

    def test_the_doors_refusal_propagates_and_is_never_bypassed(self):  # noqa: VACUOUS_ASSERTION — no kill is the contract; the refused train's two merges, the door's rc 3 and text, and the dispatching control are positive
        self.ready("one", "g")
        self.ready("two", "h")
        rc, text = self.train(door=self.door("r1", live=()))
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.spawns), 1)
        # THE SAME WINDOW: trunk has not moved and r1 is still running.
        with mock.patch.object(gatewindow, "launch",
                               wraps=gatewindow.launch) as door:
            rc, text = self.train(name="train2",
                                  door=self.door("r2", live=("r1",)))
        self.assertEqual(rc, 3, text)
        self.assertIn("REFUSED", text)
        self.assertIn("train1", text)
        # NEVER BYPASSED: the door was asked ONCE and never to supersede,
        # nothing was dispatched for it, and nothing was killed. A retry with
        # --supersede is refused by the door too when the room does not contain
        # the running head, so the call record is what shows the verb asked.
        self.assertEqual(door.call_count, 1)
        self.assertFalse(door.call_args[1].get("supersede"))
        self.assertEqual(len(self.spawns), 1, self.spawns)
        self.assertEqual(self.kills, [])
        self.assertEqual(len(gatewindow.read_runs(self.store)), 1)
        # AND NOTHING REMOVED: the refused train's room keeps its merges.
        self.assertEqual(len(self.merges(self.room("train2"))), 2)
        # CONTROL: once the running suite is over, the same verb on the same
        # window dispatches — the refusal was the door's window rule.
        rc, text = self.train(name="train3", door=self.door("r3", live=()))
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.spawns), 2, self.spawns)


class CliSurface(unittest.TestCase):

    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landwindow.cmd_train(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_junk_refuses_before_anything_is_read(self):
        with mock.patch.object(landwindow, "compose") as work:
            for argv in (["--bogus"], ["--bogus", "--apply"],
                         ["--apply", "--bogus", "--help"], ["--name"],
                         ["--repo", "a", "--repo", "b"]):
                rc, _out, err = self.call(argv)
                self.assertEqual(rc, 2, (argv, err))
                self.assertTrue(err, argv)
            self.assertFalse(work.called)
            # CONTROL: a clean tail reaches the verb with what it said.
            rc, _out, _err = self.call(["--name", "train9", "--apply"])
        self.assertEqual(work.call_args[1]["name"], "train9")
        self.assertTrue(work.call_args[1]["apply"])
        self.assertEqual(work.call_args[1]["max_behind"], 200)
        rc, out, _err = self.call(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("helm train", out)
        self.assertIn("--max-behind N", out)

    def test_a_bad_max_behind_refuses_with_rc_2_before_anything_is_read(self):  # noqa: VACUOUS_ASSERTION — the verb never called is the contract; each refusal's rc and sentence are positive, and the control values reach the verb with their number
        with mock.patch.object(landwindow, "compose") as work:
            for value, said in (("abc", "takes a whole number"),
                                ("1.5", "takes a whole number"),
                                ("", "takes a whole number"),
                                ("0", "under the floor of 10"),
                                ("9", "under the floor of 10")):
                rc, _out, err = self.call(["--max-behind", value])
                self.assertEqual(rc, 2, (value, err))
                self.assertIn("--max-behind", err)
                self.assertIn(said, err, value)
            # A NEGATIVE VALUE reads as a flag, and the tail guard refuses it.
            rc, _out, err = self.call(["--max-behind", "-5"])
            self.assertEqual(rc, 2, err)
            self.assertFalse(work.called)
            # CONTROL: the floor itself and a wide cap reach the verb.
            for value in ("10", "300"):
                rc, _out, err = self.call(["--max-behind", value])
                self.assertEqual(work.call_args[1]["max_behind"], int(value),
                                 err)


if __name__ == "__main__":
    unittest.main()
