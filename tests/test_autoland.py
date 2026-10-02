#!/usr/bin/env python3
"""`helm train auto` — THE LANDING TRAIN DRIVES ITSELF, ONE STATE AT A TIME.

Every arm drives the SHIPPED state machine (`autoland.tick`) and fakes only
its seams: the land-request plan, the compose, the audits, the gate door and
its receipts, blame, foldcheck, the git remote, the shared checkout, the
closes, the chat post and the clock. A fake seam records its call, so an arm
reads the ORDER the machine acted in, never a reconstruction of it.

The guard arms (a public or undeclared remote, a fork's URL, a dirty shared
checkout) run the REAL seams against temp repositories, with only the one
network probe (`hostpath_guard._visibility`) replaced.

Each refusal arm carries its control on the same observable: the one changed
fact that makes the same tick act.
"""
import contextlib
import fcntl
import io
import json
import os
import shutil
import signal
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
_tmp_home(prefix="helm-test-autoland-", var="HELM_HOME")

from helm import (autoland, dispatches, eventledger, foldcheck,  # noqa: E402
                  gate, gatecanary, hostpath_guard, landwindow, observed,
                  registry, store, taskkey, tasks, vcs)
from helm.store import load as store_load  # noqa: E402

SRC = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

T0 = 1000000.0
TRUNK = "a" * 40
TIP1, TIP2, TIP3 = "1" * 40, "2" * 40, "3" * 40
ROW1, ROW2, ROW3 = "d1" * 8, "d2" * 8, "d3" * 8
GID = "ab" * 8
RED_GID = "cd" * 8
PREV_GID = "ef" * 8


def _git(cwd, *args):
    proc = subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                          text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _pid(path):
    """The pid a helper wrote to `path`, or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _gone(pid, bound_s):
    """Whether process `pid` is gone (absent or a zombie) within `bound_s`
    seconds, polled."""
    end = time.monotonic() + bound_s
    while True:
        try:
            with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
                state = fh.read().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return True
        if state in ("Z", "X"):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)


def _reap(path):
    """Kill the process whose pid `path` names, if it still runs."""
    pid = _pid(path)
    if pid and not _gone(pid, 0):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _car(rid, lane, tip, basis="source-clean"):
    return {"id": rid, "lane": lane, "tip": tip, "basis": basis,
            "lr": {"id": rid, "lane": lane, "author": "builder-seat",
                   "hold_actor": "reader-seat",
                   "source_clean_tip": tip if basis == "source-clean"
                   else None,
                   "reviewed_tip": tip if basis != "source-clean" else None},
            "word": "SOURCE-CLEAN" if basis == "source-clean" else "READY"}


def _sliced(row, planned):
    """`row` as the sliced kind (v10) stores it: the runner's evidence under
    `slice_authority`, its planned count beside the outcome whose Ran and
    skips the row repeats, every key the real reader
    (`gate._validate_slice`) requires. `planned` is stored as given, so an
    arm can plant a malformed one."""
    auth = {"v": 2, "kind": "gateslice",
            "runner": {"path": gate.SLICE_RUNNER,
                       "files": [{"path": p, "blob": "0" * 40}
                                 for p in gate.SLICE_RUNNER_FILES]},
            "workers": 16, "units": 1, "planned": planned,
            "modules_digest": "a" * 64, "inventory_digest": "b" * 64,
            "assignment_digest": "c" * 64,
            "schedule": "recorded-longest-first", "leak_mode": "fail",
            "leaks": 0, "swept": "empty-after-exit",
            "seconds": {"tests.test_x": 1.0},
            "outcome": {"ran": row["ran"], "skipped": row.get("skipped") or 0,
                        "failures": 0, "errors": 0, "expected_failures": 0,
                        "unexpected_successes": 0, "ok": True}}
    return dict(row, v=gate.SLICE_VERSION, slice_authority=auth)


class FakeOps(autoland.Ops):
    """Every seam the machine touches, recorded in `calls` in call order."""

    def __init__(self, test):
        self.test = test
        self.clock = T0
        self.cars = []
        self.excluded = []
        self.flying = []
        self.calls = []
        self.posts = []
        # the heads whose first post-land ledger read was asked for
        self.postlands = []
        self.receipt_rows = []
        self.conflicts = set()
        self.audit_ok = True
        # the log the pre-gate audits wrote, which blame reads when they
        # fail (task/3674); None is a run whose log was not written
        self.audit_log = None
        self.verify_ok = (True, None)
        self.ast = 19
        self.launch_rc = 0
        self.launch_text = "DISPATCHED"
        self.guard = (True, None)
        self.ff_answer = (True, None)
        self.ff_raises = 0
        self.recheck_answer = ("RED", "fails alone")
        self.blame_answer = None
        # each blame asked: (room, gate id, audit log)
        self.blamed = []
        self.remote = TRUNK
        self.pushes = 0
        self.push_refusals = 0
        self.push_refusal_text = "fatal: the remote end hung up unexpectedly"
        self.guard_answer = (0, "installed")
        self.on_push_guard = None
        # the destination each push-guard read resolves (task/3265 races F1)
        self.target = "private://trunk"
        # the plan reads the REAL ejection store, and each push records what
        # it stood in at that instant (task/3265 races F2)
        self.real_ejections = False
        self.ejected_at_push = None
        # the land's receipt is sliced: its authority is the REAL canary
        # marker's to deny (task/3265 races F3)
        self.sliced = False
        self.tree_rung = foldcheck.PASS
        self.heads = {}
        self.fold_rc = 0
        self.fold_text = None
        self.preclosed = set()
        self.changes_by_tip = {}
        self.closed_rows = []
        self.close_fails = []
        self.closed_tasks = []
        # the LAND step's question to a task's room, and the doors it asks
        # of the chain's other rows (task/3746)
        self.room_posts = []
        self.row_closes = []
        self.row_close_ok = set()
        self.removed = []
        self.flakes = []
        self.b_gate = None
        self.composed = []
        # what `console_walk.surface` would say after the land (task/3444)
        self.walk_line = None
        # THE LANE LEASES A LAND ENDS (task/3674): the claims ledger by lane,
        # each lane's landedness (a lane absent here is landed), what each
        # release answers, every release asked in order, and the lanes whose
        # release crashes the tick once, after it released
        self.leases = {}
        self.landed = {}
        self.release_answers = {}
        self.released = []
        self.release_raises = set()
        # what the web's stop-facts and trunk's tree read (task/3796); None
        # is a home no `helm web` serves
        self.web = None

    # -- time, plan, flight, chat -------------------------------------
    def now(self):
        return self.clock

    def plan(self, root):
        self.calls.append("plan")
        cars = [dict(c) for c in self.cars]
        excluded = [dict(c) for c in self.excluded]
        if self.real_ejections:
            standing, why = landwindow.read_ejections(root)
            self.test.assertIsNone(why, why)
            excluded += [dict(c, why=landwindow.ejection_reason(
                standing[c["tip"]])) for c in cars if c["tip"] in standing]
            cars = [c for c in cars if c["tip"] not in standing]
        return {"root": root, "identity": os.path.join(root, ".git"),
                "ref": "origin/main", "trunk": TRUNK,
                "authority": {"ref": "refs/heads/main", "remote": "origin",
                              "sha": TRUNK, "why": None},
                "max_behind": 200, "ejections_unknown": None,
                "train": "train7",
                "room": self.room_path(root, "train7"),
                "cars": cars, "excluded": excluded}, None

    def in_flight(self, root, mine):
        self.calls.append("in_flight")
        return list(self.flying)

    def post(self, text):
        self.calls.append("post")
        self.posts.append(text)
        return {"id": "row%d" % len(self.posts)}, None

    def address(self, text):
        return "@integrator " + text

    def console_walk(self, root, post, say=None):
        return post(self.walk_line) if self.walk_line else None

    def car_facts(self, root, car):
        return {"task": "task/%d" % (3000 + int(car["tip"][0], 16)),
                "title": "the fleet got lane %s" % car["lane"],
                "priority": "P1", "doors": [], "author": "builder-seat",
                "reader": "reader-seat", "model": "a-model",
                "admit": (True, None)}

    # -- compose, audits, gate ----------------------------------------
    def compose(self, got):
        self.calls.append("compose")
        self.composed.append([c["id"] for c in got["cars"]])
        merged = [c for c in got["cars"] if c["id"] not in self.conflicts]
        refused = [(c, "conflict in helm/x.py") for c in got["cars"]
                   if c["id"] in self.conflicts]
        head = ("c%d" % len(self.composed)) * 20
        self.heads[got["room"]] = head
        return {"head": head if merged else None, "merged": merged,
                "refused": refused, "stuck": None}

    def audits(self, root, room, trunk):
        self.calls.append("audits")
        return self.audit_ok, "audits %s" % ("OK" if self.audit_ok
                                             else "FAILED"), self.audit_log

    def authority(self, root):
        return {"ref": "refs/heads/main", "remote": "origin",
                "sha": self.remote, "why": None}

    def ancestry(self, root, older, newer):
        # What git would answer here: a composed or pushed head is on no
        # trunk but its own, so nothing but TRUNK itself is an ancestor of
        # TRUNK; every other pair descends.
        if newer == TRUNK and older != TRUNK:
            return vcs.NOT_ANCESTOR
        return vcs.ANCESTOR

    def head_of(self, room):
        return self.heads.get(room)

    def launch(self, room, name, trunk):
        self.calls.append("launch")
        # FAB KEYS THE JOB by its tree and attempt, and each recorded flake
        # of the tree is one more attempt (gatewindow.job_identity): a
        # relaunch with no new flake is answered by the same job.
        return self.launch_rc, {"host": "host-a",
                                "job_id": "gate-x%d" % len(self.flakes),
                                "room": room}, self.launch_text

    def gate_of(self, room):
        # what the landing-window door recorded for the gate blame's compose
        # launched on a b-room: unrecorded unless a test names it
        return dict(self.b_gate or {"host": None, "job_id": None})

    def receipts(self):
        return list(self.receipt_rows), None

    def verify(self, root, head, gid):
        self.calls.append("verify")
        if self.sliced:
            why = gate.sliced_land_disabled()
            return why is None, why
        return self.verify_ok

    def ast_delta(self, root, trunk, head):
        return self.ast, None

    def red_facts(self, room, gid):
        return ({"room": room}, {"id": gid, "tree": "e" * 40,
                                 "mode": "sliced",
                                 "tests": [{"id": "tests.test_x.T.test_y"}],
                                 "modules": ["tests.test_x"]}, None)

    def recheck(self, st, red):
        self.calls.append("recheck")
        return self.recheck_answer

    def audit_recheck(self, st, log):
        self.calls.append("audit_recheck")
        return self.recheck_answer

    def record_flake(self, root, record):
        self.calls.append("record_flake")
        self.flakes.append(record)
        return record, None

    def blame(self, room, gid, audits=None):
        self.calls.append("blame")
        self.blamed.append((room, gid, audits))
        return self.blame_answer

    # -- the land -------------------------------------------------------
    def foldcheck(self, root, head, gid, target=None):
        self.calls.append("foldcheck")
        return [foldcheck.Rung("tip-exists", foldcheck.PASS, "ok"),
                foldcheck.Rung("tree-vs-gate", self.tree_rung, "tree"),
                foldcheck.Rung("ff-able", foldcheck.PASS, "ok"),
                foldcheck.Rung("head-clean", foldcheck.REFUSE, "not yet"),
                foldcheck.Rung("origin-has-it", foldcheck.REFUSE, "not yet")]

    def push_target(self, root):
        self.calls.append("push_guard")
        target = self.target
        if self.on_push_guard:
            hook, self.on_push_guard = self.on_push_guard, None
            hook()
        ok, why = self.guard
        return (target, why) if ok else (None, why)

    def push_guard(self, root):
        target, why = self.push_target(root)
        return target is not None, why

    def remote_head(self, root):
        return self.remote

    def settled_trunk(self, root, target):
        # the one read after an ambiguous push (helm-codex B1, B3)
        self.calls.append("settled_trunk")
        self.test.assertEqual(target, "private://trunk")
        return self.remote, None

    def push(self, root, head, target, keep=(), lease=None):
        self.calls.append("push")
        self.test.assertEqual(target, "private://trunk")
        if self.real_ejections:
            standing, why = landwindow.read_ejections(root)
            self.test.assertIsNone(why, why)
            self.ejected_at_push = set(standing)
        if self.push_refusals:
            self.push_refusals -= 1
            return False, self.push_refusal_text
        # the remote's own compare-and-swap (door read B1): with a lease the
        # update goes only while trunk still reads it; with none it is a
        # plain push, which takes any fast-forward
        if lease is not None and lease != self.remote:
            return False, (" ! [rejected]        %s -> main (stale info)"
                           % head)
        self.pushes += 1
        self.remote = head
        return True, "pushed"

    def ff(self, root, head):
        self.calls.append("ff")
        if self.ff_raises:
            self.ff_raises -= 1
            raise RuntimeError("injected crash after the push")
        return self.ff_answer

    def install_guard(self, root):
        self.calls.append("install_guard")
        return self.guard_answer

    def postland(self, root, head):
        self.calls.append("postland")
        self.postlands.append(head)

    def source_clean_closed(self, root, rid, tip, head):
        return rid in self.preclosed

    def fold_apply(self, root, head, gid, train_car_ids=()):
        self.calls.append("fold_apply")
        if self.fold_text is not None:
            return self.fold_rc, self.fold_text
        lines = ["ok    tip-exists     x", "ok    tree-vs-gate   x",
                 "ok    ff-able        x", "ok    head-clean     x",
                 "ok    origin-has-it  %s is on origin/main" % head[:12],
                 "all five PASSED — this fold is provable", "",
                 "SOURCE-CLEAN HOLDS THIS HEAD LANDS (gate:%s, APPLY):" % gid]
        lines += ["  CLOSED    %s  tip %s  held by reader-seat — ok"
                  % (c["id"], c["tip"][:12]) for c in self.cars
                  if c["basis"] == "source-clean"]
        return 0, "\n".join(lines)

    def lr_close(self, rid, live, restart):
        self.calls.append("lr_close")
        if self.close_fails:
            raise autoland.CloseFailed(self.close_fails.pop(0))
        self.closed_rows.append((rid, live, restart))
        return {"id": rid}, None

    def task_room_post(self, room, text, key):
        self.calls.append("task_room_post")
        if key not in {k for _room, _text, k in self.room_posts}:
            self.room_posts.append((room, text, key))
        return None

    def lr_row_close(self, rid, reason, evidence, live=None, restart=None):
        self.calls.append("lr_row_close")
        self.row_closes.append((rid, reason))
        return None if (rid, reason) in self.row_close_ok \
            else "the %s door refused in the fake" % reason

    def task_close(self, task, reason):
        self.calls.append("task_close")
        self.closed_tasks.append((task, reason))
        return {"id": task}, None

    def changes(self, root, trunk, tip):
        return self.changes_by_tip.get(tip, {"helm/plain.py": "+x = 1\n"}), None

    def remove_room(self, root, room):
        self.calls.append("remove_room")
        self.removed.append(room)
        return True, None

    # -- the lane leases a land ends, the web it follows (task/3674) ----
    def lane_lease(self, root, lane):
        self.calls.append("lane_lease")
        row = self.leases.get(lane)
        return (dict(row), None) if row else (None, None)

    def lane_landed(self, root, lane):
        state = self.landed.get(lane, vcs.ANCESTOR)
        return state, "lane/%s reads %s" % (lane, state)

    def release_lease(self, root, lane, holder, lease):
        self.calls.append("release_lease")
        self.released.append((lane, holder, lease))
        ok, text = self.release_answers.get(
            lane, (True, "helm work: worktree:proj:%s released" % lane))
        if ok:
            self.leases.pop(lane, None)
        if lane in self.release_raises:
            self.release_raises.discard(lane)
            raise RuntimeError("injected crash after the release of %s"
                               % lane)
        return ok, text

    def web_code(self, root):
        self.calls.append("web_code")
        return dict(self.web or {"why": "no stop-facts have been written "
                                        "(is `helm web` running?)"})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-autoland-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home"),
            "HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted"),
            autoland.VETO_ENV: "300"})
        env.start()
        self.addCleanup(env.stop)
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
        self.root = os.path.realpath(self.repo)
        self.ops = FakeOps(self)
        # THE INTEGRATOR'S ONE-TIME SEED: no train starts without a counter.
        autoland.seed_counter(self.root, 383, TRUNK)

    def tick(self, apply=True):
        out = io.StringIO()
        rc = autoland.tick(self.repo, apply=apply, ops=self.ops, out=out)
        return rc, out.getvalue()

    def current(self):
        got, why = autoland.active(self.root)
        self.assertIsNone(why, why)
        return got

    def archived(self):
        return autoland.archived(self.root)

    def state_files(self):
        found = []
        for base, _dirs, files in os.walk(autoland.state_dir(self.root)):
            found += [os.path.join(base, f) for f in files]
        return sorted(found)

    def to_gating(self, cars=None):
        """IDLE -> INTENT -> (window) -> composed, audited, launched."""
        self.ops.cars = cars or [_car(ROW1, "one", TIP1),
                                 _car(ROW2, "two", TIP2)]
        self.tick()
        self.ops.clock += 301
        self.tick()
        st = self.current()
        self.assertEqual(st["state"], autoland.GATING, st)
        return st

    def green(self, st, ran=25268, prev_ran=25249, planned=None,
              prev_planned=None):
        """Trunk's green receipt and the train's. A planned count given
        makes that receipt the sliced kind carrying it; None leaves it a
        serial receipt, which carries none."""
        prev = {"id": PREV_GID, "status": "OK", "suite": True,
                "ran": prev_ran, "head": TRUNK, "tree": "b" * 40,
                "ts": "2026-01-01T00:00:00Z"}
        row = {"id": GID, "status": "OK", "suite": True, "ran": ran,
               "head": st["head"], "tree": "f" * 40,
               "ts": "2026-01-01T01:00:00Z"}
        self.ops.receipt_rows = [
            prev if prev_planned is None else _sliced(prev, prev_planned),
            row if planned is None else _sliced(row, planned)]


class IdleArms(Base):
    def test_idle_with_no_cars_posts_nothing(self):
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertIn("plan", self.ops.calls)
        self.assertIn("no car is READY", out)
        self.assertEqual(self.ops.posts, [])
        self.assertIsNone(self.current())
        self.assertEqual(self.state_files(), [])

    def test_idle_with_cars_posts_one_intent_and_records_it(self):
        self.ops.cars = [_car(ROW1, "one", TIP1), _car(ROW2, "two", TIP2)]
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.ops.posts), 1, self.ops.posts)
        post = self.ops.posts[0]
        self.assertIn("@integrator", post)
        self.assertIn("INTENT", post)
        self.assertIn("train7", post)
        for lane, tip, row in (("one", TIP1, ROW1), ("two", TIP2, ROW2)):
            self.assertIn(lane, post)
            self.assertIn(tip[:12], post)
            self.assertIn(row[:12], post)
        self.assertIn("helm train veto train7 --reason", post)
        st = self.current()
        self.assertEqual(st["state"], autoland.INTENT)
        self.assertEqual(st["intent_ts"], T0)
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1, ROW2])
        # A SECOND TICK IN THE WINDOW posts nothing more and composes nothing.
        self.ops.clock += 60
        self.tick()
        self.assertEqual(len(self.ops.posts), 1, self.ops.posts)
        self.assertNotIn("compose", self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.INTENT)


class Preflight(Base):
    """What would stop a land after its gate is spent is asked before a
    train starts."""

    def test_an_unseeded_counter_starts_no_train(self):
        os.remove(autoland.counter_path(self.root))
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        rc, out = self.tick()
        self.assertEqual(rc, 1, out)
        self.assertIsNone(self.current())
        refused = [p for p in self.ops.posts if "seed" in p]
        self.assertEqual(len(refused), 1, self.ops.posts)
        self.assertNotIn("INTENT", refused[0])
        self.tick()
        self.assertEqual(len(self.ops.posts), 1, self.ops.posts)
        # the control: seeded, the same tick posts the intent
        autoland.seed_counter(self.root, 383, TRUNK)
        self.tick()
        self.assertIn("INTENT", self.ops.posts[-1])

    def test_a_push_the_guard_refuses_starts_no_train(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.ops.guard = (False, "remote origin reads PUBLIC")
        rc, out = self.tick()
        self.assertEqual(rc, 1, out)
        self.assertIsNone(self.current())
        self.assertIn("PUBLIC", self.ops.posts[0])
        self.ops.guard = (True, None)
        self.tick()
        self.assertEqual(self.current()["state"], autoland.INTENT)


class VetoArms(Base):
    def test_a_veto_inside_the_window_vetoes_and_nothing_composes(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        row, why = autoland.veto(self.root, "train7", "a-seat", "not today")
        self.assertIsNone(why, why)
        self.assertEqual(row["veto"]["by"], "a-seat")
        self.assertEqual(row["veto"]["reason"], "not today")
        self.ops.clock += 301
        self.tick()
        self.assertNotIn("compose", self.ops.calls)
        self.assertIsNone(self.current())
        done = self.archived()
        self.assertEqual([d["state"] for d in done], [autoland.VETOED])
        self.assertTrue(any("VETOED" in p and "not today" in p
                            for p in self.ops.posts), self.ops.posts)
        # The control: without the veto, the same window composes.
        self.ops.clock += 1
        self.tick()
        self.ops.clock += 301
        self.tick()
        self.assertIn("compose", self.ops.calls)

    def test_a_veto_past_intent_is_refused(self):
        self.to_gating()
        row, why = autoland.veto(self.root, "train7", "a-seat", "too late")
        self.assertIsNone(row)
        self.assertIn("past INTENT", why)

    def test_a_veto_names_its_train(self):
        row, why = autoland.veto(self.root, "train99", "a-seat", "r")
        self.assertIsNone(row)
        self.assertIn("train99", why)

    def test_the_veto_verb_records_who_vetoed(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "the-vetoer"}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = autoland.cmd_veto(["train7", "--reason", "hold it",
                                    "--repo", self.repo])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(self.current()["veto"]["by"], "the-vetoer")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(autoland.cmd_veto(["train7"]), 2)

    def test_a_veto_written_while_the_intent_posts_is_kept(self):
        """The veto verb writes the file between the tick's read and its
        next write: that write must not erase it."""
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        root, real_post = self.root, self.ops.post

        def post_then_veto(text):
            got = real_post(text)
            _row, why = autoland.veto(root, "train7", "a-seat", "mid-post")
            self.assertIsNone(why, why)
            return got

        self.ops.post = post_then_veto
        self.tick()
        self.ops.post = real_post
        self.assertEqual(self.current()["veto"]["reason"], "mid-post")
        self.ops.clock += 301
        self.tick()
        self.assertNotIn("compose", self.ops.calls)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.VETOED])

    def test_a_veto_that_lands_as_the_window_closes_still_vetoes(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        root, real_plan = self.root, self.ops.plan

        def plan_then_veto(where):
            got = real_plan(where)
            autoland.veto(root, "train7", "a-seat", "at the bell")
            return got

        self.ops.plan = plan_then_veto
        self.ops.clock += 301
        self.tick()
        self.assertIn("plan", self.ops.calls)
        self.assertNotIn("compose", self.ops.calls)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.VETOED])


class Admission(Base):
    """Source-clean door admission uses anchored hold evidence, not today's
    model, runtime or live approval-tier resolver; current owner policy can
    veto a proven hold but cannot retroactively grant one."""

    FACTS = {"task": "task/1", "priority": "P1", "doors": ["guard"],
             "author": "builder-seat", "reader": "reader-seat"}

    def setUp(self):
        super().setUp()
        projects = mock.patch.object(registry, "load",
                                     return_value={"projects": {}})
        projects.start()
        self.addCleanup(projects.stop)

    @staticmethod
    def policy(*members):
        return {"id": "approval-test", "class": "certain",
                "_policy_confidence_valid": True,
                "_policy_source_valid": True, "policy_kind": "approval-tier",
                "policy_members": list(members), "policy_reason": "test owner rule"}

    def hold_row(self, family="codex", model="a-model", recorded=None):
        authority = {"v": 5, "identity": "reader-seat",
                     "roster_identity": "reader-seat", "session": "hold-session",
                     "runtime": {"backend": "native", "family": family},
                     "runtime_verified": True}
        row = {"id": ROW1, "hold_actor": "reader-seat", "repo_root": self.root,
               "source_clean_tip": TIP1, "hold_ts": "2026-09-01T00:00:00Z"}
        proof = {"v": 1, "row": ROW1, "actor": "reader-seat",
                 "tip": TIP1, "ts": row["hold_ts"], "family": family,
                 "model": model, "authority": authority,
                 "policy": recorded or self.policy("family:" + family)}
        proof["anchor"] = dispatches._proof_anchor(
            "source-clean-holder-v1", proof)
        row["hold_approval"] = proof
        return row

    def admission(self, row=None, current=None, reads=(False, None), **facts):
        from helm import reviewer_eligibility
        self.asked = []

        def input_only(model, family=None):
            self.asked.append((model, family))
            return reads(model, family) if callable(reads) else reads

        with mock.patch.object(store_load, "_policy_hits",
                               return_value=[current or self.policy("family:codex")]), \
                mock.patch.object(reviewer_eligibility, "input_only", input_only):
            return autoland.Ops()._door_admission(
                self.root, dict(self.FACTS, hold_row=row, **facts))

    def test_a_proven_tier_reader_carries_a_door(self):
        self.assertEqual(self.admission(self.hold_row()), (True, None))
        self.assertEqual(self.asked, [("a-model", "codex")])

    def test_a_bare_call_without_hold_proof_fails_closed(self):
        ok, why = self.admission()
        self.assertFalse(ok)
        self.assertIn("no proven approval-tier holder at the hold", why)
        self.assertEqual(self.asked, [])

    def test_a_reader_outside_the_current_tier_does_not_carry_a_door(self):
        row = self.hold_row()
        ok, why = self.admission(row, current=self.policy("family:claude"))
        self.assertFalse(ok)
        self.assertIn("reader-seat", why)
        self.assertIn("current approval tier", why)
        # A later widening cannot grant a hold denied by its recorded policy.
        row = self.hold_row(recorded=self.policy("family:claude"))
        ok, why = self.admission(row)
        self.assertFalse(ok)
        self.assertIn("not admitted at the hold", why)

    def test_an_input_only_reader_does_not_carry_a_door(self):
        ok, why = self.admission(self.hold_row(), reads=(True, "a local model"))
        self.assertFalse(ok)
        self.assertIn("input only", why)

    def test_no_current_approval_tier_policy_does_not_carry_a_door(self):
        with mock.patch.object(store_load, "_policy_hits", return_value=[]):
            ok, why = autoland.Ops()._door_admission(
                self.root, dict(self.FACTS, hold_row=self.hold_row()))
        self.assertFalse(ok)
        self.assertIn("unavailable", why)
        self.assertIn("no live policy declares kind approval-tier", why)
        self.assertTrue(self.admission(self.hold_row())[0])

    def test_a_current_policy_that_raises_does_not_carry_a_door(self):
        with mock.patch.object(store_load, "_policy_hits",
                               side_effect=OSError("policy store unreadable")):
            ok, why = autoland.Ops()._door_admission(
                self.root, dict(self.FACTS, hold_row=self.hold_row()))
        self.assertFalse(ok)
        self.assertIn("unreadable (OSError)", why)

    def test_an_unread_hold_model_does_not_carry_a_door(self):
        ok, why = self.admission(self.hold_row(model=None))
        self.assertFalse(ok)
        self.assertIn("runtime is unproven", why)
        self.assertTrue(self.admission(self.hold_row())[0])

    def test_the_frozen_holders_family_reaches_the_input_only_check(self):
        def by_family(model, family):
            return ((True, "family %s reads as input only" % family)
                    if family == "gemini" else (False, None))

        row = self.hold_row(family="gemini", model="mystery-model")
        ok, why = self.admission(row, current=self.policy("family:gemini"),
                                 reads=by_family, family="codex",
                                 model="a different live model")
        self.assertEqual(self.asked, [("mystery-model", "gemini")])
        self.assertFalse(ok)
        self.assertIn("input only", why)
        row = self.hold_row(model="mystery-model")
        self.assertTrue(self.admission(row, reads=by_family)[0])

    def test_a_read_that_cannot_be_ruled_out_as_input_only_is_refused(self):
        ok, why = self.admission(self.hold_row(),
                                 reads=(None, "cannot rule out codex-spark"))
        self.assertFalse(ok)
        self.assertIn("input only or unknown", why)

    def test_the_real_facts_use_frozen_holder_identity_not_live_resolvers(self):
        from helm import review_door, reviewer_eligibility
        row = self.hold_row(family="gemini", model="mystery-model")
        seen = []

        def input_only(model, family=None):
            seen.append((model, family))
            return False, None

        with mock.patch.object(dispatches, "snapshot",
                               return_value=({ROW1: row}, None)), \
                mock.patch.object(review_door, "lane_doors",
                                  return_value={"doors": [("guard", None)]}), \
                mock.patch.object(dispatches, "approval_tier",
                                  side_effect=AssertionError("live tier read")), \
                mock.patch.object(reviewer_eligibility, "read_model",
                                  side_effect=AssertionError("live model read")), \
                mock.patch.object(store_load, "_policy_hits",
                                  return_value=[self.policy("family:gemini")]), \
                mock.patch.object(reviewer_eligibility, "input_only", input_only):
            facts = autoland.Ops().car_facts(self.root,
                                             _car(ROW1, "one", TIP1))
        self.assertEqual((facts["model"], facts["family"]),
                         ("mystery-model", "gemini"))
        self.assertEqual(facts["admit"], (True, None))
        self.assertEqual(seen, [("mystery-model", "gemini")])

    def test_an_altered_hold_proof_cannot_be_reanchored_by_the_reader(self):
        row = self.hold_row()
        row["hold_approval"]["model"] = "another-model"
        ok, why = self.admission(row)
        self.assertFalse(ok)
        self.assertIn("does not match its anchor", why)

    def test_a_barred_car_stays_out_of_the_intent_and_is_posted_once(self):
        self.ops.cars = [_car(ROW1, "one", TIP1), _car(ROW2, "two", TIP2)]
        real = self.ops.car_facts

        def facts(root, car):
            got = real(root, car)
            if car["id"] == ROW2:
                got["admit"] = (False, "a DOOR whose holder is outside")
            return got

        self.ops.car_facts = facts
        self.tick()
        st = self.current()
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1])
        barred = [p for p in self.ops.posts if "does not ride" in p]
        self.assertEqual(len(barred), 1, self.ops.posts)
        self.assertIn("two", barred[0])
        self.ops.clock += 60
        self.ops.calls = []
        self.tick()
        self.assertEqual(len([p for p in self.ops.posts
                              if "does not ride" in p]), 1)


class PushAdmission(Base):
    """A pushed train records its exact current DOOR hold only on git success."""

    def setUp(self):
        super().setUp()
        from helm import dispatches_tier, reviewer_eligibility
        self.row = Admission.hold_row(
            self, recorded=Admission.policy("family:codex"))
        self.row.update(status="held", lane="one")
        self.car = _car(ROW1, "one", TIP1)
        self.car["lr"]["hold_ts"] = self.row["hold_ts"]
        self.ops.target = autoland.PushTarget(
            "private://trunk", "refs/heads/main", "origin")
        push = self.ops.push
        settle = self.ops.settled_trunk
        self.ops.push = lambda root, head, target, keep=(), lease=None: push(
            root, head, target.url, keep, lease=lease)
        self.ops.settled_trunk = lambda root, target: settle(root, target.url)
        real = self.ops.car_facts
        self.ops.car_facts = lambda root, car: dict(real(root, car),
                                                     doors=["guard"])
        for patch in (mock.patch.object(registry, "load",
                                        return_value={"projects": {}}),
                      mock.patch.object(store_load, "_policy_hits",
                                        return_value=[Admission.policy(
                                            "family:codex")]),
                      mock.patch.object(reviewer_eligibility, "input_only",
                                        return_value=(False, None))):
            patch.start()
            self.addCleanup(patch.stop)
        snap = mock.patch.object(dispatches, "snapshot",
                                 return_value=({ROW1: self.row}, None))
        self.snapshot = snap.start()
        self.addCleanup(snap.stop)
        self.assertTrue(dispatches_tier.hold_approval(
            self.row, self.root)[0])

    def landing(self):
        st = self.to_gating([self.car])
        self.green(st)
        self.ops.ff_answer = (False, "pause after push")
        self.ops.calls = []
        return st

    def test_successful_last_word_saves_the_current_hold_with_pushed(self):
        st = self.landing()
        seen = []
        push = self.ops.push

        def before_push(*args, **kwargs):
            sending = self.current()
            self.assertTrue(sending["sending"])
            self.assertNotIn("push_admission", sending)
            return push(*args, **kwargs)

        self.ops.push = before_push
        save = autoland._Tick.save

        def record(tick, state, note=None, create=False):
            save(tick, state, note, create)
            if note and note.startswith("pushed "):
                seen.append(autoland.read_train(self.root, state["train"]))

        with mock.patch.object(autoland._Tick, "save", record):
            self.tick()
        self.assertEqual(self.ops.pushes, 1)
        self.assertEqual(len(seen), 1)
        recorded = seen[0]
        self.assertEqual(recorded["step"], "pushed")
        marker = recorded["push_admission"]
        self.assertEqual(marker, {
            "v": 2, "repo": self.root,
            "repo_id": dispatches._repo_info(self.root)["repo_id"],
            "head": st["head"], "gate": GID,
            "target": "refs/heads/main at private://trunk", "remote": "origin",
            "cars": [{"id": ROW1, "lane": "one", "tip": TIP1,
                      "hold_ts": self.row["hold_ts"],
                      "hold_actor": self.row["hold_actor"],
                      "anchor": self.row["hold_approval"]["anchor"]}]})
        self.assertEqual(self.current()["push_admission"], marker)

    def test_marker_remote_is_the_one_in_the_vetted_push_target(self):
        self.ops.target = autoland.PushTarget(
            "private://trunk", "refs/heads/main", "upstream")
        self.landing()
        self.tick()
        marker = self.current()["push_admission"]
        self.assertEqual(self.ops.pushes, 1)
        self.assertEqual(marker["remote"], "upstream")
        self.assertEqual(marker["target"],
                         "refs/heads/main at private://trunk")

    def test_untyped_successful_push_mints_no_door_authority(self):
        self.ops.target = "private://trunk"
        self.ops.push = FakeOps.push.__get__(self.ops)
        self.landing()
        self.tick()
        self.assertEqual(self.ops.pushes, 1)
        train = self.current()
        self.assertEqual(train["step"], "pushed")
        self.assertNotIn("push_admission", train)

    def test_final_door_added_after_compose_is_in_push_snapshot(self):
        self.ops.car_facts = FakeOps.car_facts.__get__(self.ops)
        self.landing()
        real = self.ops.car_facts
        self.ops.car_facts = lambda root, car: dict(real(root, car),
                                                     doors=["guard"])
        self.tick()
        self.assertEqual(self.ops.pushes, 1)
        cars = self.current()["push_admission"]["cars"]
        self.assertGreater(len(cars), 0)
        self.assertEqual(cars[0]["id"], ROW1)

    def test_remote_already_carrying_head_mints_no_admission(self):
        st = self.landing()
        self.ops.remote = st["head"]
        self.tick()
        self.assertEqual(self.ops.pushes, 0)
        self.assertIn("ff", self.ops.calls)
        train = self.current()
        self.assertTrue(train)
        self.assertEqual(train["step"], "pushed")
        self.assertNotIn("push_admission", train)

    def test_ambiguous_push_settling_at_head_mints_no_admission(self):
        st = self.landing()

        def lost_answer(root, head, target, keep=(), lease=None):
            self.ops.remote = head
            return False, "lost answer"

        self.ops.push = lost_answer
        with mock.patch.object(autoland, "PUSH_SETTLE_S", 0):
            self.tick()
        train = self.current()
        self.assertEqual(train["step"], "pushed")
        self.assertEqual(self.ops.remote, st["head"])
        self.assertIn("settled_trunk", self.ops.calls)
        self.assertNotIn("push_admission", train)

    def test_mismatched_ledger_row_stops_before_push(self):
        st = self.landing()
        self.row["lane"] = "another-lane"
        self.tick()
        self.assertEqual(self.ops.pushes, 0)
        self.assertIn("plan", self.ops.calls)
        train = self.current()
        self.assertTrue(train)
        self.assertEqual(train["stopped"]["step"], "verified")
        self.assertNotIn("push_admission", train)
        self.assertIn("matching standing hold", train["stopped"]["why"])

    def test_mismatched_hold_identity_stops_before_push(self):
        self.landing()
        self.row["hold_actor"] = "different-holder"
        self.tick()
        self.assertEqual(self.ops.pushes, 0)
        self.assertIn("plan", self.ops.calls)
        train = self.current()
        self.assertTrue(train)
        self.assertNotIn("push_admission", train)
        self.assertIn("not admitted", train["stopped"]["why"])

    def test_unreadable_door_hold_stops_before_push(self):
        self.landing()
        self.snapshot.return_value = (None, "ledger unreadable")
        self.tick()
        self.assertEqual(self.ops.pushes, 0)
        self.assertIn("plan", self.ops.calls)
        train = self.current()
        self.assertTrue(train)
        self.assertNotIn("push_admission", train)
        self.assertIn("ledger unreadable", train["stopped"]["why"])


class PushAdmissionNonDoor(Base):
    def test_non_door_car_pushes_with_unreadable_ledger(self):
        st = self.to_gating([_car(ROW1, "one", TIP1)])
        self.green(st)
        self.ops.ff_answer = (False, "pause after push")
        with mock.patch.object(dispatches, "snapshot",
                               side_effect=OSError("ledger unreadable")):
            self.tick()
        self.assertEqual(self.ops.pushes, 1)
        train = self.current()
        self.assertEqual(train["step"], "pushed")
        self.assertNotIn("push_admission", train)

    def test_non_door_unreadable_checkout_identity_does_not_stop_push(self):
        st = self.to_gating([_car(ROW1, "one", TIP1)])
        self.green(st)
        self.ops.ff_answer = (False, "pause after push")
        with mock.patch.object(dispatches, "_repo_info", return_value=None):
            self.tick()
        self.assertEqual(self.ops.pushes, 1)
        train = self.current()
        self.assertTrue(train)
        self.assertEqual(train["step"], "pushed")
        self.assertNotIn("push_admission", train)


class NativeHoldAdmission(Base):
    """The native transcript is read by the hold-proof WRITER at the hold's
    recorded time, not by compose. A later Opus turn cannot upgrade a Sonnet
    hold, and an ambiguous subagent turn cannot mint a holder proof."""

    T0 = 1790500000.0
    SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.tmp, "claude")
        env = mock.patch.dict(os.environ, {"HELM_CLAUDE_DIR": self.claude})
        env.start()
        self.addCleanup(env.stop)
        projects = mock.patch.object(registry, "load",
                                     return_value={"projects": {}})
        projects.start()
        self.addCleanup(projects.stop)

    def transcript(self, turns):
        directory = os.path.join(self.claude, "projects", "proj")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "%s.jsonl" % self.SESSION), "w",
                  encoding="utf-8") as fh:
            for epoch, name in turns:
                fh.write(json.dumps(
                    {"type": "assistant", "sessionId": self.SESSION,
                     "timestamp": time.strftime(
                         "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch)),
                     "message": {"role": "assistant", "model": name}}) + "\n")

    def facts(self, hold_epoch):
        from helm import dispatches_tier, home, review_door
        evidence = {"v": 5, "identity": "reader-seat",
                    "roster_identity": "reader-seat", "session": self.SESSION,
                    "runtime": {"agent_harness": "claude", "family": "claude",
                                "backend": "native"},
                    "runtime_verified": True}
        car = _car(ROW1, "one", TIP1)
        stamp = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(hold_epoch))
                 if hold_epoch is not None else "")
        car["lr"]["hold_ts"] = stamp
        row = {"id": ROW1, "hold_actor": "reader-seat", "repo_root": self.root,
               "source_clean_tip": TIP1, "hold_ts": stamp}
        policy = Admission.policy("family:claude")
        with mock.patch.object(home, "session_id", return_value=self.SESSION), \
                mock.patch.object(dispatches,
                                  "_approval_identity_family_evidence",
                                  return_value=({"claude"}, evidence,
                                                dispatches._subsumed_family_anchor(evidence),
                                                None)), \
                mock.patch.object(store_load, "_policy_hits",
                                  return_value=[policy]):
            proof = dispatches_tier.record_hold_approval(
                dict(row, repo_root=self.root), "reader-seat", TIP1, stamp)
        if proof:
            row["hold_approval"] = proof
        # Compose reads the frozen row, even if today's transcript, roster or
        # upstream now says something else. It may still read current policy.
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({ROW1: row}, None)), \
                mock.patch.object(review_door, "lane_doors",
                                  return_value={"doors": [("guard", None)]}), \
                mock.patch.object(dispatches, "approval_tier",
                                  side_effect=AssertionError("live tier read")), \
                mock.patch.object(store_load, "_policy_hits",
                                  return_value=[policy]):
            facts = autoland.Ops().car_facts(self.root, car)
        return facts, car

    def test_a_native_opus_hold_rides(self):
        self.transcript([(self.T0, "claude-opus-5-5"),
                         (self.T0 + 300, "claude-opus-5-5")])
        facts, car = self.facts(self.T0 + 320)
        self.assertEqual(facts["model"], "claude-opus-5-5")
        self.assertEqual(facts["family"], "claude")
        self.assertEqual(facts["admit"], (True, None))
        self.assertEqual(facts["hold_row"]["hold_approval"]["ts"],
                         car["lr"]["hold_ts"])
        self.assertEqual(facts["hold_row"]["hold_approval"]["policy"],
                         Admission.policy("family:claude"))

    def test_a_native_sonnet_hold_does_not_ride(self):
        self.transcript([(self.T0, "claude-sonnet-5"),
                         (self.T0 + 300, "claude-sonnet-5")])
        facts, _car_ = self.facts(self.T0 + 320)
        self.assertIsNone(facts["model"])
        self.assertNotIn("hold_approval", facts["hold_row"])
        ok, why = facts["admit"]
        self.assertFalse(ok)
        self.assertIn("no proven approval-tier holder at the hold", why)

    def test_a_hold_in_the_sonnet_phase_does_not_ride_as_the_later_opus(self):
        # THE FAIL-OPEN ARM: Sonnet until T0+1200, Opus from T0+2400.
        self.transcript([(self.T0, "claude-sonnet-5"),
                         (self.T0 + 600, "claude-sonnet-5"),
                         (self.T0 + 1200, "claude-sonnet-5"),
                         (self.T0 + 2400, "claude-opus-5-5"),
                         (self.T0 + 3000, "claude-opus-5-5")])
        # POSITIVE CONTROL on the same transcript: a hold in the Opus phase
        # rides, so the refusal below is the hold's moment.
        facts, _car_ = self.facts(self.T0 + 3060)
        self.assertEqual((facts["model"], facts["admit"]),
                         ("claude-opus-5-5", (True, None)))
        facts, _car_ = self.facts(self.T0 + 1260)
        self.assertIsNone(facts["model"])
        self.assertNotIn("hold_approval", facts["hold_row"])
        ok, why = facts["admit"]
        self.assertFalse(ok)
        self.assertIn("no proven approval-tier holder at the hold", why)

    def test_a_hold_with_no_recorded_time_does_not_ride(self):
        # The transcript's newest turn is Opus, but a hold whose moment is
        # not recorded cannot pin it and cannot mint an approval proof.
        self.transcript([(self.T0, "claude-opus-5-5")])
        facts, _car_ = self.facts(None)
        self.assertNotIn("hold_approval", facts["hold_row"])
        ok, why = facts["admit"]
        self.assertFalse(ok)
        self.assertIn("no proven approval-tier holder at the hold", why)
        # POSITIVE CONTROL: the same transcript with the hold's time rides.
        facts, _car_ = self.facts(self.T0 + 60)
        self.assertEqual(facts["admit"], (True, None))

    def subagent(self, epoch, name, mtime):
        """One subagent turn of the seat's session, where the harness
        writes it (<session>/subagents/), last touched at `mtime`."""
        directory = os.path.join(self.claude, "projects", "proj",
                                 self.SESSION, "subagents")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "agent-a1.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"type": "assistant", "sessionId": self.SESSION,
                 "isSidechain": True,
                 "timestamp": time.strftime(
                     "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch)),
                 "message": {"role": "assistant", "model": name}}) + "\n")
        os.utime(path, (mtime, mtime))

    def test_an_opus_seats_sonnet_subagent_at_the_hold_does_not_ride(self):
        # F1 (review FIX b7c5e42b4398): a subagent runs `helm
        # dispatch hold` in its seat's name. An Opus seat whose Sonnet
        # subagent wrote a turn three minutes before the hold has no proven
        # model, so the hold writer mints no proof and the door does not ride.
        hold = self.T0 + 960
        self.transcript([(self.T0 + s, "claude-opus-5-5")
                         for s in (0, 300, 600, 900)])
        # POSITIVE CONTROL: the seat alone rides.
        facts, _car_ = self.facts(hold)
        self.assertEqual(facts["admit"], (True, None))
        self.subagent(hold - 180, "claude-sonnet-5", hold - 180)
        facts, _car_ = self.facts(hold)
        self.assertIsNone(facts["model"])
        self.assertNotIn("hold_approval", facts["hold_row"])
        ok, why = facts["admit"]
        self.assertFalse(ok)
        self.assertIn("no proven approval-tier holder at the hold", why)

    def test_a_subagent_turn_past_the_window_leaves_the_opus_hold(self):
        # The same Sonnet turn eleven minutes before the hold, in a file
        # touched after it: outside the window, so the Opus hold rides.
        hold = self.T0 + 960
        self.transcript([(self.T0 + s, "claude-opus-5-5")
                         for s in (0, 300, 600, 900)])
        self.subagent(hold - 660, "claude-sonnet-5", hold + 60)
        facts, _car_ = self.facts(hold)
        self.assertEqual(facts["model"], "claude-opus-5-5")
        self.assertEqual(facts["admit"], (True, None))


class WindowArms(Base):
    def test_after_the_window_only_cars_still_ready_compose(self):
        self.ops.cars = [_car(ROW1, "one", TIP1), _car(ROW2, "two", TIP2)]
        self.tick()
        # car two stopped being READY; car three joined after the intent.
        self.ops.cars = [_car(ROW1, "one", TIP1), _car(ROW3, "three", TIP3)]
        self.ops.excluded = [dict(_car(ROW2, "two", TIP2),
                                  why="is READY-CONTESTED")]
        self.ops.clock += 301
        self.tick()
        self.assertEqual(self.ops.composed, [[ROW1]])
        st = self.current()
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1])
        self.assertIn(ROW2, [d["id"] for d in st["dropped"]])
        self.assertNotIn(ROW3, [d["id"] for d in st["dropped"]])
        self.assertTrue(any("two" in p and "dropped" in p.lower()
                            for p in self.ops.posts), self.ops.posts)

    def test_a_retipped_car_is_dropped(self):
        self.ops.cars = [_car(ROW1, "one", TIP1), _car(ROW2, "two", TIP2)]
        self.tick()
        self.ops.cars = [_car(ROW1, "one", TIP1), _car(ROW2, "two", TIP3)]
        self.ops.clock += 301
        self.tick()
        self.assertEqual(self.ops.composed, [[ROW1]])

    def test_zero_cars_after_the_window_returns_to_idle(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        self.ops.cars = []
        self.ops.clock += 301
        self.tick()
        self.assertNotIn("compose", self.ops.calls)
        self.assertIsNone(self.current())
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.ABANDONED])
        # IDLE again: the next tick with a car opens a fresh intent.
        self.ops.cars = [_car(ROW2, "two", TIP2)]
        self.tick()
        self.assertEqual(self.current()["state"], autoland.INTENT)

    def test_a_conflicting_car_is_dropped_and_posted_never_resolved(self):
        self.ops.conflicts = {ROW2}
        st = self.to_gating()
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1])
        self.assertTrue(any("two" in p and "conflict" in p
                            for p in self.ops.posts), self.ops.posts)

    def test_failed_pre_gate_audits_stop_before_the_gate(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.ops.audit_ok = False
        self.tick()
        self.ops.clock += 301
        self.tick()
        self.assertIn("audits", self.ops.calls)
        self.assertNotIn("launch", self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        # The audits run BETWEEN the compose and the gate.
        self.assertLess(self.ops.calls.index("compose"),
                        self.ops.calls.index("audits"))


class HappyPath(Base):
    def test_green_pushes_then_folds_then_closes_then_announces(self):  # noqa: VACUOUS_ASSERTION — lr_close is asserted in the same call list and closed_rows is asserted non-empty; no task close is the point
        autoland.seed_counter(self.root, 383, TRUNK)
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        self.assertLess(self.ops.calls.index("audits"),
                        self.ops.calls.index("launch"))
        self.green(st)
        self.ops.calls = []
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        calls = self.ops.calls
        for earlier, later in (("verify", "foldcheck"),
                               ("foldcheck", "push"),
                               ("push_guard", "push"), ("push", "ff"),
                               ("ff", "install_guard"),
                               ("install_guard", "postland"),
                               ("postland", "fold_apply"),
                               ("fold_apply", "lr_close")):
            self.assertLess(calls.index(earlier), calls.index(later),
                            (earlier, later, calls))
        # NO TASK IS CLOSED AT LAND (task/3643, slice 2): a land without a
        # re-read of the whole ask is not a done task; the join reports the
        # task as landed and its owner closes it after that read.
        self.assertNotIn("task_close", calls)
        # THE LAND'S FIRST LEDGER READ is asked for once, of the pushed head,
        # after the checkout is at it and before the fold reads the ledger
        # (task/3538).
        self.assertEqual(self.ops.postlands, [st["head"]])
        announce = self.ops.posts[-1]
        self.assertEqual(calls[-2:], ["post", "remove_room"], calls)
        # the owner reads plain words first: what the land changed, in the
        # words its tasks were filed with, then the head it pushed
        self.assertIn("LAND 384: task/3001: the fleet got lane one; "
                      "task/3002: the fleet got lane two. PUSHED %s"
                      % st["head"][:11], announce)
        self.assertIn("gate:%s whole-suite OK, Ran 25268, +19 = AST +19"
                      % GID, announce)
        self.assertIn("CL ", announce)
        self.assertIn("falsified if", announce)
        self.assertEqual(self.ops.pushes, 1)
        # the approved car closes landed; the source-clean one closed by fold
        self.assertEqual(self.ops.closed_rows, [(ROW2, True, None)])
        self.assertEqual(self.ops.closed_tasks, [])
        self.assertIsNone(self.current())
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])
        self.assertEqual(autoland.read_counter(self.root)["n"], 384)
        self.assertEqual(autoland.read_counter(self.root)["sha"], st["head"])
        # the land log keeps what the counter overwrites: each number, its
        # head, its gate and its train, for the morning report to read
        lands, why = autoland.land_log(self.root)
        self.assertIsNone(why, why)
        self.assertEqual({k: (v["sha"], v["gate"], v["ran"], v["train"])
                          for k, v in lands.items()},
                         {384: (st["head"], GID, 25268, st["name"])})

    def test_a_walk_owed_is_posted_to_the_integrator_after_the_land(self):
        """The console walk owed rides the land's post-land posts, addressed
        to the integrator and never to @all (task/3444)."""
        self.ops.walk_line = "console walk owed since abc train9, 2d"
        st = self.to_gating()
        self.green(st)
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertIn("@all LAND 384", self.ops.posts[-2])
        self.assertEqual(self.ops.posts[-1], "@integrator console walk owed "
                         "since abc train9, 2d")

    def test_a_land_log_that_will_not_write_is_posted_and_the_land_lands(
            self):
        """The land is on trunk either way: a log line that will not write
        is said once, naming the seed that records it, and the closes and
        the announcement still run."""
        st = self.to_gating()
        self.green(st)
        with mock.patch.object(autoland.eventledger, "append",
                               lambda path, row: False):
            rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        said = [p for p in self.ops.posts if "land log" in p]
        self.assertEqual(len(said), 1, self.ops.posts)
        self.assertIn("LAND 384 MISSING until `helm train auto seed 384 %s`"
                      % st["head"][:12], said[0])
        self.assertIn("LAND 384: ", self.ops.posts[-1])
        self.assertEqual(autoland.read_counter(self.root)["n"], 384)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_a_crash_after_the_push_resumes_at_the_ff_and_never_pushes_twice(
            self):
        autoland.seed_counter(self.root, 383, TRUNK)
        st = self.to_gating()
        self.green(st)
        self.ops.ff_raises = 1
        with self.assertRaises(RuntimeError):
            self.tick()
        self.assertEqual(self.ops.pushes, 1)
        self.assertEqual(self.current()["step"], "pushed")
        self.ops.calls = []
        self.tick()
        self.assertEqual(self.ops.pushes, 1)
        self.assertNotIn("push", self.ops.calls)
        self.assertEqual(self.ops.calls[0], "ff", self.ops.calls)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_a_ran_delta_below_the_ast_delta_stops_the_land(self):
        st = self.to_gating()
        self.green(st)
        self.ops.ast = 20
        self.tick()
        self.assertNotIn("push", self.ops.calls)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertTrue(any("+19" in p and "+20" in p
                            for p in self.ops.posts), self.ops.posts)

    def test_a_ran_delta_above_the_ast_delta_lands_and_names_the_surplus(
            self):
        """ONE-SIDED (task/3906): a surplus over the AST count is an
        inherited arm, not a shortfall — it lands, named in the note."""
        st = self.to_gating()
        self.green(st)
        self.ops.ast = 18
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        done, = self.archived()
        self.assertEqual(done["state"], autoland.DONE)
        notes = [h["note"] for h in done["history"] if " GREEN, " in
                 (h.get("note") or "")]
        self.assertEqual(len(notes), 1, done["history"])
        self.assertIn("AST +18: +1 collected beyond the AST count: "
                      "inherited arms or a new base", notes[0])

    def test_a_needs_restart_car_is_posted_to_the_integrator(self):
        autoland.seed_counter(self.root, 383, TRUNK)
        self.ops.changes_by_tip = {TIP2: {"helm/hooks.py": "+x\n"}}
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        self.green(st)
        self.tick()
        (rid, live, restart), = self.ops.closed_rows
        self.assertEqual((rid, live), (ROW2, False))
        self.assertIn("hooks", restart)
        self.assertTrue(any(p.startswith("@integrator") and "hooks" in p
                            and "restart" in p for p in self.ops.posts),
                        self.ops.posts)

    def test_a_close_that_did_not_answer_is_retried_next_tick(self):
        """A post-land close whose child crashed is not the car's answer: the
        whole failure is posted, the car is not done, nothing after it is
        closed, and the next tick closes it (the in-process close's raise
        at 3777bb9, task/3562)."""
        autoland.seed_counter(self.root, 383, TRUNK)
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        self.green(st)
        crash = ("`helm lr close %s --reason landed` exited 1: Traceback "
                 "(most recent call last):\n  File \"x\", line 1, in close\n"
                 "KeyError: 'forced'" % ROW2[:12])
        self.ops.close_fails = [crash]
        rc, out = self.tick()
        self.assertEqual(rc, 1, out)
        self.assertIn("KeyError: 'forced'", out)
        st = self.current()
        self.assertEqual((st["state"], st["step"]),
                         (autoland.LANDING, "numbered"))
        self.assertNotIn(ROW2, st.get("closed", []))
        self.assertEqual((self.ops.closed_rows, self.ops.closed_tasks),
                         ([], []))
        self.assertTrue(any("KeyError: 'forced'" in p and "retried" in p
                            for p in self.ops.posts), self.ops.posts)
        self.ops.calls = []
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertIn("lr_close", self.ops.calls)
        self.assertEqual(self.ops.closed_rows, [(ROW2, True, None)])
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])


# The counts of a real train's two receipts (trunk's and the train's), whose
# lane deleted one test inside a class that skips in its setUpClass on a gate
# host with no chrome: Ran moved -99, planned -100, the diff -100.
TRUNK_RAN, TRUNK_PLANNED = 27316, 27327
LANE_RAN, LANE_PLANNED = 27217, 27227
DROPPED = "a module stopped being collected or a test was dropped"


class LandFirstCar(Base):
    """task/4223: a land-first car rides the train like any other and lands
    marked landed before review; its row is never closed at the LAND, so it
    stays open as the post-land read of the landed merge."""

    def test_the_landed_cars_row_stays_open_as_a_post_land_read(self):
        st = self.to_gating([_car(ROW1, "one", TIP1, basis="approved"),
                             _car(ROW2, "two", TIP2,
                                  basis=landwindow.LAND_FIRST)])
        self.assertIn("lane two, task/3002, tip %s, row %s, landed before "
                      "review; its row stays open for reader-seat's "
                      "post-land read" % (TIP2[:12], ROW2[:12]),
                      self.ops.posts[0])
        self.green(st)
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ops.pushes, 1)
        # CONTROL: the approved car closes landed; the land-first one does not
        self.assertEqual(self.ops.closed_rows, [(ROW1, True, None)])
        self.assertIn("lane two (row %s, landed before review; its row stays "
                      "open for reader-seat's post-land read)" % ROW2[:12],
                      self.ops.posts[-1])
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])


class PlannedCount(Base):
    """THE PLANNED DELTA IS THE QUESTION THE CHECK ASKS (task/3613). Ran is
    what ran on that host: a class whose setUpClass skips records one skip
    and runs none of its tests, so a test deleted inside it moves the diff
    and not Ran. The sliced receipt's planned count is discovery's whole
    inventory, the same on every host, so the check compares it whenever
    both receipts carry it, and says which rule it used."""

    def stopped_post(self):
        self.assertNotIn("push", self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        posts = [p for p in self.ops.posts if "STOPPED" in p]
        self.assertEqual(len(posts), 1, self.ops.posts)
        return posts[0]

    def landed(self):
        self.assertEqual(self.ops.pushes, 1, self.ops.posts)
        done, = self.archived()
        self.assertEqual(done["state"], autoland.DONE)
        return done

    def green_note(self, done):
        notes = [h["note"] for h in done["history"]
                 if " GREEN, " in (h.get("note") or "")]
        self.assertEqual(len(notes), 1, done["history"])
        return notes[0]

    def test_a_test_dropped_inside_a_setupclass_skipped_class_lands(self):
        """MUST-HIT: Ran -99, planned -100, AST -100."""
        st = self.to_gating()
        self.green(st, ran=LANE_RAN, prev_ran=TRUNK_RAN, planned=LANE_PLANNED,
                   prev_planned=TRUNK_PLANNED)
        self.ops.ast = -100
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        done = self.landed()
        rec = done["receipt"]
        self.assertEqual((rec["rule"], rec["delta"], rec["ast"],
                          rec["planned"], rec["base_planned"], rec["ran"],
                          rec["base_ran"]),
                         ("planned", -100, -100, LANE_PLANNED, TRUNK_PLANNED,
                          LANE_RAN, TRUNK_RAN))
        note = self.green_note(done)
        self.assertIn(" GREEN, planned ", note)
        self.assertEqual(note, "gate:%s GREEN, planned -100, AST -100" % GID)
        self.assertIn("gate:%s whole-suite OK, Ran %d, planned -100 = AST "
                      "-100" % (GID, LANE_RAN), self.ops.posts[-1])

    def test_train520s_inherited_surplus_lands_and_is_named_in_the_note(
            self):
        """MUST-HIT (task/3906, the scope cut): train520's own numbers — the
        diff adds 205 test methods by the AST count while discovery
        collected +208, the 3 extra being a base class's new arms handed to
        descendants the diff never touched. The check is ONE-SIDED: the
        surplus lands and the land note names it."""
        st = self.to_gating()
        self.green(st, ran=25249 + 208, prev_ran=25249,
                   planned=25260 + 208, prev_planned=25260)
        self.ops.ast = 205
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        done = self.landed()
        self.assertEqual((done["receipt"]["rule"], done["receipt"]["delta"],
                          done["receipt"]["ast"]), ("planned", 208, 205))
        note = self.green_note(done)
        self.assertEqual(note, "gate:%s GREEN, planned +208, AST +205: +3 "
                         "collected beyond the AST count: inherited arms or "
                         "a new base" % GID)
        # The PUBLIC LAND line names the surplus too (helm-codex's
        # MELD-DIFF): never a false equality over +208 vs +205.
        self.assertIn("+208 vs AST +205: +3 collected beyond the AST count",
                      self.ops.posts[-1])
        self.assertNotIn("+208 = AST +205", self.ops.posts[-1])

    def test_a_deleted_test_module_lands(self):
        """MUST-HIT: a module DELETED in the diff is seen by the AST count
        too (-2 = -2), so it lands — the STOP is a module PRESENT in head
        that discovery fails to collect, never a deleted one."""
        st = self.to_gating()
        self.green(st, ran=25249 - 2, prev_ran=25249,
                   planned=25260 - 2, prev_planned=25260)
        self.ops.ast = -2
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        done = self.landed()
        self.assertEqual((done["receipt"]["delta"], done["receipt"]["ast"]),
                         (-2, -2))
        self.assertEqual(self.green_note(done),
                         "gate:%s GREEN, planned -2, AST -2" % GID)

    def test_a_present_but_uncollected_module_stops_naming_the_shortfall(
            self):
        """MUST-MISS: a module PRESENT in head that discovery fails to
        collect (an import error, a renamed base, a load_tests filter):
        the diff adds 2 by the AST count while discovery collected -2 —
        collected -2 vs AST 0 is the shortfall shape, here pushed plainly
        negative. The STOP names the shortfall exactly."""
        st = self.to_gating()
        self.green(st, ran=25249 - 2, prev_ran=25249,
                   planned=25260 - 2, prev_planned=25260)
        self.ops.ast = 0
        self.tick()
        post = self.stopped_post()
        self.assertIn("gate:%s planned -2 tests over trunk's gate:%s (25260 "
                      "planned), but the diff adds +0 test methods: %s"
                      % (GID, PREV_GID, DROPPED), post)

    def test_a_module_that_stops_being_collected_still_stops(self):
        """MUST-MISS: the diff adds 19 test methods while a 40-test module
        drops out of discovery (renamed off the pattern, say). Planned
        reads -21, AST +19."""
        st = self.to_gating()
        self.green(st, ran=25249 + 19 - 40, prev_ran=25249,
                   planned=25260 + 19 - 40, prev_planned=25260)
        self.tick()
        post = self.stopped_post()
        self.assertIn("gate:%s planned -21 tests over trunk's gate:%s (25260 "
                      "planned), but the diff adds +19 test methods: %s"
                      % (GID, PREV_GID, DROPPED), post)
        self.assertIn("the planned rule", post)

    def test_a_skipped_class_that_stops_being_collected_stops_too(self):
        """What the Ran rule never sees: a 14-test class that skips in its
        setUpClass drops out of discovery while the diff adds 19 test
        methods. Ran reads +19, the AST's own number; planned reads +5.
        Trunk plans 20 tests more than it runs (its skipped classes), so the
        train's count stays above its own Ran, as a real one does."""
        st = self.to_gating()
        self.green(st, ran=25249 + 19, prev_ran=25249,
                   planned=25269 + 19 - 14, prev_planned=25269)
        self.tick()
        post = self.stopped_post()
        self.assertIn("planned +5 tests over trunk's gate:%s" % PREV_GID,
                      post)
        self.assertIn(DROPPED, post)

    def test_a_trunk_receipt_without_a_planned_count_lands_by_the_ran_rule(
            self):
        """A receipt minted by the serial runner carries no planned count:
        the train's Ran delta is asked, as before, and the pass says so."""
        st = self.to_gating()
        self.green(st, planned=25280)
        self.tick()
        done = self.landed()
        self.assertEqual((done["receipt"]["rule"], done["receipt"]["delta"]),
                         ("ran", 19))
        note = self.green_note(done)
        self.assertTrue(note.startswith("gate:%s GREEN, Ran +19 (the Ran "
                                        "rule: " % GID), note)
        self.assertIn("), AST +19", note)
        self.assertIn("gate:%s" % PREV_GID, note)
        self.assertIn("Ran 25268, +19 = AST +19", self.ops.posts[-1])

    def test_a_trunk_receipt_without_a_planned_count_stops_by_the_ran_rule(
            self):
        """The control of the must-hit arm, in the one-sided shortfall
        shape (task/3906): the Ran delta falls SHORT of the diff (the diff
        deletes 98 while Ran moves -99), so the land stops exactly as it
        did — a two-sided mismatch by more deletion now lands, so the
        stop shape is collected short."""
        st = self.to_gating()
        self.green(st, ran=TRUNK_RAN - 99, prev_ran=TRUNK_RAN,
                   planned=LANE_PLANNED)
        self.ops.ast = -98
        self.tick()
        post = self.stopped_post()
        self.assertIn("gate:%s ran -99 over trunk's gate:%s (%d), but the "
                      "diff adds -98 test methods: %s"
                      % (GID, PREV_GID, TRUNK_RAN, DROPPED), post)
        self.assertIn("the Ran rule", post)

    def test_a_train_receipt_without_a_planned_count_stops_by_the_ran_rule(
            self):
        st = self.to_gating()
        self.green(st, ran=TRUNK_RAN - 99, prev_ran=TRUNK_RAN,
                   prev_planned=TRUNK_PLANNED)
        self.ops.ast = -98
        self.tick()
        post = self.stopped_post()
        self.assertIn("gate:%s ran -99" % GID, post)
        self.assertIn("the Ran rule", post)
        self.assertIn("gate:%s" % GID, post.split("the Ran rule", 1)[1])

    def test_a_bool_planned_count_is_unknown_and_never_a_pass(self):
        """DISCRIMINATING: a naive isinstance read takes the bool True for
        the int 1, reading planned +1 = the diff's +1 — a naive LAND; the
        real read refuses a bool (type is not int), the Ran rule applies,
        and its -99 falls SHORT of +1."""
        st = self.to_gating()
        self.green(st, ran=TRUNK_RAN - 99, prev_ran=TRUNK_RAN,
                   planned=True, prev_planned=TRUNK_PLANNED)
        self.ops.ast = 1
        self.tick()
        post = self.stopped_post()
        self.assertIn("planned count True is UNKNOWN", post)
        self.assertIn("gate:%s ran -99" % GID, post)
        self.assertIn("the Ran rule", post)
        self.assertIn("the diff adds +1 test methods", post)

    def test_a_negative_planned_count_is_unknown_and_never_a_pass(self):
        """DISCRIMINATING (helm-codex's MELD-DIFF): the naive reader coerces
        -5 to its absolute and reads planned +2 = the diff's +2 — a naive
        LAND; the real read stops on the Ran rule's -99 short of +2."""
        st = self.to_gating()
        self.green(st, ran=TRUNK_RAN - 99, prev_ran=TRUNK_RAN,
                   planned=-TRUNK_PLANNED - 2, prev_planned=TRUNK_PLANNED)
        self.ops.ast = 2
        self.tick()
        post = self.stopped_post()
        self.assertIn("planned count -%d is UNKNOWN" % (TRUNK_PLANNED + 2),
                      post)
        self.assertIn("gate:%s ran -99" % GID, post)
        self.assertIn("the Ran rule", post)
        self.assertIn("the diff adds +2 test methods", post)

    def test_a_string_planned_count_is_unknown_and_never_a_pass(self):
        """DISCRIMINATING: a string count int()s cleanly, so the naive read
        is planned +2 = the diff's +2 — a naive LAND; the real read stops
        on the Ran rule's -99 short of +2."""
        st = self.to_gating()
        self.green(st, ran=TRUNK_RAN - 99, prev_ran=TRUNK_RAN,
                   planned=str(TRUNK_PLANNED + 2), prev_planned=TRUNK_PLANNED)
        self.ops.ast = 2
        self.tick()
        post = self.stopped_post()
        self.assertIn("planned count '%d' is UNKNOWN" % (TRUNK_PLANNED + 2),
                      post)
        self.assertIn("gate:%s ran -99" % GID, post)
        self.assertIn("the Ran rule", post)
        self.assertIn("the diff adds +2 test methods", post)

    def test_a_planned_count_below_its_own_ran_is_unknown(self):
        """UNKNOWN-REASON-ONLY (no naive-pass claim: a count below the
        receipt's own Ran gives no coherent naive reading)."""
        st = self.to_gating()
        self.green(st, ran=TRUNK_RAN - 99, prev_ran=TRUNK_RAN,
                   planned=TRUNK_RAN - 100, prev_planned=TRUNK_PLANNED)
        self.ops.ast = -98
        self.tick()
        post = self.stopped_post()
        self.assertIn("below its own Ran %d" % (TRUNK_RAN - 99), post)
        self.assertIn("the Ran rule", post)

    def test_a_receipt_with_no_ran_stops_the_delta_unknown(self):
        """A green receipt whose Ran does not read gives the Ran rule no
        delta at all: the cross-check cannot be asked, so the land stops
        UNKNOWN rather than comparing None (helm-codex's read of 18d78b)."""
        st = self.to_gating()
        self.green(st)
        self.ops.receipt_rows[1] = dict(self.ops.receipt_rows[1])
        del self.ops.receipt_rows[1]["ran"]
        self.tick()
        post = self.stopped_post()
        self.assertIn("an UNKNOWN count", post)
        self.assertIn("the collected delta is UNKNOWN", post)

    def bool_ran(self, idx):
        """A serial receipt carrying a bool Ran (the v4/v5 schema takes
        one) stops the delta UNKNOWN. The fixture is DISCRIMINATING: an
        isinstance read takes True for 1, so the naive delta -25248
        equals the diff's -25248 — a naive LAND (helm-codex's MELD-DIFF
        numbers)."""
        st = self.to_gating()
        self.green(st)  # serial receipts: the Ran rule decides
        self.ops.receipt_rows[idx] = dict(self.ops.receipt_rows[idx])
        self.ops.receipt_rows[idx]["ran"] = True
        self.ops.ast = -25248
        self.tick()
        post = self.stopped_post()
        self.assertIn("the collected delta is UNKNOWN", post)

    def test_a_bool_ran_on_the_train_receipt_stops_the_delta_unknown(self):
        """isinstance would read the train receipt's True as 1; type is
        not int, so the delta is UNKNOWN and the land stops."""
        self.bool_ran(1)

    def test_a_bool_ran_on_trunks_receipt_stops_the_delta_unknown(self):
        """The converse: a bool Ran on TRUNK's receipt stops the same
        way."""
        self.bool_ran(0)

    def test_a_newer_serial_trunk_receipt_does_not_hide_the_sliced_one(self):
        """Trunk's exact tree can hold both kinds. A serial run of it minted
        later, on a host whose Ran counts the class the gate host skips,
        does not decide the rule: the sliced trunk receipt is the base."""
        st = self.to_gating()
        self.green(st, ran=LANE_RAN, prev_ran=TRUNK_RAN, planned=LANE_PLANNED,
                   prev_planned=TRUNK_PLANNED)
        self.ops.receipt_rows.insert(1, {
            "id": "99" * 8, "status": "OK", "suite": True,
            "ran": TRUNK_RAN + 1, "head": TRUNK, "tree": "b" * 40,
            "ts": "2026-01-01T00:30:00Z"})
        self.ops.ast = -100
        self.tick()
        done = self.landed()
        self.assertEqual((done["receipt"]["base"], done["receipt"]["rule"]),
                         (PREV_GID, "planned"))


class PlannedCountReader(unittest.TestCase):
    """`autoland.planned_count`: the one reader of a receipt's planned count,
    total, and UNKNOWN for anything but a positive int no less than its own
    Ran on the kind whose content id binds it."""

    def row(self, planned):
        return _sliced({"id": GID, "status": "OK", "suite": True, "ran": 100,
                        "skipped": 3}, planned)

    def test_the_fixture_is_the_real_sliced_shape(self):
        auth = self.row(110)["slice_authority"]
        self.assertIn("planned", auth)
        self.assertEqual(set(auth), gate._SLICE_KEYS)
        self.assertIn("ran", auth["outcome"])
        self.assertEqual(set(auth["outcome"]), gate._SLICE_OUTCOME)

    def test_the_sliced_kind_reads_its_planned_count(self):
        self.assertEqual(autoland.planned_count(self.row(110)), (110, None))
        self.assertEqual(autoland.planned_count(self.row(100)), (100, None))

    def test_a_receipt_of_another_kind_carries_none(self):
        n, why = autoland.planned_count({"id": GID, "v": 4, "ran": 100})
        self.assertIsNone(n)
        self.assertIn("gate:%s carries no planned count" % GID, why)
        # a count stored on a kind whose content id does not bind it is not
        # read: only the sliced kind's id binds `slice_authority`
        n, why = autoland.planned_count(dict(self.row(110), v=4))
        self.assertIsNone(n)
        self.assertIn("gate:%s carries no planned count" % GID, why)

    def test_a_malformed_planned_count_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the loop is a fixed twelve-row list whose length is asserted first, and every row asserts its UNKNOWN reason positively
        rows = [self.row(v) for v in (True, False, -5, 0, "110", 110.0, None,
                                      [110], 99)]
        missing = self.row(110)
        del missing["slice_authority"]["planned"]
        rows += [missing, dict(self.row(110), slice_authority="planned=110"),
                 dict(self.row(110), ran="100")]
        self.assertEqual(len(rows), 12)
        for row in rows:
            with self.subTest(row=row["slice_authority"], ran=row["ran"]):
                n, why = autoland.planned_count(row)
                self.assertIsNone(n)
                self.assertIn("UNKNOWN", why)
                self.assertIn("gate:%s" % GID, why)


# THE DOOR'S ROUTING HEADER COMES FIRST AND FAB'S REASON AFTER IT, exactly as
# `gatewindow.launch` prints them (routed_text, then the dispatch's refusal).
_ROUTED = ("helm gate window: ROUTED to host-a — expected to finish first "
           "(~4.1 min)\n"
           "  host-a       median 4.1 min over 12 sliced gate(s), idle\n")
_CAUSE = "helm cannot run /x/compose/train7 as slices"
_HELD = ("helm gate window: `fab gate submit` answered UNKNOWN for gate-%s, "
         "so this dispatch is UNKNOWN and the window is HELD.\n"
         "  %s: helm refused to plan this tree: NOT a missing declaration, "
         "a POLICY refusal: /x/compose/train7 is a COMPOSE room\n"
         "  Ask the authority before launching anything else: `helm gate "
         "window show --recover` asks host-a whether it holds that job."
         % ("ab" * 32, _CAUSE))
_NOT_RUN = ("helm gate window: NOT DISPATCHED — fab launched nothing for "
            "gate-%s on host-a, so no suite runs and nothing holds the "
            "window.\n  fab said: Fab launched nothing on host-a (exit 75, "
            "retry the submit): RUNTIME RETIRED on host-a\n"
            "  retry: helm gate window launch --repo /x/compose/train7"
            % ("ab" * 32))


class DoorText(Base):
    """task/3463 item 14: auto-land's first own train stopped on `the gate
    door refused (exit 4): helm gate window: ROUTED to host-a`, and nothing
    else. The door had said why on the lines after that header, and the stop
    kept only the first line. Every record of a door that did not launch
    carries the door's whole text."""

    def refused(self, rc, text):
        self.ops.launch_rc, self.ops.launch_text = rc, text
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        self.ops.clock += 301
        _rc, out = self.tick()
        return self.current(), out

    def test_a_refused_door_stops_the_train_with_its_whole_text(self):
        st, out = self.refused(4, _ROUTED + _HELD)
        self.assertEqual(st["state"], autoland.STOPPED, st)
        why = st["stopped"]["why"]
        # CONTROL on the same record: the header the old stop kept is kept.
        self.assertIn("ROUTED to host-a", why)
        self.assertIn(_CAUSE, why)
        self.assertIn("window show --recover", why)
        self.assertIn(_CAUSE, st["history"][-1]["note"])
        self.assertIn(_CAUSE, out)
        stops = [p for p in self.ops.posts if "STOPPED" in p]
        self.assertEqual(len(stops), 1, self.ops.posts)
        self.assertIn(_CAUSE, stops[0])
        # AND EVERY TICK AFTER IT, which re-prints the stop from the record.
        _rc, again = self.tick()
        self.assertIn(_CAUSE, again)

    def test_resume_after_a_refused_door_relaunches_and_restarts_no_wait(self):  # noqa: VACUOUS_ASSERTION — the one relaunch and the launched gate job are positive
        """A refused door launched nothing, so --resume has no gate to wait
        on: the next tick launches, and the resume never says a wait
        restarted."""
        st, _out = self.refused(4, _ROUTED + _HELD)
        self.assertEqual(st["state"], autoland.STOPPED, st)
        self.assertFalse(st["launched"])
        said, why = autoland.resume(self.root, "integrator",
                                    now=self.ops.clock)
        self.assertIsNone(why, why)
        self.assertNotIn("wait restart", said)
        self.ops.launch_rc = 0
        self.ops.calls = []
        self.tick()
        self.assertEqual(self.ops.calls.count("launch"), 1)
        st = self.current()
        self.assertEqual((st["state"], st["gate"]["job_id"]),
                         (autoland.GATING, "gate-x0"))

    def test_a_long_answer_keeps_its_head_and_its_cause_and_says_what_it_cut(self):
        # A 200-line answer (a traceback, a long push) would otherwise land in
        # the room whole: the stop keeps the header, the tail where the cause
        # is, and a count of what it cut.
        middle = "".join("  frame %d\n" % i for i in range(200))
        st, out = self.refused(4, _ROUTED + middle + _CAUSE + "\n")
        why = st["stopped"]["why"]
        self.assertIn("ROUTED to host-a", why)
        self.assertIn(_CAUSE, why)
        self.assertIn("... 180 lines cut ...", why)
        self.assertNotIn("frame 100\n", why + "\n")
        self.assertLessEqual(len(why.splitlines()), 3 + 20 + 1 + 1)
        stops = [p for p in self.ops.posts if "STOPPED" in p]
        self.assertEqual(len(stops), 1, self.ops.posts)
        self.assertIn(_CAUSE, stops[0])

    def test_a_door_that_launched_nothing_waits_with_fabs_reason(self):  # noqa: VACUOUS_ASSERTION — the one wait post is asserted to exist and to carry both the header and Fab's reason, positively
        st, _out = self.refused(3, _ROUTED + _NOT_RUN)
        self.assertEqual(st["state"], autoland.GATING, st)
        waits = [p for p in self.ops.posts if "does not launch yet" in p]
        self.assertEqual(len(waits), 1, self.ops.posts)
        self.assertIn("ROUTED to host-a", waits[0])
        self.assertIn("RUNTIME RETIRED on host-a", waits[0])

    # THE SAME LOSS AT EVERY STOP THAT QUOTES A TOOL'S MULTI-LINE ANSWER:
    # git prints `To <url>` before the rejection, foldcheck its passing rungs
    # before the failing one, the rail its checks before its refusal, and a
    # crashed blame `Traceback` before the exception.

    def test_a_refused_push_stops_with_gits_whole_answer(self):
        self.ops.push_refusal_text = (
            "To private://trunk\n ! [rejected]        x -> main (fetch "
            "first)\nerror: failed to push some refs to 'private://trunk'")
        self.ops.push_refusals = 1
        st = self.to_gating()
        self.green(st)
        self.tick()
        why = self.current()["stopped"]["why"]
        self.assertIn("To private://trunk", why)
        self.assertIn("(fetch first)", why)

    def test_a_refused_rail_reinstall_stops_with_the_rails_whole_answer(self):
        self.ops.guard_answer = (1, "rail: checking the hooks\nREFUSED: "
                                    "core.hooksPath points elsewhere")
        st = self.to_gating()
        self.green(st)
        self.tick()
        why = self.current()["stopped"]["why"]
        self.assertIn("rail: checking the hooks", why)
        self.assertIn("core.hooksPath points elsewhere", why)

    def test_a_failed_fold_apply_stops_with_the_failing_rung(self):
        self.ops.fold_rc = 1
        self.ops.fold_text = ("ok    tip-exists     x\n"
                              "REFUSE origin-has-it  x is not on origin/main")
        st = self.to_gating()
        self.green(st)
        self.tick()
        why = self.current()["stopped"]["why"]
        self.assertIn("ok    tip-exists", why)
        self.assertIn("REFUSE origin-has-it", why)

    def test_a_blame_that_printed_no_json_is_refused_with_its_whole_text(self):
        from helm import trainblame

        def crashed(room, gate=None, apply=False, as_json=False, out=None):
            out.write("Traceback (most recent call last):\n  File \"x\"\n"
                      "ValueError: the b-room is unreadable\n")
            return 1

        with mock.patch.object(trainblame, "blame", crashed):
            rc, result = autoland.Ops().blame(self.root, GID)
        self.assertEqual(rc, 1)
        self.assertIn("Traceback", result["refused"])
        self.assertIn("ValueError: the b-room is unreadable",
                      result["refused"])


class RedPath(Base):
    def red(self, st):
        self.ops.receipt_rows = [{"id": RED_GID, "status": "FAILED",
                                  "suite": True, "ran": 25000,
                                  "head": st["head"], "tree": "e" * 40,
                                  "ts": "2026-01-01T01:00:00Z"}]

    def test_red_runs_blame_apply_once_and_tracks_the_b_room(self):
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2)])
        self.red(st)
        broom = autoland.Ops.room_path(self.ops, self.root, "train7b")
        self.ops.blame_answer = (0, {"verdict": {
            "kind": "EJECT", "car": {"n": 2, "id": ROW2, "lane": "two",
                                     "tip": TIP2}}})
        self.ops.heads[broom] = "9" * 40
        self.tick()
        self.assertEqual(self.ops.calls.count("blame"), 1)
        st = self.current()
        self.assertEqual(st["state"], autoland.GATING)
        self.assertEqual(st["room"], broom)
        self.assertIn(broom, st["rooms"])
        self.assertEqual(st["head"], "9" * 40)
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1])
        self.assertNotIn("push", self.ops.calls)
        # the next tick waits on the b-room's gate; blame is not run again
        self.tick()
        self.assertEqual(self.ops.calls.count("blame"), 1)

    def only_in_the_b_room(self, *tips):
        """git's answer for a b-room whose compose REFUSED the other cars'
        merges: only `tips` are in its head."""
        self.ops.ancestry = lambda root, older, newer: (
            vcs.ANCESTOR if older in tips or older == TRUNK
            else vcs.NOT_ANCESTOR)

    def test_a_car_whose_merge_the_b_room_refused_is_not_carried(self):
        """Blame ejects car two, and the b-room's compose REFUSES car three's
        merge (a conflict, a hook, a timeout): compose_room still gates the
        room with car one and exits 1. Car three is not in the head, so the
        train must not carry it to the last word, the close or the LAND
        announcement (task/3265 door read); its land request rides the next
        train."""
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2),
                             _car(ROW3, "three", TIP3)])
        self.red(st)
        broom = autoland.Ops.room_path(self.ops, self.root, "train7b")
        self.ops.blame_answer = (1, {"verdict": {
            "kind": "EJECT", "car": {"n": 2, "id": ROW2, "lane": "two",
                                     "tip": TIP2}}})
        self.ops.heads[broom] = "9" * 40
        self.only_in_the_b_room(TIP1)
        self.tick()
        st = self.current()
        self.assertEqual(st["state"], autoland.GATING)
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1])

    def test_a_b_room_where_no_car_merged_lands_nothing(self):
        """Every kept car's merge was refused, so the b-room stands at trunk:
        there is nothing to land and no LAND number to take."""
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2)])
        self.red(st)
        broom = autoland.Ops.room_path(self.ops, self.root, "train7b")
        self.ops.blame_answer = (1, {"verdict": {
            "kind": "EJECT", "car": {"n": 2, "id": ROW2, "lane": "two",
                                     "tip": TIP2}}})
        self.ops.heads[broom] = TRUNK
        self.only_in_the_b_room()
        self.tick()
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.ABANDONED])
        self.assertNotIn("push", self.ops.calls)
        self.assertTrue(any("merged" in p for p in self.ops.posts),
                        self.ops.posts)

    def test_trunk_red_stops_and_posts_and_pushes_nothing(self):
        st = self.to_gating()
        self.red(st)
        self.ops.blame_answer = (0, {"verdict": {"kind": "TRUNK-RED",
                                                 "car": None}})
        self.tick()
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertNotIn("push", self.ops.calls)
        stops = [p for p in self.ops.posts if "STOPPED" in p]
        self.assertEqual(len(stops), 1, self.ops.posts)
        self.assertIn(RED_GID, stops[0])
        self.assertTrue(stops[0].startswith("@integrator"))
        # a second tick on a STOPPED train posts nothing more
        self.tick()
        self.assertEqual(len([p for p in self.ops.posts if "STOPPED" in p]),
                         1)

    def test_a_tick_that_dies_inside_blame_never_runs_it_again(self):
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2)])
        self.red(st)
        broom = autoland.Ops.room_path(self.ops, self.root, "train7b")
        self.ops.heads[broom] = "9" * 40
        ancestors = {TIP1}
        self.ops.ancestry = lambda root, older, newer: (
            vcs.ANCESTOR if older in ancestors or older == TRUNK
            else vcs.NOT_ANCESTOR)

        def blame_then_die(room, gid):
            self.ops.calls.append("blame")
            raise RuntimeError("injected crash inside blame")

        self.ops.blame = blame_then_die
        with self.assertRaises(RuntimeError):
            self.tick()
        self.ops.calls = []
        self.tick()
        self.assertNotIn("blame", self.ops.calls)
        self.assertIn("launch", self.ops.calls)
        st = self.current()
        self.assertEqual(st["state"], autoland.GATING)
        self.assertEqual(st["room"], broom)
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1])

    def test_an_interrupted_blame_with_no_b_room_stops(self):
        st = self.to_gating()
        self.red(st)

        def blame_then_die(room, gid):
            raise RuntimeError("injected crash inside blame")

        self.ops.blame = blame_then_die
        with self.assertRaises(RuntimeError):
            self.tick()
        self.tick()
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        self.assertTrue(any("interrupted" in p for p in self.ops.posts),
                        self.ops.posts)

    def test_a_red_that_passes_alone_on_its_host_is_a_flake(self):
        st = self.to_gating()
        self.red(st)
        self.ops.recheck_answer = ("GREEN", "passes alone on host-a")
        self.ops.calls = []
        self.tick()
        self.assertNotIn("blame", self.ops.calls)
        self.assertIn("record_flake", self.ops.calls)
        self.assertLess(self.ops.calls.index("record_flake"),
                        self.ops.calls.index("launch"))
        self.assertEqual(self.current()["state"], autoland.GATING)
        # the re-gate is red again and flakes again: a second flake stops
        self.ops.receipt_rows.append(dict(self.ops.receipt_rows[0],
                                          id="77" * 8,
                                          ts="2026-01-01T02:00:00Z"))
        self.tick()
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        self.assertEqual(self.ops.calls.count("launch"), 1)

    def second_flake_stop(self):
        """A red, a flake and its one re-gate, red again and passing alone
        again: STOPPED at GATING on the second flake."""
        st = self.to_gating()
        self.red(st)
        self.ops.recheck_answer = ("GREEN", "passes alone on host-a")
        self.tick()
        st = self.current()
        self.assertEqual((st["state"], st["gate"]["job_id"]),
                         (autoland.GATING, "gate-x1"))
        self.ops.receipt_rows.append(dict(self.ops.receipt_rows[0],
                                          id="77" * 8,
                                          ts="2026-01-01T02:00:00Z"))
        self.tick()
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertEqual(st["stopped"]["state"], autoland.GATING)
        self.assertIn("a second flake", st["stopped"]["why"])
        return st

    def test_the_first_flake_re_gate_waits_on_its_own_fresh_gate(self):
        """The one automatic re-gate is unchanged: the flake is recorded, a
        gate of a new attempt (a new job) launches in the same tick, and the
        next tick waits on that gate."""
        st = self.to_gating()
        self.red(st)
        self.ops.recheck_answer = ("GREEN", "passes alone on host-a")
        self.ops.calls = []
        self.tick()
        self.assertEqual(self.ops.calls.count("launch"), 1)
        st = self.current()
        self.assertEqual((st["state"], st["flakes"], st["gate"]["job_id"]),
                         (autoland.GATING, 1, "gate-x1"))
        _rc, out = self.tick()
        self.assertIn("waiting on the gate", out)
        self.assertEqual(self.ops.calls.count("launch"), 1)

    def test_resume_after_a_second_flake_launches_a_fresh_gate(self):  # noqa: VACUOUS_ASSERTION — the launch count and the fresh job id are positive
        """--resume after a second-flake STOP put the train back
        at GATING still holding its finished gate, and every tick printed
        'waiting' on a job that had exited red. The second flake is recorded
        (so the relaunch is a new attempt, a new job), and the resumed tick
        launches it on the same head."""
        st = self.second_flake_stop()
        head = st["head"]
        # the second flake is recorded too: the next attempt is a new job
        self.assertEqual(len(self.ops.flakes), 2)
        self.assertEqual(self.ops.flakes[1]["gate"], "77" * 8)
        said, why = autoland.resume(self.root, "integrator",
                                    now=self.ops.clock)
        self.assertIsNone(why, why)
        self.assertIn("fresh gate", said)
        self.ops.calls = []
        _rc, out = self.tick()
        self.assertNotIn("waiting on the gate", out)
        self.assertEqual(self.ops.calls.count("launch"), 1)
        st = self.current()
        self.assertEqual((st["state"], st["head"], st["gate"]["job_id"]),
                         (autoland.GATING, head, "gate-x2"))
        self.assertEqual(st["launch_ts"], self.ops.clock)
        # the flake bound stands: the fresh gate red and passing alone once
        # more stops again, and launches nothing on its own
        self.ops.receipt_rows.append(dict(self.ops.receipt_rows[0],
                                          id="88" * 8,
                                          ts="2026-01-01T03:00:00Z"))
        self.tick()
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertIn("a second flake", st["stopped"]["why"])
        self.assertEqual(self.ops.calls.count("launch"), 1)

    def test_a_finished_gate_already_read_is_not_waited_on(self):  # noqa: VACUOUS_ASSERTION — the STOPPED state naming the spent job and its red is positive
        """The defensive arm: the launched gate job is the one whose red
        this train already read (here the second flake could not be
        recorded, so the relaunch is the same tree and attempt, and Fab
        answers with that same finished job). The tick stops naming it; it
        never reports 'waiting'."""
        st = self.to_gating()
        self.red(st)
        self.ops.recheck_answer = ("GREEN", "passes alone on host-a")
        self.tick()
        self.ops.record_flake = lambda root, record: (
            None, "the flake store is locked")
        self.ops.receipt_rows.append(dict(self.ops.receipt_rows[0],
                                          id="77" * 8,
                                          ts="2026-01-01T02:00:00Z"))
        self.tick()
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        autoland.resume(self.root, "integrator", now=self.ops.clock)
        self.ops.calls = []
        _rc, out = self.tick()
        self.assertEqual(self.ops.calls.count("launch"), 1)
        self.assertEqual(self.current()["gate"]["job_id"], "gate-x1")
        _rc, out = self.tick()
        self.assertNotIn("waiting on the gate", out)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertIn("gate-x1", st["stopped"]["why"])
        self.assertIn("77" * 8, st["stopped"]["why"])

    def test_a_resumed_fresh_gate_that_reads_green_lands(self):  # noqa: VACUOUS_ASSERTION — the verify call, one push and the DONE archive are positive
        """The resumed gate finishes OK: the tick reads it and lands."""
        st = self.second_flake_stop()
        autoland.resume(self.root, "integrator", now=self.ops.clock)
        self.tick()
        self.green(self.current())
        self.ops.calls = []
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("waiting on the gate", out)
        self.assertIn("verify", self.ops.calls)
        self.assertEqual(self.ops.pushes, 1)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_resume_after_a_second_flake_in_a_b_room_launches_a_fresh_gate(self):  # noqa: VACUOUS_ASSERTION — the launch count and the b-room head are positive
        """The gate blame's compose launched on a b-room records its job
        too, so a second flake there is spent like any other, and --resume
        gates the b-room afresh rather than waiting on its finished job."""
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2)])
        self.red(st)
        self.ops.recheck_answer = ("GREEN", "passes alone on host-a")
        self.tick()
        broom = autoland.Ops.room_path(self.ops, self.root, "train7b")
        self.ops.heads[broom] = "9" * 40
        self.ops.b_gate = {"host": "host-b", "job_id": "gate-b"}
        self.ops.recheck_answer = ("RED", "fails alone")
        self.ops.blame_answer = (0, {"verdict": {
            "kind": "EJECT", "car": {"n": 2, "id": ROW2, "lane": "two",
                                     "tip": TIP2}}})
        self.ops.receipt_rows.append(dict(self.ops.receipt_rows[0],
                                          id="77" * 8,
                                          ts="2026-01-01T02:00:00Z"))
        self.tick()
        st = self.current()
        self.assertEqual((st["state"], st["room"], st["gate"]["job_id"]),
                         (autoland.GATING, broom, "gate-b"))
        self.ops.recheck_answer = ("GREEN", "passes alone on host-b")
        self.ops.receipt_rows.append(dict(self.ops.receipt_rows[0],
                                          id="88" * 8, head="9" * 40,
                                          ts="2026-01-01T03:00:00Z"))
        self.tick()
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertIn("a second flake", st["stopped"]["why"])
        said, why = autoland.resume(self.root, "integrator",
                                    now=self.ops.clock)
        self.assertIsNone(why, why)
        self.assertIn("fresh gate", said)
        self.ops.calls = []
        _rc, out = self.tick()
        self.assertNotIn("waiting on the gate", out)
        self.assertEqual(self.ops.calls.count("launch"), 1)
        st = self.current()
        self.assertEqual((st["state"], st["room"], st["head"]),
                         (autoland.GATING, broom, "9" * 40))
        self.assertNotEqual(st["gate"]["job_id"], "gate-b")

    def test_resume_after_an_unreadable_red_reads_that_red_again(self):  # noqa: VACUOUS_ASSERTION — the recheck call and the blame of that red are positive
        """A red that could not be read for blame was never acted on, so
        --resume reads it again rather than waiting on a finished gate."""
        st = self.to_gating()
        self.red(st)
        facts = self.ops.red_facts
        self.ops.red_facts = lambda room, gid: (None, None, "ledger locked")
        self.tick()
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertNotIn(RED_GID, st["red"])
        self.ops.red_facts = facts
        self.ops.blame_answer = (0, {"verdict": {"kind": "TRUNK-RED",
                                                 "car": None}})
        autoland.resume(self.root, "integrator", now=self.ops.clock)
        self.ops.calls = []
        _rc, out = self.tick()
        self.assertNotIn("waiting on the gate", out)
        self.assertIn("recheck", self.ops.calls)
        self.assertEqual(self.ops.blamed[-1][1], RED_GID)

    def no_receipt_timeout_stop(self):
        """GATING with no receipt names the head, past GATE_WAIT_S: STOPPED
        at GATING with the launch_ts exactly where it timed out."""
        st = self.to_gating()
        self.red(st)
        self.ops.recheck_answer = ("GREEN", "passes alone on host-a")
        self.ops.calls = []
        self.ops.flakes = []
        self.tick()  # red -> a flake re-gate launches gate-x1
        st = self.current()
        self.assertEqual(st["gate"]["job_id"], "gate-x1")
        self.ops.receipt_rows = []  # nothing ever names the head
        self.ops.clock += 7300  # past GATE_WAIT_S
        _rc, out = self.tick()
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertEqual(st["stopped"]["state"], autoland.GATING)
        self.assertIn("no receipt names the head", st["stopped"]["why"])
        return st

    def test_resume_after_a_no_receipt_timeout_waits_a_fresh_gate_wait_s(self):  # noqa: VACUOUS_ASSERTION — the wait post and the same job id are positive
        """task/4123: --resume after a no-receipt TIMEOUT stop at GATING kept
        launch_ts exactly where it timed out, so the next tick timed out again
        at once. The wait starts again on the SAME gate job: the resumed tick
        prints 'waiting', not a stop, and relaunches nothing. The same job is
        kept (Fab answers it if still running); a receipt arriving after the
        resume is read and lands."""
        st = self.no_receipt_timeout_stop()
        self.assertEqual(st["gate"]["job_id"], "gate-x1")
        old_launch_ts = st["launch_ts"]
        said, why = autoland.resume(self.root, "integrator",
                                    now=self.ops.clock)
        self.assertIsNone(why, why)
        self.assertIn("wait restarted", said, said)
        # launch_ts moved forward to now, not the old timed-out value
        self.assertGreater(self.ops.clock, old_launch_ts)
        self.assertEqual(self.current()["launch_ts"], self.ops.clock)
        self.ops.calls = []
        # ONE tick on the fresh wait: waiting, not a second timeout stop, and
        # nothing relaunched (the same job stays)
        _rc, out = self.tick()
        self.assertIn("waiting on the gate", out)
        self.assertNotIn("with a fresh gate", out)
        self.assertEqual(self.ops.calls.count("launch"), 0)
        st = self.current()
        self.assertEqual(st["state"], autoland.GATING)
        self.assertEqual(st["gate"]["job_id"], "gate-x1")
        self.assertEqual(st["launch_ts"], self.ops.clock)
        # A receipt that arrives after the resume is read and lands:
        self.green(st)
        _rc, out = self.tick()
        self.assertNotIn("waiting on the gate", out)
        self.assertIn("verify", self.ops.calls)
        self.assertEqual(self.ops.pushes, 1)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_resume_after_an_unreadable_red_names_the_unspent_job_not_a_timeout(self):  # noqa: VACUOUS_ASSERTION — the history note naming the job and the re-read recheck are positive
        """The wait restart rides every launched, unspent GATING stop, not
        only the timeout: a stop whose receipt is still there reads it again
        (the clock is never asked), and the record says what is true of it
        (the job is unspent), never that no receipt named the head."""
        st = self.to_gating()
        self.red(st)
        facts = self.ops.red_facts
        self.ops.red_facts = lambda room, gid: (None, None, "ledger locked")
        self.tick()
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        self.ops.red_facts = facts
        said, why = autoland.resume(self.root, "integrator",
                                    now=self.ops.clock)
        self.assertIsNone(why, why)
        note = self.current()["history"][-1]["note"]
        self.assertIn("gate job gate-x0 is not spent", note)
        self.assertNotIn("no receipt named the head", note)
        self.ops.blame_answer = (0, {"verdict": {"kind": "TRUNK-RED",
                                                 "car": None}})
        self.ops.calls = []
        _rc, out = self.tick()
        self.assertNotIn("waiting on the gate", out)
        self.assertIn("recheck", self.ops.calls)

    def test_resume_on_a_b_room_gate_with_no_recorded_job_says_no_id_recorded(self):  # noqa: VACUOUS_ASSERTION — the history note naming the unrecorded job is positive
        """Blame's b-room gate whose door row named no job: a no-receipt
        timeout stop there, resumed, restarts the wait and the record says
        the job's id was not recorded, never 'gate job None'."""
        st = self.to_gating()
        self.red(st)
        broom = autoland.Ops.room_path(self.ops, self.root, "train7b")
        self.ops.blame_answer = (0, {"verdict": {
            "kind": "EJECT", "car": {"n": 2, "id": ROW2, "lane": "two",
                                     "tip": TIP2}}})
        self.ops.heads[broom] = "9" * 40
        self.tick()
        st = self.current()
        self.assertEqual((st["state"], st["room"], st["gate"]["job_id"]),
                         (autoland.GATING, broom, None))
        self.ops.clock += 7300  # past GATE_WAIT_S, no receipt for the b-room
        self.tick()
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertIn("no receipt names the head", st["stopped"]["why"])
        said, why = autoland.resume(self.root, "integrator",
                                    now=self.ops.clock)
        self.assertIsNone(why, why)
        self.assertIn("wait restarted", said, said)
        note = self.current()["history"][-1]["note"]
        self.assertIn("its gate job (no id recorded) is not spent", note)
        self.assertNotIn("None", note)
        self.assertEqual(self.current()["launch_ts"], self.ops.clock)


class AuditRedPath(Base):
    """task/3674: RED PRE-GATE AUDITS TAKE A RED GATE'S EJECT ROAD. When the
    audits fail on the composed room, `helm train blame --apply` reads their
    log in place of a receipt; when blame by diff names exactly one car, it
    is told, recorded and ejected, and the b-room its compose gates is
    tracked (`_Tick.ejected`), once the failing tests fail again alone on
    the audits' host. Anything else STOPS the train as before. The
    fake blame answers as the real one does; tests/test_trainblame.py
    (ARedPreGateAuditIsBlamedByDiffAlone) drives the real decision."""

    LOG = "/nowhere/train7-audits.log"

    @staticmethod
    def two():
        return [_car(ROW1, "one", TIP1), _car(ROW2, "two", TIP2)]

    def red_audits(self, cars, answer, log=LOG):
        """IDLE -> INTENT -> (window) -> composed, audits RED, blamed."""
        self.ops.cars = cars
        self.ops.audit_ok = False
        self.ops.audit_log = log
        self.ops.blame_answer = answer
        self.tick()
        self.ops.clock += 301
        return self.tick()

    @staticmethod
    def ejects(rid, lane, tip, n):
        return (0, {"verdict": {
            "kind": "EJECT", "by": "diff",
            "car": {"n": n, "id": rid, "lane": lane, "tip": tip},
            "why": "blame by diff: lane %s's diff touches "
                   "tests/test_x.py, and no other car's does" % lane}})

    def room7(self):
        return autoland.Ops.room_path(self.ops, self.root, "train7")

    def broom(self):
        """train7b, the b-room blame composes, standing at a head."""
        broom = autoland.Ops.room_path(self.ops, self.root, "train7b")
        self.ops.heads[broom] = "9" * 40
        return broom

    def assert_stopped_as_today(self, refused):
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertEqual((st["stopped"]["state"], st["stopped"]["step"]),
                         (autoland.COMPOSING, "composed"))
        self.assertIn("the pre-gate audits failed on the composed room",
                      st["stopped"]["why"])
        self.assertIn(refused, st["stopped"]["why"])
        self.assertEqual(self.ops.blamed, [(self.room7(), None, self.LOG)])
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1, ROW2])
        self.assertEqual(st.get("ejected") or [], [])
        self.assertNotIn("launch", self.ops.calls)
        stops = [p for p in self.ops.posts if "STOPPED" in p]
        self.assertEqual(len(stops), 1, self.ops.posts)
        self.assertIn(refused, stops[0])

    def test_the_one_car_blame_names_is_ejected_and_the_train_gates(self):  # noqa: VACUOUS_ASSERTION — no launch, no STOP post and no second audit run are the contract; the b-room state, the ejection and the one blame call are positive
        broom = self.broom()
        self.red_audits(self.two(), self.ejects(ROW2, "two", TIP2, 2))
        self.assertEqual(self.ops.blamed, [(self.room7(), None, self.LOG)])
        st = self.current()
        self.assertEqual(st["state"], autoland.GATING, st.get("stopped"))
        self.assertEqual((st["room"], st["head"], st["name"]),
                         (broom, "9" * 40, "train7b"))
        self.assertIn(broom, st["rooms"])
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1])
        self.assertEqual([(e["id"], e["gate"]) for e in st["ejected"]],
                         [(ROW2, autoland.AUDITS)])
        # blame's own compose launched the b-room's gate
        self.assertTrue(st["launched"])
        self.assertNotIn("launch", self.ops.calls)
        self.assertEqual([p for p in self.ops.posts if "STOPPED" in p], [])
        # the next tick waits on the b-room's gate: the audits are not run
        # again and blame is never asked twice about one red
        self.ops.calls = []
        self.tick()
        self.assertNotIn("audits", self.ops.calls)
        self.assertEqual(len(self.ops.blamed), 1)
        self.assertEqual(self.current()["state"], autoland.GATING)

    def test_a_failure_that_passes_alone_ejects_no_car(self):  # noqa: VACUOUS_ASSERTION — no blame, ejection or launch is the contract; the STOPPED state, its reason and the resumed RED control that ejects are positive
        """The failing tests are re-run alone, once,
        before blame, as a red gate's are. A census module is chosen because
        a car's diff touches it, so a flake there would almost always be
        pinned on that car: only a RED re-run takes the EJECT road."""
        self.ops.recheck_answer = ("GREEN", "pass alone on host-q")
        self.red_audits(self.two(), self.ejects(ROW2, "two", TIP2, 2))
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertIn("the pre-gate audits failed on the composed room",
                      st["stopped"]["why"])
        self.assertIn("GREEN (pass alone on host-q)", st["stopped"]["why"])
        self.assertIn("audit_recheck", self.ops.calls)
        self.assertEqual(self.ops.blamed, [])
        self.assertEqual(st.get("ejected") or [], [])
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1, ROW2])
        self.assertNotIn("launch", self.ops.calls)
        # CONTROL: resumed, the same red run that fails alone again is
        # blamed and its one named car ejected.
        broom = self.broom()
        self.ops.recheck_answer = ("RED", "fails alone on host-q")
        _said, why = autoland.resume(self.root, "integrator")
        self.assertIsNone(why, why)
        self.tick()
        st = self.current()
        self.assertEqual((st["state"], st["room"]), (autoland.GATING, broom))
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1])
        self.assertEqual(self.ops.blamed, [(self.room7(), None, self.LOG)])

    def test_a_re_run_that_waits_or_cannot_be_read_ejects_no_car(self):  # noqa: VACUOUS_ASSERTION — no blame or ejection is the contract; each STOPPED reason and the resumed RED control that ejects are positive
        """A busy host or a re-run that cannot be read is no proof the
        failure repeats: the train stops as a red audit stopped before, and
        a person's --resume runs the audits and the re-run again."""
        self.ops.recheck_answer = (
            "UNKNOWN", "the audits' log names no host they ran on")
        self.red_audits(self.two(), self.ejects(ROW2, "two", TIP2, 2))
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertIn("UNKNOWN (the audits' log names no host they ran on)",
                      st["stopped"]["why"])
        self.ops.recheck_answer = ("WAIT", "host-q is running a gate")
        _said, why = autoland.resume(self.root, "integrator")
        self.assertIsNone(why, why)
        self.tick()
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertIn("WAIT (host-q is running a gate)", st["stopped"]["why"])
        self.assertEqual(self.ops.blamed, [])
        self.assertEqual(st.get("ejected") or [], [])
        self.assertEqual(self.ops.calls.count("audit_recheck"), 2)
        # CONTROL: a RED re-run on the next resume ejects the named car.
        broom = self.broom()
        self.ops.recheck_answer = ("RED", "fails alone on host-q")
        _said, why = autoland.resume(self.root, "integrator")
        self.assertIsNone(why, why)
        self.tick()
        self.assertEqual(self.current()["room"], broom)
        self.assertEqual(len(self.ops.blamed), 1)

    def test_two_named_cars_stop_as_today(self):  # noqa: VACUOUS_ASSERTION — nothing ejected or launched is the contract; the STOPPED state, its reason, the blame call and the one stop post are positive
        refused = ("blame by diff names 2 car(s) for the pre-gate audits' "
                   "failures, and a red pre-gate audit is never bisected")
        self.red_audits(self.two(), (1, {"refused": refused}))
        self.assert_stopped_as_today(refused)

    def test_no_named_car_stops_as_today(self):  # noqa: VACUOUS_ASSERTION — nothing ejected or launched is the contract; the STOPPED state, its reason, the blame call and the resumed second blame are positive
        refused = ("blame by diff names 0 car(s) for the pre-gate audits' "
                   "failures, and a red pre-gate audit is never bisected")
        self.red_audits(self.two(), (1, {"refused": refused}))
        self.assert_stopped_as_today(refused)
        # a person's --resume runs the audits again, and blame with them
        _said, why = autoland.resume(self.root, "integrator")
        self.assertIsNone(why, why)
        self.ops.calls = []
        self.tick()
        self.assertEqual(self.ops.calls.count("audits"), 1)
        self.assertEqual(len(self.ops.blamed), 2)

    def test_an_audit_run_that_wrote_no_log_stops_and_blames_nobody(self):  # noqa: VACUOUS_ASSERTION — no blame and no launch are the contract; the resumed control on the same train blames and gates the b-room
        self.red_audits(self.two(), self.ejects(ROW2, "two", TIP2, 2),
                        log=None)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertIn("the pre-gate audits failed on the composed room",
                      st["stopped"]["why"])
        self.assertEqual(self.ops.blamed, [])
        self.assertNotIn("launch", self.ops.calls)
        # CONTROL: resumed, the same red run with its log written is blamed
        # and its one named car ejected.
        broom = self.broom()
        self.ops.audit_log = self.LOG
        _said, why = autoland.resume(self.root, "integrator")
        self.assertIsNone(why, why)
        self.tick()
        st = self.current()
        self.assertEqual((st["state"], st["room"]), (autoland.GATING, broom))
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1])

    def test_a_blame_refusing_an_unreadable_log_stops(self):  # noqa: VACUOUS_ASSERTION — nothing ejected or launched is the contract; the STOPPED state, its reason, the blame call and the one stop post are positive
        refused = ("the pre-gate audits' log %s holds no readable failure "
                   "list (the run printed no readable summary)" % self.LOG)
        self.red_audits(self.two(), (1, {"refused": refused}))
        self.assert_stopped_as_today(refused)

    def test_a_one_car_train_whose_car_is_named_ejects_it_and_ends(self):  # noqa: VACUOUS_ASSERTION — no launch, push or STOP post is the contract; the ABANDONED archive, its ejection and the one ended post are positive
        self.red_audits([_car(ROW1, "one", TIP1)],
                        self.ejects(ROW1, "one", TIP1, 1))
        self.assertIsNone(self.current())
        done = self.archived()
        self.assertEqual([d["state"] for d in done], [autoland.ABANDONED])
        self.assertEqual([(e["id"], e["gate"]) for e in done[0]["ejected"]],
                         [(ROW1, autoland.AUDITS)])
        self.assertNotIn("launch", self.ops.calls)
        self.assertNotIn("push", self.ops.calls)
        self.assertEqual([p for p in self.ops.posts if "STOPPED" in p], [])
        ended = [p for p in self.ops.posts if "only car" in p]
        self.assertEqual(len(ended), 1, self.ops.posts)
        self.assertIn("pre-gate audit", ended[0])
        self.assertNotIn("gate:audits", ended[0])

    def test_a_tick_that_dies_inside_the_audit_blame_never_runs_it_again(
            self):
        broom = self.broom()
        self.ops.ancestry = lambda root, older, newer: (
            vcs.ANCESTOR if older in (TIP1, TRUNK) else vcs.NOT_ANCESTOR)

        def blame_then_die(room, gid, audits=None):
            self.ops.blamed.append((room, gid, audits))
            raise RuntimeError("injected crash inside blame")

        self.ops.blame = blame_then_die
        with self.assertRaises(RuntimeError):
            self.red_audits(self.two(), None)
        self.ops.calls = []
        self.tick()
        self.assertEqual(len(self.ops.blamed), 1)
        self.assertNotIn("audits", self.ops.calls)
        self.assertIn("launch", self.ops.calls)
        st = self.current()
        self.assertEqual((st["state"], st["room"]), (autoland.GATING, broom))
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1])

    def test_ops_re_runs_the_audit_failures_on_the_audits_own_host(self):
        """Ops.audit_recheck reads the red audits from their log and hands
        them to the gate's own re-run (Ops.recheck), pinned to the host the
        audits ran on; an audits red that names no host runs nothing."""
        from helm import trainblame
        red = {"host": "host-q", "modules": ["tests.test_x"], "mode": None,
               "tests": [{"id": "tests.test_x.T.test_y"}]}
        seen = []

        def rerun(_ops, st, got):
            seen.append(((st.get("gate") or {}).get("host"), got))
            return "RED", "fails alone on host-q"

        with mock.patch.object(trainblame, "read_room",
                               return_value=({"head": "h"}, None)), \
                mock.patch.object(trainblame, "audit_red",
                                  return_value=(red, None)), \
                mock.patch.object(autoland.Ops, "recheck", rerun):
            got = autoland.Ops().audit_recheck(
                {"room": self.root, "gate": None}, self.LOG)
        self.assertEqual(got, ("RED", "fails alone on host-q"))
        self.assertEqual(seen, [("host-q", red)])
        # CONTROL: the same red with no host is UNKNOWN and re-runs nothing.
        with mock.patch.object(trainblame, "read_room",
                               return_value=({"head": "h"}, None)), \
                mock.patch.object(trainblame, "audit_red",
                                  return_value=(dict(red, host=None), None)), \
                mock.patch.object(autoland.Ops, "recheck", rerun):
            status, why = autoland.Ops().audit_recheck(
                {"room": self.root, "gate": None}, self.LOG)
        self.assertEqual(status, "UNKNOWN")
        self.assertIn("names no host", why)
        self.assertEqual(len(seen), 1)

    def test_ops_hands_blame_the_audit_log_in_place_of_a_gate(self):
        from helm import trainblame
        seen = []

        def answered(room, gate=None, apply=False, as_json=False, out=None,
                     audits=None):
            seen.append((room, gate, apply, as_json, audits))
            out.write(json.dumps({"exit": 0, "verdict": {"kind": "EJECT"}}))
            return 0

        with mock.patch.object(trainblame, "blame", answered):
            rc, result = autoland.Ops().blame(self.root, None,
                                              audits=self.LOG)
            rc2, _result2 = autoland.Ops().blame(self.root, GID)
        self.assertEqual((rc, result["verdict"]["kind"]), (0, "EJECT"))
        self.assertEqual(rc2, 0)
        self.assertEqual(seen, [(self.root, None, True, True, self.LOG),
                                (self.root, "gate:" + GID, True, True,
                                 None)])


class Guards(Base):
    def test_a_missing_receipt_refuses_the_land(self):
        self.to_gating()
        self.ops.receipt_rows = []
        _rc, out = self.tick()
        self.assertIn("waiting on the gate", out)
        self.assertNotIn("push", self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.GATING)

    def test_an_unverified_receipt_refuses_the_land(self):
        st = self.to_gating()
        self.green(st)
        self.ops.verify_ok = (False, "receipt is a sliced receipt")
        self.tick()
        self.assertNotIn("push", self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        self.assertTrue(any("sliced" in p for p in self.ops.posts))

    def test_a_refused_push_guard_pushes_nothing(self):
        st = self.to_gating()
        self.green(st)
        self.ops.guard = (False, "remote origin reads PUBLIC")
        self.tick()
        self.assertEqual(self.ops.pushes, 0)
        self.assertEqual(self.current()["state"], autoland.STOPPED)

    def test_a_refused_fast_forward_stops_before_the_fold(self):
        st = self.to_gating()
        self.green(st)
        self.ops.ff_answer = (False, "the shared checkout is dirty")
        self.tick()
        self.assertEqual(self.ops.pushes, 1)
        self.assertNotIn("fold_apply", self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        self.assertTrue(any("dirty" in p for p in self.ops.posts))


class PushSafety(Base):
    """THE CHECKS BIND THE PUSH, NOT THE STEP. A push retried from a recorded
    step (a refused push, then `--resume`) asks the push guard and foldcheck
    again, and a train ended or paused while its land tick runs is not
    pushed. Each arm carries its control on the same observable."""

    def stopped_at_pushing(self):
        st = self.to_gating()
        self.green(st)
        self.ops.push_refusals = 1
        self.tick()
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED, st)
        self.assertEqual(st["stopped"]["step"], "pushing", st)
        self.assertEqual(self.ops.pushes, 0)
        said, why = autoland.resume(self.root, "integrator", now=self.ops.clock)
        self.assertIsNone(why, why)
        self.assertEqual(self.current()["step"], "pushing")
        self.ops.calls = []
        return st

    def test_a_resumed_push_asks_the_push_guard_again(self):
        self.stopped_at_pushing()
        self.ops.guard = (False, "remote origin reads PUBLIC")
        self.tick()
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertNotIn("push", self.ops.calls)
        self.assertIn("push_guard", self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        self.assertTrue(any("PUBLIC" in p for p in self.ops.posts),
                        self.ops.posts)
        # the control: the guard passes, and the same resumed step pushes once
        autoland.resume(self.root, "integrator", now=self.ops.clock)
        self.ops.guard = (True, None)
        self.tick()
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_a_resumed_push_asks_foldcheck_again(self):
        self.stopped_at_pushing()
        self.ops.tree_rung = foldcheck.REFUSE
        self.tick()
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertIn("foldcheck", self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        # the control: the rung passes, and the same resumed step pushes
        autoland.resume(self.root, "integrator", now=self.ops.clock)
        self.ops.tree_rung = foldcheck.PASS
        self.tick()
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)

    def test_a_train_abandoned_during_its_land_tick_is_not_pushed(self):
        st = self.to_gating()
        self.green(st)
        self.ops.on_push_guard = lambda: autoland.abandon(
            self.root, "integrator", "a car is bad", ops=self.ops,
            now=self.ops.clock)
        self.tick()
        # the land step ran up to its push: the guard was asked, then the
        # abandon was found before the push
        self.assertIn("push_guard", self.ops.calls)
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertNotIn("push", self.ops.calls)
        # the abandon stands: nothing was written back over it
        self.assertIsNone(self.current())
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.ABANDONED])

    def test_a_train_paused_during_its_land_tick_is_not_pushed(self):
        st = self.to_gating()
        self.green(st)
        self.ops.on_push_guard = lambda: autoland.pause(
            self.root, "integrator", "hold every land")
        self.tick()
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertEqual(self.current()["step"], "pushing")
        # the control: resumed, the next tick checks again and pushes once
        autoland.resume(self.root, "integrator", now=self.ops.clock)
        self.tick()
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_a_remote_changed_after_the_target_read_cannot_redirect_it(self):  # noqa: VACUOUS_ASSERTION — the resumed control pushes the same train once
        """The immutable target is vetted before readiness and read again
        as the last read before the push (task/3265 races R1). A remote
        mutation during the readiness read cannot change where the push
        goes: that last read sees it and stops the train, nothing pushed."""
        st = self.to_gating()
        self.green(st)
        real = self.ops.plan

        def plan_while_a_public_pushurl_is_added(root):
            self.ops.guard = (False, "remote origin points at a PUBLIC "
                                     "mirror")
            return real(root)

        self.ops.plan = plan_while_a_public_pushurl_is_added
        self.ops.calls = []
        self.tick()
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertEqual(self.ops.calls.count("push_guard"), 2, self.ops.calls)
        # the last guard read comes after the readiness read
        last = max(i for i, c in enumerate(self.ops.calls)
                   if c == "push_guard")
        self.assertLess(self.ops.calls.index("plan"), last, self.ops.calls)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED, st)
        self.assertIn("PUBLIC", st["stopped"]["why"])
        # the control: the mirror gone, the resumed step pushes once
        self.ops.plan = real
        self.ops.guard = (True, None)
        _said, why = autoland.resume(self.root, "integrator",
                                     now=self.ops.clock)
        self.assertIsNone(why, why)
        self.tick()
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])


class LastWordRaces(Base):
    """THE FOUR RACES OF THE LAST WORD (helm-codex's gap check at
    94142115286e, task/3265). The bar: an auto-land never pushes anything
    but the exact gated, approved tree to the exact validated destination,
    and never pushes after a veto. Each arm interleaves its writer at the one
    instant the race needs, and carries its control on the same observable.
    """

    def at_landing(self):
        st = self.to_gating()
        self.green(st)
        self.ops.calls = []
        return st

    def test_a_destination_moved_after_foldcheck_is_never_pushed(self):  # noqa: VACUOUS_ASSERTION — the resumed control pushes the same train once
        """F1: the destination foldcheck and the guard vetted is the one the
        push may use; one that reads otherwise in the last word stops the
        train, and nothing is pushed anywhere."""
        self.at_landing()
        # the first guard read (beside foldcheck) resolves trunk; every read
        # after it resolves another destination
        self.ops.on_push_guard = lambda: setattr(self.ops, "target",
                                                 "private://elsewhere")
        self.tick()
        self.assertIn("foldcheck", self.ops.calls)
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertNotIn("push", self.ops.calls)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED, st)
        self.assertIn("private://elsewhere", st["stopped"]["why"])
        # the control: the destination back where foldcheck asked it, the
        # resumed step asks it all again and pushes once
        self.ops.target = "private://trunk"
        _said, why = autoland.resume(self.root, "integrator",
                                     now=self.ops.clock)
        self.assertIsNone(why, why)
        self.tick()
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_a_trunk_rewound_after_the_compose_is_never_re_landed(self):  # noqa: VACUOUS_ASSERTION — the control pushes the same train once
        """B1 (the binding door read of b8fa6ef6128): trunk rewound from the
        trunk the train was composed and gated on to an ancestor of its head
        (an integrator backing out a land) is one the head fast-forwards, so
        a plain push re-landed what the rewind removed. Rewound before the
        land step reads trunk, the train stops there; rewound after every
        read and before the update, the push's lease is refused by the
        remote. Both stop naming the moved trunk with nothing pushed. The
        control, trunk unchanged, pushes the same resumed train once."""
        rewound = "9" * 40
        self.at_landing()
        self.ops.remote = rewound
        self.tick()
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertNotIn("push", self.ops.calls)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED, st)
        moved = "trunk reads %s, not %s" % (rewound[:12], TRUNK[:12])
        self.assertIn(moved, st["stopped"]["why"])
        # beside the push: every read sees the gated trunk, and the rewind
        # lands between the last of them and the update
        self.ops.remote = TRUNK
        real = self.ops.push

        def rewound_beside_the_push(root, head, target, keep=(), lease=None):
            self.ops.remote = rewound
            return real(root, head, target, keep, lease=lease)

        self.ops.push = rewound_beside_the_push
        _said, why = autoland.resume(self.root, "integrator",
                                     now=self.ops.clock)
        self.assertIsNone(why, why)
        self.ops.calls = []
        self.tick()
        self.assertIn("push", self.ops.calls)
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertEqual(self.ops.remote, rewound)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED, st)
        self.assertIn(moved, st["stopped"]["why"])
        self.assertIn("stale info", st["stopped"]["why"])
        # the control: trunk reads the gated trunk again, and the same
        # resumed train pushes once
        self.ops.push = real
        self.ops.remote = TRUNK
        _said, why = autoland.resume(self.root, "integrator",
                                     now=self.ops.clock)
        self.assertIsNone(why, why)
        self.tick()
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def eject(self, tip, lane, rid):
        return landwindow.record_ejection(self.root, {
            "tip": tip, "lr": rid, "lane": lane, "train": "train7",
            "gate": RED_GID, "verdict": "EJECT", "tests": [], "by": "blame"})

    def test_an_ejection_racing_the_last_read_waits_until_after_the_push(self):  # noqa: VACUOUS_ASSERTION — the same store then holds the ejection once the push is done
        """F2: an ejection written after the last plan read would land a car
        the ledger no longer lets ride. The ejection writer takes the lock
        the last read and the push hold, so it waits until the push is
        done; it is not lost, and the push never stood beside it."""
        self.at_landing()
        self.ops.real_ejections = True
        real = self.ops.plan
        said, written = [], threading.Event()

        def ejection():
            said.append(self.eject(TIP1, "one", ROW1))
            written.set()

        racer = threading.Thread(target=ejection, daemon=True)

        def the_last_read_then_an_ejection(root):
            got = real(root)
            racer.start()
            # without the shared lock the ejection is written here, between
            # the last read and the push
            written.wait(2.0)
            return got

        self.ops.plan = the_last_read_then_an_ejection
        self.tick()
        racer.join(60)
        self.assertFalse(racer.is_alive())
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual(self.ops.ejected_at_push, set())
        # the ejection waited for the push and was recorded after it
        self.assertEqual([why for _row, why in said], [None])
        standing, why = landwindow.read_ejections(self.root)
        self.assertIsNone(why, why)
        self.assertEqual(set(standing), {TIP1})

    def test_an_ejection_before_the_last_read_keeps_the_train_unpushed(self):  # noqa: VACUOUS_ASSERTION — the readmitted control pushes the same train once
        """F2's other order: an ejection written before the last read is in
        the plan it reads, so the car is not READY and nothing is pushed."""
        self.at_landing()
        self.ops.real_ejections = True
        _row, why = self.eject(TIP1, "one", ROW1)
        self.assertIsNone(why, why)
        self.tick()
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED, st)
        self.assertIn("ejected from train7", st["stopped"]["why"])
        # the control: readmitted, the resumed step lands the same train
        _row, why = landwindow.readmit(self.root, TIP1, "blame was wrong",
                                       "integrator")
        self.assertIsNone(why, why)
        _said, why = autoland.resume(self.root, "integrator",
                                     now=self.ops.clock)
        self.assertIsNone(why, why)
        self.tick()
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual(self.ops.ejected_at_push, set())

    def test_a_canary_veto_written_after_the_last_foldcheck_stops_the_push(self):  # noqa: VACUOUS_ASSERTION — the cleared control pushes the same train once
        """F3: the canary's DISABLE marker, written after the last foldcheck
        read the land's sliced receipt as authorizing, is read again inside
        the locked section beside the push, and it aborts the push."""
        self.at_landing()
        self.ops.sliced = True
        real = self.ops.plan

        def the_canary_diverges_during_the_last_word(root):
            gatecanary.write_marker(
                {"reason": "serial and sliced disagree on one test",
                 "divergences": [{"test": "tests.test_x.T.test_y"}]},
                {"tree": "e" * 40, "head": TRUNK, "id": "serial-1"},
                {"id": "sliced-1"})
            return real(root)

        self.ops.plan = the_canary_diverges_during_the_last_word
        self.tick()
        self.assertIn("foldcheck", self.ops.calls)
        self.assertIsNotNone(gate.sliced_land_disabled())
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertNotIn("push", self.ops.calls)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED, st)
        self.assertIn("canary", st["stopped"]["why"])
        # the control: a person cleared the marker, and the resumed step
        # pushes the same train once
        self.ops.plan = real
        os.remove(gate.sliced_land_marker_path())
        _said, why = autoland.resume(self.root, "integrator",
                                     now=self.ops.clock)
        self.assertIsNone(why, why)
        self.tick()
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_a_destination_moved_after_the_last_veto_read_is_never_pushed(self):  # noqa: VACUOUS_ASSERTION — the resumed control pushes the same train once
        """R1 (helm-codex's read of 62af397dc1a): the destination is read
        again inside the locks as the LAST read before the push, after the
        receipt's land authority, so one that moved while that authority was
        asked stops the train with nothing pushed."""
        self.at_landing()
        real, asked = self.ops.verify, []

        def the_trunk_moves_while_the_veto_is_asked(root, head, gid):
            asked.append(gid)
            if len(asked) == 2:         # land_veto's: the green step was 1
                self.ops.target = "private://elsewhere"
            return real(root, head, gid)

        self.ops.verify = the_trunk_moves_while_the_veto_is_asked
        self.tick()
        self.assertEqual(asked, [GID, GID], self.ops.calls)
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED, st)
        self.assertIn("private://elsewhere", st["stopped"]["why"])
        # the control: the destination back, the resumed step pushes once
        self.ops.verify = real
        self.ops.target = "private://trunk"
        _said, why = autoland.resume(self.root, "integrator",
                                     now=self.ops.clock)
        self.assertIsNone(why, why)
        self.tick()
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)

    def racing_the_push(self, *writes):
        """Start `writes` in order on another thread at the instant the push
        begins, and record, for each, whether it had finished when the push
        went: the first is given 30 s, each after it 2 s. -> (thread,
        {index: finished at the push}, [Event per write])"""
        done = [threading.Event() for _w in writes]
        seen, failed = {}, []
        real = self.ops.push

        def writer():
            for write, event in zip(writes, done):
                try:
                    write()
                except Exception as exc:        # noqa: BLE001 — reported
                    failed.append(exc)
                event.set()

        racer = threading.Thread(target=writer, daemon=True)

        def push(root, head, target, keep=(), lease=None):
            racer.start()
            for i, event in enumerate(done):
                seen[i] = event.wait(30.0 if i == 0 else 2.0)
            return real(root, head, target, keep, lease=lease)

        self.ops.push = push
        self.addCleanup(lambda: self.assertEqual(failed, []))
        return racer, seen, done

    def test_a_canary_veto_written_beside_the_push_waits_until_after_it(self):  # noqa: VACUOUS_ASSERTION — the marker positively stands once the push is done
        """R2: a DISABLE marker written after land_veto read the receipt as
        authorizing and before the push is ordered: its writer takes the
        readiness lock the last word holds through the push, so it lands
        after the push, never beside it."""
        self.at_landing()
        racer, seen, done = self.racing_the_push(
            lambda: gatecanary.write_marker(
                {"reason": "serial and sliced disagree on one test",
                 "divergences": [{"test": "tests.test_x.T.test_y"}]},
                {"tree": "e" * 40, "head": TRUNK, "id": "serial-1"},
                {"id": "sliced-1"}))
        self.tick()
        racer.join(60)
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual(seen, {0: False})
        self.assertTrue(done[0].is_set())
        self.assertIsNotNone(gate.sliced_land_disabled())

    def test_an_approval_policy_written_beside_the_push_waits_until_after_it(self):  # noqa: VACUOUS_ASSERTION — the policy positively stands once the push is done
        """R2: the approval-tier policy decides which holders a door car's
        admission accepts. A policy write between the last word's reads and
        the push is ordered after the push; a prior that carries no policy is
        the control, written at the same instant and never waiting."""
        self.at_landing()
        where = os.path.join(self.tmp, "store")

        def prior(pid, kind=None):
            e = {"id": pid, "statement": "who may approve a land",
                 "confidence": 1.0, "source": "human: the owner"}
            if kind:
                e.update(policy_kind=kind, policy_reason="the land reads it",
                         policy_members=["family:codex"])
            return store.write_prior(e, root_dir=where)

        racer, seen, done = self.racing_the_push(
            lambda: prior("a-plain-prior"),
            lambda: prior("the-approval-tier", "approval-tier"))
        self.tick()
        racer.join(60)
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual(seen, {0: True, 1: False})
        self.assertTrue(done[1].is_set())
        self.assertEqual(sorted(os.listdir(where)), [
            "prior-a-plain-prior.md", "prior-the-approval-tier.md"])

    #: The runtime testimony a door car's admission reads off a roster row.
    TESTIMONY = {"runtime": {"family": "kimi", "agent_harness": "claude",
                             "backend": "proxy"},
                 "runtime_verified": True}

    def roster_of(self, rows):
        """A roster of this test's own, written whole before the tick, while
        nothing holds the land order."""
        from helm import chat, pk, seats
        env = mock.patch.dict(os.environ, {
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat")})
        env.start()
        self.addCleanup(env.stop)
        chat._ensure_dir()
        pk.write_json(seats.roster_path(), rows)
        return seats

    # THE INTEGRATOR'S RULING (task/3265 r6, a scope cut after helm-codex's
    # round-5 read): `helm chat seat gc` is not a land-order writer. It
    # deletes only dead seats' rows, which is housekeeping, not a veto; the
    # land acts on the snapshot its last read took.
    def test_a_gc_of_a_testified_dead_seat_beside_the_push_does_not_wait(self):
        """The ruling above. A gc victim is a DEAD seat by construction: no
        transcript for any remembered session, no live process, no fresh
        presence. So `helm chat seat gc --apply` keeps main's code whole and
        takes no land order, even when its victim's row carries runtime
        testimony: run beside the push, it finishes while the push waits.
        The control, a disown at the same instant on a live row that carries
        testimony (the gc keeps it, because its session has a transcript),
        runs in the land order and waits until after the push."""
        self.at_landing()
        dead, live = "s-dead-0001", "s-live-0002"
        seats = self.roster_of({
            "junk-testified": dict(self.TESTIMONY, session=dead,
                                   sessions=[dead]),
            "seat-a": dict(self.TESTIMONY, session=live,
                              sessions=["s-live-0001", live])})
        kept, proc = (os.path.join(self.tmp, d)
                      for d in ("transcripts", "proc"))
        os.makedirs(os.path.join(kept, "proj"))
        os.makedirs(proc)
        open(os.path.join(kept, "proj", live + ".jsonl"), "w").close()
        pruned, said = [], []
        racer, seen, done = self.racing_the_push(
            lambda: pruned.append(seats.gc_roster(
                apply=True, roots=[kept], proc_dir=proc)[1]),
            lambda: said.append(seats.disown_session(
                "seat-a", "s-live-0001")[0]))
        self.tick()
        racer.join(60)
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual(seen, {0: True, 1: False})
        self.assertTrue(done[1].is_set())
        self.assertEqual(pruned, [["junk-testified"]])
        self.assertEqual(said, [True])
        rows = seats.roster()
        self.assertEqual(sorted(rows), ["seat-a"])
        self.assertEqual(rows["seat-a"]["sessions"], [live])

    def test_a_disown_moving_testimony_beside_the_push_waits_until_after_it(self):  # noqa: VACUOUS_ASSERTION — the testified session positively moves once the push is done
        """task/3265 r4 (helm-codex R2 of round 3): `helm chat seat
        disown` moves a session id, and the runtime testimony filed under it,
        out of a row, which changes whom the door tier admits. When a row it
        changes carries testimony it runs in the land order, so it lands
        after the push. The control, a disown at the same instant on a row
        with no testimony, takes no land order and does not wait."""
        self.at_landing()
        sid = "s-testified-0001"
        entry = {"runtime": dict(self.TESTIMONY["runtime"]),
                 "verified": True, "source": "lifecycle"}
        seats = self.roster_of({
            "plain-seat": {"session": "s-plain-0002",
                           "sessions": ["s-plain-0001", "s-plain-0002"]},
            "testified-seat": dict(self.TESTIMONY, session=sid,
                                   sessions=["s-testified-0000", sid],
                                   runtime_sessions={sid: entry}),
            "owner-seat": {"session": "s-owner-0001",
                           "sessions": ["s-owner-0001"]}})
        said = []

        def disown(seat, dead, to=None):
            said.append(seats.disown_session(seat, dead, to=to)[0])

        racer, seen, done = self.racing_the_push(
            lambda: disown("plain-seat", "s-plain-0001"),
            lambda: disown("testified-seat", sid, to="owner-seat"))
        self.tick()
        racer.join(60)
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual(seen, {0: True, 1: False})
        self.assertTrue(done[1].is_set())
        self.assertEqual(said, [True, True])
        rows = seats.roster()
        self.assertEqual(rows["plain-seat"]["sessions"], ["s-plain-0002"])
        self.assertNotIn("runtime_sessions", rows["testified-seat"])
        self.assertEqual(rows["owner-seat"]["runtime_sessions"], {sid: entry})

    def test_a_join_evicting_testimony_beside_the_push_waits_until_after_it(self):  # noqa: VACUOUS_ASSERTION — the testified session positively leaves its row once the push is done
        """B4 (the binding door read of b8fa6ef6128): an ordinary join
        (`write_roster`, no runtime of its own) that binds a session another
        row remembers evicts it from that row and prunes the testimony filed
        under it, which the door tier reads. When a row it changes carries
        testimony the join runs in the land order, so it lands after the
        push. The control, the same seat joining a session no row carrying
        testimony names, takes no land order and does not wait."""
        self.at_landing()
        shared, current = "s-shared-0001", "s-testified-0002"
        entry = {"runtime": dict(self.TESTIMONY["runtime"]),
                 "verified": True, "source": "lifecycle"}
        seats = self.roster_of({
            "testified-seat": dict(self.TESTIMONY, session=current,
                                   sessions=[shared, current],
                                   runtime_sessions={shared: entry,
                                                     current: entry})})
        env = mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "joiner-seat"})
        env.start()
        self.addCleanup(env.stop)

        def join(sid):
            seats.write_roster("joiner-seat", session=sid,
                               presence_beat=False)

        racer, seen, done = self.racing_the_push(
            lambda: join("s-joiner-0001"), lambda: join(shared))
        self.tick()
        racer.join(60)
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual(seen, {0: True, 1: False})
        self.assertTrue(done[1].is_set())
        rows = seats.roster()
        self.assertEqual(rows["joiner-seat"]["session"], shared)
        self.assertEqual(rows["testified-seat"]["sessions"], [current])
        self.assertEqual(rows["testified-seat"]["runtime_sessions"],
                         {current: entry})


#: A git remote helper for `linger::` URLs that, like git's credential-cache
#: daemon on a push that succeeds, leaves a child behind that never calls
#: setsid and closes its standard streams. It records the sleeper's pid and
#: its own, waits for the arm's release, then connects git to the bare
#: remote so the push lands.
_LINGER = """#!/bin/sh
sleep 120 </dev/null >/dev/null 2>&1 &
echo $! > '%(dir)s/sleeper.pid'
echo $$ > '%(dir)s/helper.pid'
: > '%(dir)s/helper.started'
while [ ! -e '%(dir)s/helper.release' ]; do sleep 0.05; done
read -r caps
printf 'connect\\n\\n'
read -r what
printf '\\n'
exec git receive-pack '%(remote)s'
"""


class PushChildLocks(Base):
    """B3 (the binding door read of b8fa6ef6128): the push's three locks live
    in its lock holder and nowhere else past the tick; git and everything
    git starts hold none. A child a push leaves behind (git's
    credential-cache daemon lives for its idle timeout, 900 s by default)
    held the dispatch ledger's lock with them, so every dispatch, verdict and
    tick waited on it. The locks are still let go by closing, never by
    unlocking (R3), and a killed tick still releases none while git runs
    (`OrphanedPush`). Driven through the real `Ops.push` to a real bare
    remote, by a transport helper that leaves a sleeper behind."""

    def locks(self):
        return (("tick", autoland._lock_path(self.root, "tick")),
                ("store", autoland._lock_path(self.root, "state")),
                ("dispatch ledger", dispatches.ledger_path() + ".lock"))

    def held(self):
        """{lock: whether a descriptor other than a new one holds it}."""
        got = {}
        for name, path in self.locks():
            fd = os.open(path, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got[name] = False
            except BlockingIOError:
                got[name] = True
            finally:
                os.close(fd)
        return got

    def opened_by(self, pid):
        """The locks process `pid` has a descriptor open on."""
        want = {}
        for name, path in self.locks():
            got = os.stat(path)
            want[(got.st_dev, got.st_ino)] = name
        names = set()
        where = "/proc/%d/fd" % pid
        for fd in os.listdir(where):
            try:
                got = os.stat(os.path.join(where, fd))
            except OSError:
                continue
            if (got.st_dev, got.st_ino) in want:
                names.add(want[(got.st_dev, got.st_ino)])
        return names

    @staticmethod
    def chain(pid):
        """`pid` and each parent above it, up to the one this process
        started: the push's whole process line, the helper at its foot."""
        line = [pid]
        while pid > 1:
            with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
                pid = int(fh.read().rsplit(")", 1)[1].split()[1])
            if pid == os.getpid():
                return line
            line.append(pid)
        raise AssertionError("the helper is not under this process")

    def test_a_child_a_successful_push_leaves_behind_holds_no_lock(self):  # noqa: VACUOUS_ASSERTION — the push positively lands, and every lock is positively held while its git runs
        st = self.to_gating()
        self.green(st)
        remote = os.path.join(self.tmp, "remote.git")
        _git(self.tmp, "clone", "-q", "--bare", self.repo, remote)
        base = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "land")
        head = _git(self.repo, "rev-parse", "HEAD")
        where = os.path.join(self.tmp, "bin")
        os.makedirs(where)
        mark = lambda name: os.path.join(self.tmp, name)  # noqa: E731
        self.addCleanup(_reap, mark("sleeper.pid"))
        helper = os.path.join(where, "git-remote-linger")
        with open(helper, "w", encoding="utf-8") as fh:
            fh.write(_LINGER % {"dir": self.tmp, "remote": remote})
        os.chmod(helper, 0o755)

        def push(root, _head, _target, keep=(), lease=None):
            self.ops.calls.append("push")
            return autoland.Ops.push(self.ops, root, head, autoland.PushTarget(
                "linger::trunk", "refs/heads/main", "origin"), keep,
                lease=base)

        self.ops.push = push
        seen = {}

        def while_git_runs():
            end = time.monotonic() + 60
            while not os.path.exists(mark("helper.started")) \
                    and time.monotonic() < end:
                time.sleep(0.05)
            try:
                line = self.chain(_pid(mark("helper.pid")))
                seen["held"] = self.held()
                seen["top"] = self.opened_by(line[-1])
                seen["below"] = set().union(*(self.opened_by(pid)
                                              for pid in line[:-1]))
                seen["depth"] = len(line) - 1
            finally:
                open(mark("helper.release"), "w").close()

        watcher = threading.Thread(target=while_git_runs, daemon=True)
        watcher.start()
        with mock.patch.dict(os.environ, {
                "PATH": where + os.pathsep + os.environ.get("PATH", "")}):
            rc, out = self.tick()
        watcher.join(60)
        self.assertIn("push", self.ops.calls, out)
        # the push succeeded: the remote reads the head
        self.assertEqual(_git(remote, "rev-parse", "main"), head, out)
        every = {name for name, _path in self.locks()}
        # the control, while git ran: every lock held, by the holder at the
        # top of the push's process line, and nothing under it (the pushing
        # git, git's helper wrapper, the helper) had one open
        self.assertEqual(seen.get("held"), dict.fromkeys(every, True), out)
        self.assertEqual(seen.get("top"), every, out)
        self.assertGreaterEqual(seen.get("depth", 0), 2, out)
        self.assertEqual(seen.get("below"), set(), out)
        # the tick is done and the sleeper the push left behind still runs,
        # holding no lock: each one can be taken at once
        sleeper = _pid(mark("sleeper.pid"))
        self.assertIsNotNone(sleeper, out)
        self.assertFalse(_gone(sleeper, 0), "the sleeper ended too soon")
        self.assertEqual(self.held(), dict.fromkeys(every, False))


#: A second process asking for each lock path it is given, without waiting:
#: prints {path: whether it was refused}.
_FLOCK_PROBE = r"""
import fcntl, json, os, sys
got = {}
for path in sys.argv[1:]:
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        got[path] = False
    except BlockingIOError:
        got[path] = True
    finally:
        os.close(fd)
print(json.dumps(got))
"""

#: The `git` first on PATH in the timeout arm: every call goes to the real
#: git, except a push, which runs the real push to its end (the update
#: applies) and then never answers, as a push whose answer is lost would,
#: until the push's timeout kills its whole group.
_HANGING_GIT = """#!/bin/sh
for a in "$@"; do
  if [ "$a" = push ]; then
    '%(git)s' "$@" >>'%(dir)s/push.out' 2>&1
    : > '%(dir)s/push.applied'
    exec sleep 120
  fi
done
exec '%(git)s' "$@"
"""


class RemoteOps(FakeOps):
    """FakeOps over a REAL bare remote at a file:// URL: the plan stands on
    its trunk X, the compose's head is H (one commit on X), and trunk reads
    what the remote holds. The push is each arm's; the settle read is the
    real one (`Ops.trunk_at`), asked of the remote's real destination."""

    def __init__(self, test, bare, x, h):
        super().__init__(test)
        self.bare, self.x, self.h = bare, x, h
        self.real = autoland.PushTarget("file://" + bare, "refs/heads/main",
                                        "origin")
        self.settle_reads = []
        self.on_settle = None

    def trunk(self):
        return _git(self.bare, "rev-parse", "main")

    def plan(self, root):
        got, why = super().plan(root)
        got["trunk"] = self.x
        got["authority"] = dict(got["authority"], sha=self.x)
        return got, why

    def compose(self, got):
        answer = super().compose(got)
        if answer["head"]:
            answer["head"] = self.heads[got["room"]] = self.h
        return answer

    def authority(self, root):
        return dict(super().authority(root), sha=self.trunk())

    def remote_head(self, root):
        return self.trunk()

    def ancestry(self, root, older, newer):
        if {older, newer} <= {self.x, self.h}:
            return vcs.backend(root).ancestry(root, older, newer)
        return super().ancestry(root, older, newer)

    def settled_trunk(self, root, target):
        self.calls.append("settled_trunk")
        self.test.assertEqual(target, "private://trunk")
        if self.on_settle:
            self.on_settle()
        got = autoland.Ops.trunk_at(self, root, self.real)
        self.settle_reads.append(got)
        return got


class AmbiguousPush(Base):
    """helm-codex B1 and B3 (row 4143d2ef9053): a push whose update applied
    while git did not say so, because its answer was lost or git timed out
    or was killed. THE RULING: AN AMBIGUOUS PUSH NEVER RETRIES BY ITSELF.
    Still holding the three locks, the tick waits a settle and reads trunk
    once through the pinned, vetted destination: the head is recorded PUSHED
    and the land goes on; the lease (the trunk the train was gated on) STOPS
    it, because an update that never applied and one that applied and was
    rewound since read the same; anything else STOPS it. Driven through the
    shipped tick over a real bare remote at a file:// URL."""

    def setUp(self):
        super().setUp()
        x = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "the train")
        h = _git(self.repo, "rev-parse", "HEAD")
        bare = os.path.join(self.tmp, "remote.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", bare)
        _git(self.repo, "push", "-q", bare, "%s:refs/heads/main" % x)
        self.ops = RemoteOps(self, bare, x, h)
        self.x, self.h = x, h

    def landing(self, push):
        """The train GREEN on the gated trunk X, its head H, one tick from
        its push, with `push` as the push."""
        st = self.to_gating()
        self.green(st)
        self.ops.receipt_rows[0]["head"] = self.x
        self.assertEqual((st["trunk"], st["head"]), (self.x, self.h))
        self.ops.push = push
        self.ops.calls = []

    def applied_then_failed(self, rewind):
        """A push whose update X->H applies on the remote while git reports
        failure; with `rewind`, an honest actor then rewinds trunk H->X
        before the tick's next step."""
        def push(root, head, target, keep=(), lease=None):
            self.ops.calls.append("push")
            self.assertEqual((head, lease), (self.h, self.x))
            _git(self.repo, "push", "-q", self.ops.real.url,
                 "%s:refs/heads/main" % head)
            self.assertEqual(self.ops.trunk(), self.h)
            if rewind:
                _git(self.ops.bare, "update-ref", "refs/heads/main", self.x,
                     self.h)
            return False, "fatal: the remote end hung up unexpectedly"
        return push

    def real_push(self, root, head, target, keep=(), lease=None):
        self.ops.calls.append("push")
        return autoland.Ops.push(self.ops, root, head, self.ops.real, keep,
                                 lease=lease)

    def locks(self):
        return {"tick": autoland._lock_path(self.root, "tick"),
                "store": autoland._lock_path(self.root, "state"),
                "dispatch ledger": landwindow.readiness_lock_path()}

    def refused_to_another_process(self):
        """{lock: whether a second process is refused it}, asked by a fresh
        python that tries each without waiting."""
        paths = self.locks()
        out = subprocess.run([sys.executable, "-c", _FLOCK_PROBE]
                             + list(paths.values()), capture_output=True,
                             text=True, timeout=60, check=True).stdout
        got = json.loads(out)
        return {name: got[path] for name, path in paths.items()}

    def assert_landed(self, out):
        self.assertEqual(self.ops.trunk(), self.h, out)
        self.assertIsNone(self.current(), out)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE], out)
        self.assertRegex(self.ops.posts[-1],
                         r"LAND 384: [^.]+\. PUSHED %s " % self.h[:11])
        self.assertEqual(autoland.read_counter(self.root)["sha"], self.h)

    def test_an_update_applied_then_rewound_is_never_pushed_again(self):  # noqa: VACUOUS_ASSERTION — the train positively stops naming both readings, and the no-rewind control lands the same push
        """THE ABA: the update X->H applied and git's answer was lost, and
        an honest integrator rewound trunk H->X before the next step. Read
        after the settle, trunk is the lease again, which is also what an
        update that never applied leaves, so the train stops naming both
        readings, and no later tick pushes H again. Its control, the same
        push with no rewind, is the next arm."""
        self.landing(self.applied_then_failed(rewind=True))
        _rc, out = self.tick()
        self.assertEqual(self.ops.calls.count("push"), 1, out)
        self.assertEqual(self.ops.trunk(), self.x)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED, out)
        why = st["stopped"]["why"]
        self.assertIn("AMBIGUOUS", why)
        self.assertIn("never applied", why)
        self.assertIn("applied and was rewound", why)
        self.assertIn(self.h[:12], why)
        self.assertIn(self.x[:12], why)
        self.assertIn("the remote end hung up unexpectedly", why)
        self.assertNotIn("reads the remote first", why)
        self.assertEqual(self.ops.settle_reads, [(self.x, None)])
        self.assertLess(self.ops.calls.index("push"),
                        self.ops.calls.index("settled_trunk"))
        # two further ticks: the train stays stopped for a person, and H is
        # never pushed again
        for _ in range(2):
            self.ops.clock += 120
            self.tick()
        self.assertEqual(self.ops.calls.count("push"), 1)
        self.assertEqual(self.ops.trunk(), self.x)
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        self.assertEqual(self.current()["stopped"]["why"], why)

    def test_an_update_applied_with_its_answer_lost_is_recorded_pushed(self):  # noqa: VACUOUS_ASSERTION — the archive positively holds the train DONE and trunk reads the head
        """ACK LOST, NO REWIND (the ABA's control): the same push, and trunk
        reads the head after the settle, so the land is recorded PUSHED and
        goes on exactly as a clean one does."""
        self.landing(self.applied_then_failed(rewind=False))
        _rc, out = self.tick()
        self.assertEqual(self.ops.calls.count("push"), 1, out)
        self.assertEqual(self.ops.settle_reads, [(self.h, None)], out)
        for earlier, later in (("push", "settled_trunk"),
                               ("settled_trunk", "ff"),
                               ("ff", "fold_apply")):
            self.assertLess(self.ops.calls.index(earlier),
                            self.ops.calls.index(later),
                            (earlier, later, self.ops.calls))
        self.assert_landed(out)

    def test_a_push_its_timeout_killed_holds_its_locks_through_the_read(self):  # noqa: VACUOUS_ASSERTION — every lock is positively refused to a second process during the read, and the archive holds the train DONE
        """B3: the push's timeout killed its whole group after the update
        applied. The tick still holds the three locks through the settle
        read: a second process is refused each one while it runs. Trunk
        reads the head, so the land is recorded PUSHED. The control, on the
        same observable: every lock is free once the tick is done."""
        where = os.path.join(self.tmp, "bin")
        os.makedirs(where)
        with open(os.path.join(where, "git"), "w", encoding="utf-8") as fh:
            fh.write(_HANGING_GIT % {"dir": self.tmp,
                                     "git": shutil.which("git")})
        os.chmod(os.path.join(where, "git"), 0o755)
        self.landing(self.real_push)
        seen = {}
        self.ops.on_settle = lambda: seen.update(
            self.refused_to_another_process())
        with mock.patch.dict(os.environ, {
                "PATH": where + os.pathsep + os.environ.get("PATH", "")}), \
                mock.patch.object(autoland, "PUSH_TIMEOUT_S", 3):
            _rc, out = self.tick()
        self.assertTrue(os.path.exists(os.path.join(self.tmp,
                                                    "push.applied")), out)
        self.assertEqual(self.ops.calls.count("push"), 1, out)
        every = set(self.locks())
        self.assertEqual(seen, dict.fromkeys(every, True), out)
        self.assertEqual(self.ops.settle_reads, [(self.h, None)], out)
        self.assert_landed(out)
        self.assertEqual(self.refused_to_another_process(),
                         dict.fromkeys(every, False))

    def test_a_clean_push_is_unchanged(self):  # noqa: VACUOUS_ASSERTION — the archive positively holds the train DONE and trunk reads the head
        """The control: git answers success, nothing more is read, and the
        land goes on."""
        self.landing(self.real_push)
        _rc, out = self.tick()
        self.assertEqual(self.ops.calls.count("push"), 1, out)
        self.assertEqual(self.ops.calls.count("settled_trunk"), 0, out)
        self.assert_landed(out)

    def test_the_settle_read_goes_where_the_push_went(self):
        """The read is made through the one-time name the push is pinned to
        (`_pinned`): a url rewrite of the trunk URL, which a read of the
        literal URL follows, moves it nowhere."""
        mirror = os.path.join(self.tmp, "mirror.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", mirror)
        _git(self.repo, "push", "-q", mirror, "%s:refs/heads/main" % self.h)
        _git(self.repo, "config", "url.%s.insteadOf" % mirror,
             self.ops.real.url)
        # the premise: a read of the literal trunk URL reads the mirror
        self.assertEqual(_git(self.repo, "ls-remote", self.ops.real.url,
                              "refs/heads/main").split()[0], self.h)
        self.assertEqual(autoland.Ops().trunk_at(self.repo, self.ops.real),
                         (self.x, None))

    # -- a tick killed mid-push (the sibling of B1: the same ABA, reached
    # through a dead tick instead of a lost answer; the integrator's ruling)

    def killed_mid_push(self, applied):
        """A TICK KILLED MID-PUSH: its push was sent and the tick died before
        it recorded git's answer, so what it wrote before the push is all the
        next tick finds. With `applied`, the update X->H applied on the
        remote first. Every later push is the real one."""
        def push(root, head, target, keep=(), lease=None):
            self.ops.calls.append("push")
            self.assertEqual((head, lease), (self.h, self.x))
            if applied:
                _git(self.repo, "push", "-q", self.ops.real.url,
                     "%s:refs/heads/main" % head)
            raise RuntimeError("the tick was killed mid-push")
        self.landing(push)
        with self.assertRaises(RuntimeError):
            self.tick()
        st = self.current()
        self.assertEqual((st["state"], st["step"]),
                         (autoland.LANDING, "pushing"))
        self.assertEqual(self.ops.trunk(), self.h if applied else self.x)
        self.ops.push = self.real_push
        self.ops.calls = []
        self.ops.clock += 120

    def assert_stopped_ambiguous(self, out):
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED, out)
        why = st["stopped"]["why"]
        for words in ("AMBIGUOUS", "killed before it recorded",
                      "never applied", "applied and was rewound",
                      self.x[:12],
                      # a person's --resume keeps its meaning, and says so
                      "--resume` pushes %s again" % self.h[:12]):
            self.assertIn(words, why)
        return why

    def test_a_push_a_killed_tick_sent_then_rewound_is_never_pushed_again(self):  # noqa: VACUOUS_ASSERTION — the train positively stops AMBIGUOUS naming both readings after one settle read of the lease under all three locks, and the no-rewind control lands the same dead tick's push
        """THE DEAD TICK'S ABA: the update X->H applied, the tick was killed
        before it recorded git's answer, and an honest integrator rewound
        trunk H->X before the next tick. That tick finds a push sent and
        unanswered, so it never pushes by itself: under the same three locks
        it reads trunk once (`settle`), finds the lease, and stops naming
        both readings. Two further ticks push nothing. Its control, the same
        dead tick with no rewind, is the next arm."""
        self.killed_mid_push(applied=True)
        _git(self.ops.bare, "update-ref", "refs/heads/main", self.x, self.h)
        seen = {}
        self.ops.on_settle = lambda: seen.update(
            self.refused_to_another_process())
        _rc, out = self.tick()
        self.assertEqual(self.ops.calls.count("push"), 0, out)
        self.assertEqual(self.ops.trunk(), self.x, out)
        why = self.assert_stopped_ambiguous(out)
        self.assertEqual(self.ops.settle_reads, [(self.x, None)], out)
        self.assertEqual(seen, dict.fromkeys(self.locks(), True), out)
        for _ in range(2):
            self.ops.clock += 120
            self.tick()
        self.assertEqual(self.ops.calls.count("push"), 0)
        self.assertEqual(self.ops.trunk(), self.x)
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        self.assertEqual(self.current()["stopped"]["why"], why)

    def test_a_push_a_killed_tick_sent_that_applied_is_recorded_pushed(self):  # noqa: VACUOUS_ASSERTION — the archive positively holds the train DONE at the head, read once under all three locks
        """THE DEAD TICK, NO REWIND (the ABA's control): the update applied
        before the tick died, and trunk reads H. The next tick reads it once
        under the same three locks (`settle`), records the land PUSHED and
        lands it DONE, pushing nothing again."""
        self.killed_mid_push(applied=True)
        seen = {}
        self.ops.on_settle = lambda: seen.update(
            self.refused_to_another_process())
        _rc, out = self.tick()
        self.assertEqual(self.ops.calls.count("push"), 0, out)
        self.assertEqual(self.ops.settle_reads, [(self.h, None)], out)
        self.assertEqual(seen, dict.fromkeys(self.locks(), True), out)
        self.assertLess(self.ops.calls.index("settled_trunk"),
                        self.ops.calls.index("ff"), self.ops.calls)
        self.assert_landed(out)

    def test_a_push_a_killed_tick_sent_that_never_applied_stops(self):  # noqa: VACUOUS_ASSERTION — the train positively stops AMBIGUOUS on one settle read of the lease, and the resumed control pushes the head once and lands it DONE
        """The dead tick's update never applied, so trunk reads X, the
        lease: what an update that applied and was rewound leaves too, and
        the two cannot be told apart. The train STOPS AMBIGUOUS, nothing
        pushed. The control on the same observable: a person's `--resume` is
        the order to push again, and the next tick pushes H once and lands
        it."""
        self.killed_mid_push(applied=False)
        _rc, out = self.tick()
        self.assertEqual(self.ops.calls.count("push"), 0, out)
        self.assertEqual(self.ops.trunk(), self.x, out)
        self.assert_stopped_ambiguous(out)
        self.assertEqual(self.ops.settle_reads, [(self.x, None)], out)
        _said, why = autoland.resume(self.root, "integrator",
                                     now=self.ops.clock)
        self.assertIsNone(why, why)
        self.ops.clock += 120
        _rc, out = self.tick()
        self.assertEqual(self.ops.calls.count("push"), 1, out)
        self.assertEqual(self.ops.settle_reads, [(self.x, None)], out)
        self.assert_landed(out)

    def test_a_tick_killed_before_its_push_was_sent_pushes_as_before(self):  # noqa: VACUOUS_ASSERTION — the archive positively holds the train DONE at the head, pushed once
        """The control on the ruling's own boundary: a tick killed in its
        last word BEFORE the push was sent (at its last destination read)
        also leaves LANDING/pushing, but no push began, so nothing is
        ambiguous: the next tick pushes H once, reads nothing after it, and
        lands it."""
        real = self.ops.push_target
        reads = []

        def killed_at_the_last_read(root):
            reads.append(root)
            if len(reads) == 2:
                raise RuntimeError("the tick was killed in its last word")
            return real(root)

        self.landing(self.real_push)
        self.ops.push_target = killed_at_the_last_read
        with self.assertRaises(RuntimeError):
            self.tick()
        self.assertEqual(self.ops.calls.count("push"), 0)
        self.assertEqual(self.ops.trunk(), self.x)
        self.assertEqual((self.current()["state"], self.current()["step"]),
                         (autoland.LANDING, "pushing"))
        self.ops.clock += 120
        _rc, out = self.tick()
        self.assertEqual(self.ops.calls.count("push"), 1, out)
        self.assertEqual(self.ops.calls.count("settled_trunk"), 0, out)
        self.assert_landed(out)


class TimerUnit(unittest.TestCase):
    def test_a_stop_of_the_unit_signals_the_tick_alone(self):
        """B2 (the binding door read of b8fa6ef6128): systemd's default
        KillMode, control-group, signals every process of the unit on a stop
        or restart: the tick, the push's lock holder and git, so every lock
        dropped while an update git had sent could still land. The unit
        auto-land installs sets KillMode=process, in its [Service] section.
        Rendered only: no unit is written and no systemd is started."""
        _spath, service, _tpath, _timer = autoland.timer_units()
        section = service.split("[Service]\n", 1)[1]
        lines = [ln.strip() for ln in section.splitlines()]
        self.assertIn("KillMode=process", lines)
        self.assertEqual(service.count("KillMode="), 1)


class RealGuards(unittest.TestCase):
    """The push and fast-forward seams themselves, on temp repositories."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-autoland-git-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home")})
        env.start()
        self.addCleanup(env.stop)
        self.remote = os.path.join(self.tmp, "remote.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", self.remote)
        self.repo = os.path.realpath(os.path.join(self.tmp, "proj"))
        _git(self.tmp, "clone", "-q", self.remote, self.repo)
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
        _git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(self.repo, "config", "helm.trunkRef", "refs/heads/main")
        _git(self.repo, "config", "helm.trunkRemote", "origin")
        _git(self.repo, "config", "helm.trunkUrl", self.remote)
        self.ops = autoland.Ops()

    def visibility(self, answer):
        return mock.patch.object(hostpath_guard, "_visibility",
                                 lambda url: (answer, None))

    def test_a_private_declared_remote_passes(self):
        with self.visibility(hostpath_guard.PRIVATE):
            ok, why = self.ops.push_guard(self.repo)
        self.assertTrue(ok, why)

    def test_a_public_remote_refuses_the_push(self):
        with self.visibility(hostpath_guard.PUBLIC):
            ok, why = self.ops.push_guard(self.repo)
        self.assertFalse(ok)
        self.assertIn("PUBLIC", why)

    def test_an_unknown_visibility_refuses_the_push(self):
        with self.visibility(hostpath_guard.UNKNOWN):
            ok, why = self.ops.push_guard(self.repo)
        self.assertFalse(ok)

    def test_an_undeclared_trunk_remote_refuses_the_push(self):
        _git(self.repo, "config", "--unset", "helm.trunkRemote")
        with self.visibility(hostpath_guard.PRIVATE):
            ok, why = self.ops.push_guard(self.repo)
        self.assertFalse(ok)
        self.assertIn("helm.trunkRemote", why)

    def test_an_undeclared_trunk_url_refuses_the_push(self):
        _git(self.repo, "config", "--unset", "helm.trunkUrl")
        with self.visibility(hostpath_guard.PRIVATE):
            ok, why = self.ops.push_guard(self.repo)
        self.assertFalse(ok)
        self.assertIn("helm.trunkUrl", why)

    def test_a_fork_url_refuses_the_push(self):
        _git(self.repo, "config", "helm.trunkUrl",
             os.path.join(self.tmp, "another.git"))
        with self.visibility(hostpath_guard.PRIVATE):
            ok, why = self.ops.push_guard(self.repo)
        self.assertFalse(ok)
        self.assertIn("fork or mirror", why)

    def only_the_trunk_url_reads_private(self):
        return mock.patch.object(
            hostpath_guard, "_visibility",
            lambda url: ((hostpath_guard.PRIVATE, None) if url == self.remote
                         else (hostpath_guard.PUBLIC, None)))

    def test_a_second_url_on_the_trunk_remote_refuses_the_push(self):
        # `git push origin` pushes to EVERY url of origin, and `git remote
        # get-url` names only the first: a mirror added as a second url
        # receives the land too.
        mirror = os.path.join(self.tmp, "mirror.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", mirror)
        with self.only_the_trunk_url_reads_private():
            ok, why = self.ops.push_guard(self.repo)
            self.assertTrue(ok, why)            # the control: one url
            _git(self.repo, "config", "--add", "remote.origin.url", mirror)
            ok, why = self.ops.push_guard(self.repo)
        self.assertFalse(ok, why)
        self.assertIn(mirror, why)

    def test_a_second_push_url_on_the_trunk_remote_refuses_the_push(self):
        mirror = os.path.join(self.tmp, "mirror.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", mirror)
        _git(self.repo, "config", "--add", "remote.origin.pushurl",
             self.remote)
        with self.only_the_trunk_url_reads_private():
            ok, why = self.ops.push_guard(self.repo)
            self.assertTrue(ok, why)            # the control: one push url
            _git(self.repo, "config", "--add", "remote.origin.pushurl",
                 mirror)
            ok, why = self.ops.push_guard(self.repo)
        self.assertFalse(ok, why)
        self.assertIn(mirror, why)

    def test_a_remote_mutation_after_the_guard_cannot_redirect_the_push(self):  # noqa: VACUOUS_ASSERTION — the same push advances the vetted trunk while the injected mirror stays at its positive-control base
        mirror = os.path.join(self.tmp, "mirror.git")
        _git(self.tmp, "clone", "-q", "--bare", self.remote, mirror)
        base = _git(self.remote, "rev-parse", "main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "land")
        head = _git(self.repo, "rev-parse", "HEAD")
        with self.visibility(hostpath_guard.PRIVATE):
            target, why = self.ops.push_target(self.repo)
        self.assertEqual(target, autoland.PushTarget(
            self.remote, "refs/heads/main", "origin"), why)
        _git(self.repo, "config", "--add", "remote.origin.pushurl", mirror)
        pushed, detail = self.ops.push(self.repo, head, target, lease=base)
        self.assertTrue(pushed, detail)
        self.assertEqual(_git(self.remote, "rev-parse", "main"), head)
        self.assertEqual(_git(mirror, "rev-parse", "main"), base)

    def test_a_trunk_ref_changed_after_the_guard_cannot_move_the_push(self):  # noqa: VACUOUS_ASSERTION — the vetted ref positively advances to the pushed head
        """task/3265 races F1: the destination ref is resolved ONCE, by the
        read that vets it, and the push carries that value: helm.trunkRef
        rewritten between the vetting and the push moves nothing."""
        base = _git(self.remote, "rev-parse", "main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "land")
        head = _git(self.repo, "rev-parse", "HEAD")
        with self.visibility(hostpath_guard.PRIVATE):
            target, why = self.ops.push_target(self.repo)
        self.assertIsNotNone(target, why)
        _git(self.repo, "config", "helm.trunkRef", "refs/heads/elsewhere")
        pushed, detail = self.ops.push(self.repo, head, target, lease=base)
        self.assertTrue(pushed, detail)
        # the vetted ref moved, and the one written after the vetting never
        # came to exist on the remote
        self.assertNotEqual(base, head)
        self.assertEqual(_git(self.remote, "rev-parse", "main"), head)
        self.assertEqual(_git(self.remote, "for-each-ref", "--format",
                              "%(refname)"), "refs/heads/main")

    def test_a_dirty_shared_checkout_refuses_the_fast_forward(self):
        base = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "next")
        head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "reset", "-q", "--hard", base)
        with open(os.path.join(self.repo, "stray.txt"), "w") as fh:
            fh.write("dirt\n")
        ok, why = self.ops.ff(self.repo, head)
        self.assertFalse(ok)
        self.assertIn("dirty", why)
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), base)
        # the control: clean, the same fast-forward lands
        os.remove(os.path.join(self.repo, "stray.txt"))
        ok, why = self.ops.ff(self.repo, head)
        self.assertTrue(ok, why)
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), head)

    def test_the_push_is_fast_forward_only(self):  # noqa: VACUOUS_ASSERTION — the remote's unchanged head is the refusal observable; other real-guard arms positively push the same target
        """Fast-forward-only against the gated trunk (door read B1): a lease
        that matches the remote lifts git's own fast-forward refusal
        (measured, git 2.53), so a head that does not descend from the lease
        is refused before git runs, even with the lease reading true."""
        base = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "ahead")
        ahead = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(self.repo, "reset", "-q", "--hard", base)
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "diverged")
        diverged = _git(self.repo, "rev-parse", "HEAD")
        with self.visibility(hostpath_guard.PRIVATE):
            target, why = self.ops.push_target(self.repo)
        self.assertEqual(target, autoland.PushTarget(
            self.remote, "refs/heads/main", "origin"), why)
        ok, detail = self.ops.push(self.repo, diverged, target, lease=ahead)
        self.assertFalse(ok)
        self.assertIn("not a fast-forward of the gated trunk", detail)
        self.assertEqual(_git(self.remote, "rev-parse", "main"), ahead)

    def test_a_trunk_rewound_beside_the_push_is_never_re_landed(self):  # noqa: VACUOUS_ASSERTION — the control pushes the same head once
        """B1 (the binding door read of b8fa6ef6128): an integrator backs out
        land X by rewinding trunk from X to its parent W after the train was
        composed and gated on X. W is an ancestor of the head, so a plain
        fast-forward push re-landed X. The push's lease is the gated trunk,
        X, and the remote refuses the update because trunk no longer reads
        it; the control, trunk unchanged at X, takes the same push once."""
        w = _git(self.remote, "rev-parse", "main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "land X")
        x = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "the train")
        head = _git(self.repo, "rev-parse", "HEAD")
        with self.visibility(hostpath_guard.PRIVATE):
            target, why = self.ops.push_target(self.repo)
        self.assertEqual(target, autoland.PushTarget(
            self.remote, "refs/heads/main", "origin"), why)
        _git(self.remote, "update-ref", "refs/heads/main", w, x)
        pushed, detail = self.ops.push(self.repo, head, target, lease=x)
        self.assertFalse(pushed, detail)
        self.assertIn("stale info", detail)
        self.assertEqual(_git(self.remote, "rev-parse", "main"), w)
        # the control: trunk reads the gated trunk, and the same push lands
        _git(self.remote, "update-ref", "refs/heads/main", x, w)
        pushed, detail = self.ops.push(self.repo, head, target, lease=x)
        self.assertTrue(pushed, detail)
        self.assertEqual(_git(self.remote, "rev-parse", "main"), head)

    # A PUSH TO A LITERAL URL IS STILL REWRITTEN BY CONFIG THE URL READS NEVER
    # SHOW (task/3265 door read, measured on git 2.53): `git remote get-url`
    # prints the trunk URL while a url.<x>.pushInsteadOf or insteadOf rule whose
    # prefix matches it, or a remote section NAMED by it, sends the push to x.
    def mirror(self):
        path = os.path.join(self.tmp, "mirror.git")
        _git(self.tmp, "clone", "-q", "--bare", self.remote, path)
        return path

    def assert_rewrite_refused(self, key, value):
        """The remote's own URL reads stay the trunk (the pre-cure guard
        passes), while the rule sends a push to the literal trunk URL to
        MIRROR: only the rewrite check can refuse it."""
        mirror = self.mirror()
        key, value = key.replace("MIRROR", mirror), value.replace(
            "MIRROR", mirror)
        with self.visibility(hostpath_guard.PRIVATE):
            ok, why = self.ops.push_guard(self.repo)
            self.assertTrue(ok, why)            # the control: no rewrite
            _git(self.repo, "config", "--add", key, value)
            for extra in ((), ("--push",)):     # the premise: reads unchanged
                self.assertEqual(_git(self.repo, "remote", "get-url", "--all",
                                      *extra, "origin"), self.remote)
            ok, why = self.ops.push_guard(self.repo)
        self.assertFalse(ok, why)
        self.assertIn("rewrit", why)

    def explicit_pushurl(self):
        # git applies no pushInsteadOf to an explicit pushurl, so get-url
        # --push shows the trunk; a push to the literal URL still applies it
        _git(self.repo, "config", "remote.origin.pushurl", self.remote)

    def test_a_push_instead_of_rule_on_the_trunk_url_refuses_the_push(self):
        self.explicit_pushurl()
        self.assert_rewrite_refused("url.MIRROR.pushInsteadOf", self.remote)

    def test_a_chained_instead_of_rule_on_the_trunk_url_refuses_the_push(self):
        # origin names an alias that get-url rewrites ONCE, to the trunk; a
        # push to the trunk URL is rewritten again, to the mirror
        alias = "trunk-alias:"
        _git(self.repo, "config", "remote.origin.url", alias)
        _git(self.repo, "config", "url.%s.insteadOf" % self.remote, alias)
        self.assert_rewrite_refused("url.MIRROR.insteadOf", self.remote)

    def test_a_rule_on_a_prefix_of_the_trunk_url_refuses_the_push(self):
        self.explicit_pushurl()
        self.assert_rewrite_refused("url.MIRROR/.pushInsteadOf",
                                    os.path.dirname(self.remote) + "/")

    def test_a_remote_section_named_by_the_trunk_url_refuses_the_push(self):
        self.assert_rewrite_refused("remote.%s.pushurl" % self.remote,
                                    "MIRROR")

    def test_a_rewrite_added_after_the_guard_is_refused_by_the_push(self):
        mirror = self.mirror()
        base = _git(self.remote, "rev-parse", "main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "land")
        head = _git(self.repo, "rev-parse", "HEAD")
        with self.visibility(hostpath_guard.PRIVATE):
            target, why = self.ops.push_target(self.repo)
        self.assertEqual(target, autoland.PushTarget(
            self.remote, "refs/heads/main", "origin"), why)
        _git(self.repo, "config", "url.%s.pushInsteadOf" % mirror, self.remote)
        pushed, detail = self.ops.push(self.repo, head, target, lease=base)
        self.assertFalse(pushed, detail)
        self.assertIn("rewrit", detail)
        self.assertEqual(_git(self.remote, "rev-parse", "main"), base)
        self.assertEqual(_git(mirror, "rev-parse", "main"), base)
        # the control: the rule gone, the same push lands on the trunk
        _git(self.repo, "config", "--unset",
             "url.%s.pushInsteadOf" % mirror)
        pushed, detail = self.ops.push(self.repo, head, target, lease=base)
        self.assertTrue(pushed, detail)
        self.assertEqual(_git(self.remote, "rev-parse", "main"), head)

    def test_a_rewrite_written_beside_the_push_moves_nothing(self):  # noqa: VACUOUS_ASSERTION — the vetted trunk positively advances to each pushed head
        """task/3265 races R1: every check `push` makes can pass and git
        still reads its config after them. A url rewrite, or a remote section
        named by the trunk URL, written after the last check and before git
        read its config redirected a push to the literal URL (measured on git
        2.53). Whatever is written beside the push, the vetted trunk advances
        and the mirror does not. The URL is spelled file:// because git
        ignores a remote section whose name begins with '/'."""
        url = "file://" + self.remote
        _git(self.repo, "config", "remote.origin.url", url)
        _git(self.repo, "config", "helm.trunkUrl", url)
        mirror = self.mirror()
        base = _git(mirror, "rev-parse", "main")
        real = vcs.GitVcs.run_holding
        for key, value in (("url.%s.pushInsteadOf" % mirror, url),
                           ("url.%s.insteadOf" % mirror, url),
                           ("remote.%s.pushurl" % url, mirror)):
            lease = _git(self.remote, "rev-parse", "main")
            _git(self.repo, "commit", "-q", "--allow-empty", "-m", key)
            head = _git(self.repo, "rev-parse", "HEAD")
            with self.visibility(hostpath_guard.PRIVATE):
                target, why = self.ops.push_target(self.repo)
            self.assertEqual(target, autoland.PushTarget(
                url, "refs/heads/main", "origin"), why)

            def written_beside_the_push(be, cwd, *args, **kw):
                _git(self.repo, "config", key, value)
                return real(be, cwd, *args, **kw)

            with mock.patch.object(vcs.GitVcs, "run_holding",
                                   written_beside_the_push):
                pushed, detail = self.ops.push(self.repo, head, target,
                                               lease=lease)
            _git(self.repo, "config", "--unset", key)
            self.assertTrue(pushed, "%s: %s" % (key, detail))
            self.assertEqual(_git(self.remote, "rev-parse", "main"), head, key)
            self.assertEqual(_git(mirror, "rev-parse", "main"), base, key)

    def transport(self, scheme, body):
        """A git remote helper `git-remote-<scheme>` running `body`, on a
        PATH the returned patch installs."""
        where = os.path.join(self.tmp, "bin")
        os.makedirs(where, exist_ok=True)
        path = os.path.join(where, "git-remote-" + scheme)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return mock.patch.dict(os.environ, {
            "PATH": where + os.pathsep + os.environ.get("PATH", "")})

    def test_a_push_that_times_out_takes_its_transport_helper_with_it(self):  # noqa: VACUOUS_ASSERTION — the helper positively ran and recorded its pid
        """task/3265 races R3: the push's timeout killed only git, its direct
        child, and the transport helper git started (here one that never
        answers, as a stalled network would leave it) ran on holding every
        descriptor the push inherited. The whole process group goes."""
        pids = os.path.join(self.tmp, "helper.pid")
        self.addCleanup(_reap, pids)
        base = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "land")
        head = _git(self.repo, "rev-parse", "HEAD")
        target = autoland.PushTarget("hang::trunk", "refs/heads/main",
                                     "origin")
        with self.transport("hang", "echo $$ > '%s'\nexec sleep 120\n"
                            % pids), \
                mock.patch.object(autoland, "PUSH_TIMEOUT_S", 3):
            pushed, detail = self.ops.push(self.repo, head, target,
                                           lease=base)
        self.assertFalse(pushed, detail)
        # the push reached its transport: the helper recorded its pid
        pid = _pid(pids)
        self.assertIsNotNone(pid, detail)
        self.assertTrue(_gone(pid, 20),
                        "the transport helper %d outlived its push" % pid)

    def test_a_planted_exec_path_never_answers_the_push(self):  # noqa: VACUOUS_ASSERTION — the premise push positively runs the planted program, and the cured push positively advances the vetted trunk
        """task/3265 r4 (helm-codex R1 of round 3): git looks for a
        push's transport in GIT_EXEC_PATH before PATH, so a planted
        directory's `git-receive-pack` (a local path) or `git-remote-<scheme>`
        (a URL) could answer the push in place of git's own. The push drops
        every variable that names a program git runs (`run_holding`): it
        reaches the vetted trunk, and no planted program runs."""
        planted = os.path.join(self.tmp, "planted")
        os.makedirs(planted)
        ran = os.path.join(self.tmp, "planted-ran")
        for prog in ("git-receive-pack", "git-remote-file",
                     "git-remote-https", "git-remote-ext"):
            path = os.path.join(planted, prog)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\necho %s >> '%s'\nexit 0\n" % (prog, ran))
            os.chmod(path, 0o755)
        base = _git(self.remote, "rev-parse", "main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "land")
        head = _git(self.repo, "rev-parse", "HEAD")
        with self.visibility(hostpath_guard.PRIVATE):
            target, why = self.ops.push_target(self.repo)
        self.assertEqual(target, autoland.PushTarget(
            self.remote, "refs/heads/main", "origin"), why)
        with mock.patch.dict(os.environ, {"GIT_EXEC_PATH": planted}):
            # the premise: a plain push under this environment runs the
            # planted program in place of git's own
            subprocess.run(["git", "-C", self.repo, "push", self.remote,
                            "%s:refs/heads/premise" % head],
                           capture_output=True, timeout=60)
            with open(ran, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "git-receive-pack\n")
            os.remove(ran)
            pushed, detail = self.ops.push(self.repo, head, target,
                                           lease=base)
        self.assertTrue(pushed, detail)
        self.assertEqual(_git(self.remote, "rev-parse", "main"), head)
        self.assertFalse(os.path.exists(ran), "a planted program ran")

    def test_a_dot_segment_in_the_trunk_url_refuses_the_push(self):
        # git collapses `a/../b` on the wire, while the privacy check reads
        # the URL's literal first segments: `private/../public.git` would be
        # judged by `private` and pushed to `public`
        dotted = os.path.join(self.tmp, "x", "..", "remote.git")
        _git(self.repo, "config", "helm.trunkUrl", dotted)
        _git(self.repo, "config", "remote.origin.url", dotted)
        with self.visibility(hostpath_guard.PRIVATE):
            ok, why = self.ops.push_guard(self.repo)
        self.assertFalse(ok, why)
        self.assertIn("segment", why)


class RealSeams(Base):
    """The seams that read a repository or a ledger, run for real on a temp
    repository and an empty helm home."""

    def test_a_car_touching_the_hooks_owes_a_restart_by_its_real_diff(self):
        base = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "-b", "lane-h")
        os.makedirs(os.path.join(self.repo, "helm"))
        with open(os.path.join(self.repo, "helm", "hooks.py"), "w") as fh:
            fh.write("X = 1\n")
        _git(self.repo, "add", "helm/hooks.py")
        _git(self.repo, "commit", "-q", "-m", "hooks")
        tip = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "main")
        changes, why = autoland.Ops().changes(self.root, base, tip)
        self.assertIsNone(why, why)
        self.assertEqual(sorted(changes), ["helm/hooks.py"])
        owed = autoland.needs_restart(changes)
        self.assertEqual(len(owed), 1, owed)
        self.assertIn("hooks", owed[0])

    def test_the_real_facts_of_an_unknown_car_never_raise(self):
        car = _car(ROW1, "one", TIP1)
        facts = autoland.Ops().car_facts(self.root, car)
        self.assertEqual(sorted(facts), ["admit", "author", "doors",
                                         "family", "findings_unread",
                                         "hold_row", "model", "priority",
                                         "read_at", "reader", "task",
                                         "task_unknown", "title"])
        self.assertEqual(facts["reader"], "reader-seat")
        # its findings could not be read, and the LAND says so (task/3862)
        self.assertEqual(facts["findings_unread"],
                         "the compose could not read them: row %s is not in "
                         "the dispatch ledger" % ROW1[:12])
        # a source-clean car whose doors could not be read is judged as a
        # door: its admission is asked, never assumed
        self.assertIsNone(facts["doors"])
        self.assertIsNone(facts["hold_row"])
        self.assertFalse(facts["admit"][0])
        self.assertIn("no proven approval-tier holder at the hold",
                      facts["admit"][1])

    def test_a_land_first_car_rides_only_when_its_lane_is_no_door(self):
        """The plan asks the doors; the admission asks them again, so a
        land-first car whose doors read as a door or not at all is barred."""
        car = _car(ROW1, "one", TIP1, basis=landwindow.LAND_FIRST)
        facts = autoland.Ops().car_facts(self.root, car)
        self.assertIsNone(facts["doors"])
        self.assertEqual(facts["admit"], (False, (
            "unread and its doors could not be read, and only a lane with "
            "no door lands before review")))
        rows = {ROW1: {"id": ROW1, "chain_root": ROW1, "lane": "one",
                       "repo_id": self.repo}}
        for found, admit in (([("prod", "planted")], (False, (
                "unread and a DOOR (prod), and only a lane with no door "
                "lands before review"))), ([], (True, None))):
            with self.subTest(doors=found), \
                    mock.patch.object(dispatches, "snapshot",
                                      return_value=(rows, None)), \
                    mock.patch("helm.review_door.lane_doors",
                               lambda row, current=None, found=found: {
                                   "doors": found, "paths": []}):
                facts = autoland.Ops().car_facts(self.root, car)
            self.assertEqual(facts["admit"], admit)

    def test_each_car_uses_its_own_proof_or_says_task_unknown(self):
        first = tasks.add("first ask", "builder-seat", force_new=True)[0]["id"]
        other = tasks.add("other ask", "builder-seat", force_new=True)[0]["id"]
        fallback = tasks.add("numbered ask", "builder-seat", tid="3991",
                             force_new=True)[0]["id"]
        for lane, tid in (("one", first), ("two", other),
                          ("three", other)):
            _git(self.repo, "branch", "lane/" + lane)
            self.assertEqual(taskkey.record_lane(self.repo, lane, tid),
                             (True, None))
        plain = "e4" * 8
        rows = {ROW1: {"id": ROW1, "chain_root": ROW1, "lane": "one",
                       "task": first, "repo_id": self.repo},
                ROW2: {"id": ROW2, "chain_root": ROW1, "lane": "two",
                       "repo_id": self.repo},
                ROW3: {"id": ROW3, "chain_root": ROW3, "lane": "three",
                       "task": other, "repo_id": self.repo},
                plain: {"id": plain, "chain_root": plain,
                        "lane": "plain-3991", "repo_id": self.repo}}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(rows, None)):
            cars = [_car(rid, lane, TIP1, basis="approved")
                    for rid, lane in ((ROW1, "one"), (ROW2, "two"),
                                      (ROW3, "three"),
                                      (plain, "plain-3991"))]
            facts = [autoland.Ops().car_facts(self.root, car)
                     for car in cars]
            admitted, barred = autoland._Tick(
                self.root, False, autoland.Ops(), io.StringIO()).admitted(cars)
        self.assertEqual(barred, [])
        self.assertEqual([(c["task"], c["task_unknown"])
                          for c in admitted],
                         [(f["task"], f["task_unknown"]) for f in facts])
        self.assertIn("task UNKNOWN", autoland._merge_detail(admitted[1]))
        self.assertEqual(facts[0]["task"], first)
        self.assertIsNone(facts[1]["task"])
        self.assertIn(first, facts[1]["task_unknown"])
        self.assertIn(other, facts[1]["task_unknown"])
        self.assertEqual(facts[2]["task"], other)
        self.assertEqual(facts[3]["task"], fallback)
        self.assertIsNone(facts[3]["task_unknown"])

    def test_a_disputed_cars_land_leaves_its_chain_findings_and_rows_open(self):
        from helm import review_findings
        first = tasks.add("first ask", "builder-seat", force_new=True)[0]["id"]
        other = tasks.add("other ask", "builder-seat", force_new=True)[0]["id"]
        _git(self.repo, "branch", "lane/two")
        self.assertEqual(taskkey.record_lane(self.repo, "two", other),
                         (True, None))
        rows = {ROW1: {"id": ROW1, "chain_root": ROW1, "lane": "one",
                       "task": first, "repo_id": self.repo},
                ROW2: {"id": ROW2, "chain_root": ROW1, "lane": "two",
                       "repo_id": self.repo},
                ROW3: {"id": ROW3, "chain_root": ROW1, "lane": "one",
                       "repo_id": self.repo, "status": "open", "kind": "review"}}
        ops = autoland.Ops()
        car = _car(ROW2, "two", TIP1, basis="approved")
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(rows, None)), \
                mock.patch.object(review_findings, "named_at",
                                  return_value=([other], None)), \
                mock.patch.object(review_findings, "lane_tasks",
                                  return_value=(set(), None)):
            facts = ops.car_facts(self.root, car)
        self.assertIsNone(facts["task"])
        self.assertIn("proves", facts["task_unknown"])
        close_findings = mock.Mock(return_value=([other], []))
        close_row = mock.Mock(return_value=None)
        ops.close_findings = close_findings
        ops.lr_row_close = close_row
        report = ops.land_step(self.root, dict(car, **facts), "LAND 1", TIP1)
        close_findings.assert_not_called()
        close_row.assert_not_called()
        self.assertEqual(report["findings_closed"], [])
        self.assertEqual(report["discharged"], [])
        self.assertIn("UNKNOWN", " ".join(report["findings_errors"] +
                                            report["errors"]))

    def test_the_real_facts_carry_the_task_title_as_plain_words(self):
        from helm import tasks, trainblame
        with mock.patch.object(trainblame, "lane_task",
                               lambda rid: ("task/9", None, None)), \
                mock.patch.object(tasks, "get", lambda tid: {
                    "title": "the  fleet\ngot nine", "priority": "P2"}):
            facts = autoland.Ops().car_facts(self.root,
                                             _car(ROW1, "one", TIP1))
        self.assertEqual((facts["task"], facts["title"], facts["priority"]),
                         ("task/9", "the fleet got nine", "P2"))

    def test_a_task_read_that_is_refused_or_raises_is_unknown_never_no_task(self):
        """R3 (task/3643): a refused or failed task read is said on the merge
        line as task UNKNOWN with its reason, never as "no task", so the
        sweep cannot mistake it for a land that served none."""
        from helm import trainblame
        for lane_task, said in (
                (lambda rid: (None, None, "the dispatch ledger could not be "
                              "read (locked)"), "could not be read"),
                (mock.Mock(side_effect=OSError("boom")), "OSError")):
            with self.subTest(said=said), \
                    mock.patch.object(trainblame, "lane_task", lane_task):
                facts = autoland.Ops().car_facts(self.root,
                                                 _car(ROW1, "one", TIP1))
            self.assertIsNone(facts["task"])
            self.assertIn(said, facts["task_unknown"])
            detail = autoland._merge_detail(dict(
                _car(ROW1, "one", TIP1), task=None,
                task_unknown=facts["task_unknown"], doors=[], author="a",
                reader="b"))
            self.assertTrue(detail.startswith("task UNKNOWN"), detail)
            self.assertNotIn("no task", detail)

    def test_a_newer_proven_car_task_overrules_a_transient_blame_refusal(self):
        from helm import trainblame
        mine = tasks.add("the same lane task", "builder-seat",
                         force_new=True)[0]["id"]
        _git(self.repo, "branch", "lane/one")
        self.assertEqual(taskkey.record_lane(self.repo, "one", mine),
                         (True, None))
        rows = {ROW1: {"id": ROW1, "chain_root": ROW1, "lane": "one",
                       "task": mine, "repo_id": self.repo}}
        with mock.patch.object(trainblame, "lane_task", return_value=(
                None, "the first read was refused", "the first read was refused")), \
                mock.patch.object(dispatches, "snapshot",
                                  return_value=(rows, None)):
            facts = autoland.Ops().car_facts(
                self.root, _car(ROW1, "one", TIP1, basis="approved"))
        self.assertEqual(facts["task"], mine)
        self.assertIsNone(facts["task_unknown"])
        self.assertEqual(facts["chain"], ROW1)
        self.assertNotIn("step_unread", facts)

    def test_push_readiness_and_resumed_land_cannot_use_an_old_task(self):
        from helm import review_findings
        first = tasks.add("chain ask", "builder-seat", force_new=True)[0]["id"]
        other = tasks.add("different lane ask", "builder-seat",
                          force_new=True)[0]["id"]
        for lane in ("one", "two"):
            _git(self.repo, "branch", "lane/" + lane)
            self.assertEqual(taskkey.record_lane(self.repo, lane, first),
                             (True, None))
        before = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "child tip")
        tip = _git(self.repo, "rev-parse", "HEAD")
        fix, sibling = "f1" * 8, "e1" * 8
        rows = {ROW1: {"id": ROW1, "chain_root": ROW1, "lane": "one",
                       "task": first, "repo_id": self.repo},
                ROW2: {"id": ROW2, "chain_root": ROW1, "lane": "two",
                       "repo_id": self.repo, "status": "held", "tip": tip},
                fix: {"id": fix, "chain_root": ROW1, "lane": "one",
                      "polarity": "fix", "reviewed_tip": before,
                      "status": "verdict"},
                sibling: {"id": sibling, "chain_root": ROW1,
                          "lane": "two", "status": "open"}}
        finding = tasks.add("finding of the chain", "builder-seat",
                            force_new=True, found_in=fix,
                            found_chain=ROW1)[0]["id"]
        ops = self.ops

        def car_facts(root, car):
            return dict(autoland.Ops.car_facts(ops, root, car),
                        admit=(True, None))

        self.ops.car_facts = car_facts
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(rows, None)), \
                mock.patch.object(review_findings, "lane_tasks",
                                  return_value=(set(), None)):
            st = self.to_gating([_car(ROW2, "two", tip, basis="approved")])
            self.assertEqual((st["cars"][0]["task"], st["cars"][0]["chain"]),
                             (first, ROW1))
            _git(self.repo, "config", "branch.lane/two.helmTask", other)
            tick = autoland._Tick(self.root, True, self.ops, io.StringIO())
            self.assertIsNone(tick.ready_at_the_push(st, st["head"]))
            self.assertIsNone(tick._ready_cars[0]["task"])
            self.assertIn("proves", tick._ready_cars[0]["task_unknown"])
            # A killed tick resumes with only its saved state, not _ready_cars.
            saved = self.current()
            self.assertIsNone(saved["cars"][0]["task"])
            self.assertIn("proves", saved["cars"][0]["task_unknown"])
            # Also model an older sent record from before the refresh: the
            # post-push process must not trust even that saved compose proof.
            saved["cars"][0].update(task=first, task_unknown=None, chain=ROW1,
                                    findings_unread=None, step_unread=None)
            saved["land"] = 384
            saved["fold"] = {"closed": [], "foreign": []}
            self.assertIsNone(autoland._Tick(
                self.root, True, self.ops, io.StringIO()).closes(saved))
        self.assertIsNone(saved["cars"][0]["task"])
        self.assertIn("proves", saved["cars"][0]["task_unknown"])
        self.assertEqual(tasks.rows()[finding]["status"], "open")
        self.assertEqual(self.ops.row_closes, [])

    def test_a_later_proof_never_forgets_earlier_protected_findings(self):
        merged = autoland._Tick._car_proof(
            {"task_unknown": None, "tasks_named": ["task/1"],
             "findings_kept": ["task/2"]},
            {"task_unknown": None, "tasks_named": ["task/3"],
             "findings_kept": ["task/4"]})
        self.assertEqual(merged["tasks_named"], ["task/1", "task/3"])
        self.assertEqual(merged["findings_kept"], ["task/2", "task/4"])

    def test_the_webs_code_is_read_off_its_stop_facts_and_trunks_tree(self):
        """The console web writes the digest of the code it imported into
        every stop-facts snapshot; trunk's is the same digest of the landed
        tree. Both are a file read and a stat walk (task/3796)."""
        from helm import stopfacts
        pkg = os.path.join(self.root, "helm")
        os.makedirs(pkg)
        source = os.path.join(pkg, "a.py")
        with open(source, "w") as fh:
            fh.write("x = 1\n")
        digest = stopfacts.code_policy(pkg=pkg, fresh=True)
        loaded = time.time() - 100
        snap = {"schema": stopfacts.SCHEMA, "written_at": time.time(),
                "policy": digest,
                "resident": {"pid": os.getpid(),
                             "starttime": stopfacts.own_starttime(),
                             "started_at": loaded, "replaying_since": None,
                             "code_root": os.path.realpath(pkg)}}
        os.makedirs(os.path.dirname(stopfacts.path()), exist_ok=True)
        with open(stopfacts.path(), "w") as fh:
            json.dump(snap, fh)
        got = autoland.Ops().web_code(self.root)
        self.assertEqual((got["served"], got["trunk"], got["alive"],
                          got["why"]), (digest, digest, True, None))
        self.assertIsNone(autoland.web_follows(got, loaded + 10)[0])
        # the land changes the tree after the resident loaded it, and it
        # did not follow
        with open(source, "a") as fh:
            fh.write("y = 2\n")
        got = autoland.Ops().web_code(self.root)
        self.assertNotEqual(got["trunk"], digest)
        stale, _note = autoland.web_follows(got, loaded + 10)
        self.assertIn(digest[:12], stale)
        self.assertIn(got["trunk"][:12], stale)
        # no snapshot: unknown, never stale
        os.remove(stopfacts.path())
        got = autoland.Ops().web_code(self.root)
        self.assertTrue(got["why"], got)
        self.assertIsNone(autoland.web_follows(got, loaded + 10)[0])

    def test_the_real_lease_seams_release_a_landed_lane_whoever_holds_it(
            self):
        """The claims ledger read, the stop guard's own landedness read and
        `helm work release` in a child, on a temp repository and an empty
        helm home (task/3674)."""
        from helm import seats_claims
        _git(self.repo, "checkout", "-q", "-b", "lane/one")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "one")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "-q", "--ff-only", "lane/one")
        _git(self.repo, "checkout", "-q", "-b", "lane/two")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "two")
        _git(self.repo, "checkout", "-q", "main")
        leases = {}
        for lane, holder in (("one", "builder-a"), ("two", "builder-b")):
            ok, said, lease = seats_claims.claim(
                _lanes_resource(self.root, lane), holder,
                session="session-of-" + holder)
            self.assertTrue(ok, said)
            leases[lane] = lease
        ops = autoland.Ops()
        row, why = ops.lane_lease(self.root, "one")
        self.assertIsNone(why, why)
        self.assertEqual((row["holder"], row["lease"]),
                         ("builder-a", leases["one"]))
        self.assertIsInstance(row["fence"], int)
        self.assertIn(ops.lane_landed(self.root, "one")[0],
                      (vcs.ANCESTOR, vcs.PATCH_EQUIVALENT))
        state, proof = ops.lane_landed(self.root, "two")
        self.assertEqual(state, vcs.NOT_ANCESTOR, proof)
        self.assertEqual(ops.lane_landed(self.root, "three")[0], vcs.UNKNOWN)
        # the tick's own session is never the holder's, and the child is
        # not refused for it
        with mock.patch.dict(os.environ, {
                "CLAUDE_CODE_SESSION_ID": "the-seat-running-the-tick",
                "HELM_LANE_COORDINATION": "1"}):
            ok, text = ops.release_lease(self.root, "one", "builder-a",
                                         leases["one"])
            self.assertTrue(ok, text)
            self.assertEqual(ops.lane_lease(self.root, "one"), (None, None))
            self.assertEqual(ops.lane_lease(self.root, "two")[0]["holder"],
                             "builder-b")
            # the control: a lease the land did not read stays held
            ok, text = ops.release_lease(self.root, "two", "builder-b",
                                         "0" * 16)
            self.assertFalse(ok, text)
            self.assertIn("stays held", text)
            self.assertEqual(ops.lane_lease(self.root, "two")[0]["holder"],
                             "builder-b")


def _lanes_resource(root, lane):
    from helm.work import _lanes
    return _lanes.resource(root, lane)


class LockArms(Base):
    def test_a_manual_train_in_flight_keeps_auto_land_idle(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.ops.flying = [("/x/helm-train9", "train9: merge lane z")]
        rc, out = self.tick()
        self.assertEqual(self.ops.posts, [])
        self.assertIsNone(self.current())
        self.assertIn("train9", out)

    def test_a_manual_train_in_flight_keeps_auto_land_from_composing(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        self.ops.flying = [("/x/helm-train9", "train9: merge lane z")]
        self.ops.clock += 301
        self.tick()
        self.assertNotIn("compose", self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.INTENT)
        self.ops.flying = []
        self.tick()
        self.assertIn("compose", self.ops.calls)

    def test_a_hand_compose_holding_the_lock_keeps_auto_land_waiting(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        self.ops.clock += 301
        with autoland.compose_lock(self.root, 0) as held:
            self.assertTrue(held)
            rc, out = self.tick()
        self.assertNotIn("compose", self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.INTENT)
        self.assertIn("compose lock", out)
        self.tick()
        self.assertIn("compose", self.ops.calls)

    def test_pause_stops_every_tick(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        autoland.pause(self.root, "the-integrator", "hands on")
        rc, out = self.tick()
        self.assertEqual(self.ops.posts, [])
        self.assertEqual(self.ops.calls, [])
        self.assertIn("paused", out)
        autoland.resume(self.root, "the-integrator")
        self.tick()
        self.assertEqual(len(self.ops.posts), 1)

    def test_a_manual_helm_train_apply_refuses_while_auto_land_flies(self):
        self.to_gating()
        out = io.StringIO()
        rc = landwindow.compose(self.repo, apply=True,
                                project=lambda: ({}, None), out=out)
        self.assertEqual(rc, 1)
        self.assertIn("auto-land", out.getvalue())
        self.assertIn("train7", out.getvalue())
        # the control: the same compose, with no auto-land train in flight,
        # does not name auto-land
        autoland.abandon(self.root, "the-integrator", "clear it",
                         ops=self.ops)
        out = io.StringIO()
        landwindow.compose(self.repo, apply=True, project=lambda: ({}, None),
                           out=out)
        self.assertNotIn("auto-land", out.getvalue())

    def test_the_real_flight_census_reads_a_train_room_off_trunk(self):
        _git(self.repo, "checkout", "-q", "-b", "lane-x")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "work")
        _git(self.repo, "checkout", "-q", "main")
        room = os.path.join(self.tmp, "helm-train9")
        _git(self.repo, "worktree", "add", "-q", "--detach", room, "main")
        _git(room, "merge", "-q", "--no-ff", "lane-x", "-m",
             "train9: merge lane lane-x")
        _git(self.repo, "config", "helm.trunkRef", "refs/heads/main")
        flying = autoland.Ops().in_flight(self.root, ())
        self.assertEqual([os.path.realpath(p) for p, _s in flying],
                         [os.path.realpath(room)])
        # once trunk carries the train, it is no longer in flight
        _git(self.repo, "merge", "-q", "--ff-only",
             _git(room, "rev-parse", "HEAD"))
        self.assertEqual(autoland.Ops().in_flight(self.root, ()), [])


class DryRun(Base):
    def test_without_apply_nothing_is_written_or_posted(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        rc, out = self.tick(apply=False)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ops.posts, [])
        self.assertEqual(self.state_files(), [])
        self.assertIn("would", out)

    def test_without_apply_a_green_train_is_not_pushed(self):
        st = self.to_gating()
        self.green(st)
        path = autoland.train_path(self.root, "train7")
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
        posts = len(self.ops.posts)
        rc, out = self.tick(apply=False)
        self.assertNotIn("push", self.ops.calls)
        self.assertNotIn("verify", self.ops.calls)
        self.assertEqual(len(self.ops.posts), posts)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        self.assertIn("would", out)


class Counter(Base):
    def test_the_counter_takes_the_next_number_once(self):
        autoland.seed_counter(self.root, 383, TRUNK)
        n, why = autoland.take_land_number(self.root, "5" * 40, TRUNK,
                                           self.ops)
        self.assertEqual((n, why), (384, None))
        # a retry for the same head is the same number, never 385
        n, why = autoland.take_land_number(self.root, "5" * 40, TRUNK,
                                           self.ops)
        self.assertEqual((n, why), (384, None))

    def test_a_counter_not_on_trunk_refuses(self):
        autoland.seed_counter(self.root, 383, TRUNK)
        self.ops.ancestry = lambda root, a, b: vcs.NOT_ANCESTOR
        n, why = autoland.take_land_number(self.root, "5" * 40, TRUNK,
                                           self.ops)
        self.assertIsNone(n)
        self.assertIn("not an ancestor", why)

    def test_an_unseeded_counter_refuses_by_name(self):
        os.remove(autoland.counter_path(self.root))
        n, why = autoland.take_land_number(self.root, "5" * 40, TRUNK,
                                           self.ops)
        self.assertIsNone(n)
        self.assertIn("seed", why)

    def test_the_seed_verb_writes_the_counter(self):
        head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "config", "helm.trunkRef", "refs/heads/main")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = autoland.cmd(["seed", "383", head, "--repo", self.repo])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(autoland.read_counter(self.root)["n"], 383)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(autoland.cmd(["seed", "x", head, "--repo",
                                           self.repo]), 2)

    def _seed(self, n, sha):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = autoland.cmd(["seed", str(n), sha, "--repo", self.repo])
        return rc, out.getvalue() + err.getvalue()

    def test_a_seed_of_an_older_land_is_logged_and_the_counter_stays(self):
        """A hand land the counter never took is RECORDED after the fact
        (the morning report's MISSING line names this verb), and recording
        it never walks the counter back: the next land is still one past
        the newest."""
        older = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "next")
        head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "config", "helm.trunkRef", "refs/heads/main")
        rc, out = self._seed(383, head)
        self.assertEqual(rc, 0, out)
        rc, out = self._seed(382, older)
        self.assertEqual(rc, 0, out)
        self.assertIn("LAND 384", out)
        counter = autoland.read_counter(self.root)
        self.assertEqual((counter["n"], counter["sha"]), (383, head))
        lands, why = autoland.land_log(self.root)
        self.assertIsNone(why, why)
        self.assertEqual({k: v["sha"] for k, v in lands.items()},
                         {382: older, 383: head})
        # an older land may not carry the counter's number or a later one
        rc, out = self._seed(383, older)
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertEqual(autoland.land_log(self.root)[0][383]["sha"], head)
        self.assertEqual(autoland.read_counter(self.root)["n"], 383)

    def test_the_counter_alone_is_the_last_land_until_the_log_has_it(self):
        """A counter seeded before the land log existed still names its
        land: the report's first morning reads it from the counter."""
        lands, why = autoland.land_log(self.root)
        self.assertIsNone(why, why)
        self.assertEqual({k: v["sha"] for k, v in lands.items()},
                         {383: TRUNK})


class LandAbsorbs(Base):
    """Two of the integrator's post-land hand steps, done by the land itself
    (task/3674, owner-asked): the web restart a land no longer
    owes, read instead of asked (task/3796), and each landed car's lane
    lease, released at land whoever holds it."""

    WEB = {"helm/web_board.py": "+x\n"}

    def land(self, cars=None):
        st = self.to_gating(cars or [_car(ROW1, "one", TIP1),
                                     _car(ROW2, "two", TIP2,
                                          basis="approved")])
        self.green(st)
        return self.tick()

    def land_line(self):
        lines = [p for p in self.ops.posts if "@all LAND 384" in p]
        self.assertEqual(len(lines), 1, self.ops.posts)
        return lines[0]

    def web(self, served, trunk, started_at, alive=True):
        pkg = os.path.join(self.root, "helm")
        self.ops.web = {"served": served, "trunk": trunk, "pkg": pkg,
                        "code_root": pkg, "pid": 4242, "alive": alive,
                        "started_at": started_at, "why": None}

    def stale_posts(self):
        return [p for p in self.ops.posts if "still serves" in p]

    def ended(self):
        return [d["state"] for d in self.archived()]

    # -- the web restart it no longer owes (task/3796) --------------------
    def test_a_web_only_land_posts_no_restart_and_says_none(self):  # noqa: VACUOUS_ASSERTION — the absence of a restart post is the contract; land_line() asserts the one LAND post exists
        self.ops.changes_by_tip = {TIP2: dict(self.WEB)}
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ops.closed_rows, [(ROW2, True, None)])
        self.assertFalse(any("restart" in p for p in self.ops.posts),
                         self.ops.posts)
        self.assertNotIn("needs restart", self.land_line())

    def test_a_land_on_a_service_that_does_not_follow_its_code_still_posts(
            self):
        self.ops.changes_by_tip = {TIP2: dict(self.WEB, **{
            "helm/chatnode.py": "+x\n"})}
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        (rid, live, restart), = self.ops.closed_rows
        self.assertEqual((rid, live), (ROW2, False))
        self.assertIn("chat node", restart)
        self.assertNotIn("web", restart)
        self.assertTrue(any(p.startswith("@integrator") and "chat node" in p
                            and "needs a restart" in p
                            for p in self.ops.posts), self.ops.posts)
        land = self.land_line()
        self.assertIn("needs restart", land)
        self.assertIn("chat node", land)
        self.assertNotIn("web service", land)

    def test_a_web_still_on_old_code_a_minute_after_the_land_is_posted_once(  # noqa: VACUOUS_ASSERTION — the read and the archive absent before the minute are re-read present after it
            self):
        """The web re-execs itself within seconds of a changed tree, so it
        is read once WEB_FOLLOW_S after the fast-forward, never before;
        still on the code from before the land, it is ONE line naming both
        digests, and nothing is restarted."""
        self.ops.changes_by_tip = {TIP2: dict(self.WEB)}
        served, trunk = "0" * 32, "f" * 32
        self.web(served, trunk, started_at=T0 - 3600)
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("web_code", self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.LANDING)
        self.assertEqual(self.ended(), [])
        # a tick inside the minute still does not read it
        self.ops.clock += autoland.WEB_FOLLOW_S - 1
        self.tick()
        self.assertNotIn("web_code", self.ops.calls)
        self.ops.clock += 1
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ops.calls.count("web_code"), 1)
        stale, = self.stale_posts()
        self.assertTrue(stale.startswith("@integrator"), stale)
        self.assertIn("LAND 384", stale)
        self.assertIn(served[:12], stale)
        self.assertIn(trunk[:12], stale)
        self.assertIn("restarts nothing", stale)
        self.assertEqual(self.ended(), [autoland.DONE])
        self.tick()
        self.assertEqual(len(self.stale_posts()), 1, self.ops.posts)

    def test_a_web_serving_trunks_code_is_not_posted(self):  # noqa: VACUOUS_ASSERTION — no stale post is the contract; the read is counted once and its note asserted
        self.ops.changes_by_tip = {TIP2: dict(self.WEB)}
        self.web("f" * 32, "f" * 32, started_at=T0 - 3600)
        self.land()
        self.ops.clock += autoland.WEB_FOLLOW_S
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ops.calls.count("web_code"), 1)
        self.assertEqual(self.stale_posts(), [])
        self.assertEqual(self.ended(), [autoland.DONE])
        notes = [h["note"] for h in self.archived()[0]["history"]]
        self.assertTrue(any("serves trunk's code" in n for n in notes), notes)

    def test_a_web_that_re_execd_after_the_land_follows_while_it_refolds(  # noqa: VACUOUS_ASSERTION — no stale post is the contract; the control reads the same facts stale
            self):
        """A resident that re-exec'd keeps the facts it found, and their
        digest, until its first refresh: a start after the land's
        fast-forward is the re-exec, not old code."""
        self.ops.changes_by_tip = {TIP2: dict(self.WEB)}
        self.web("0" * 32, "f" * 32, started_at=None)
        self.land()
        self.ops.web["started_at"] = self.ops.clock + 5
        self.ops.clock += autoland.WEB_FOLLOW_S
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.stale_posts(), [])
        self.assertEqual(self.ended(), [autoland.DONE])
        # the control: the same reading with the start before the land
        # is old code
        got = dict(self.ops.web, started_at=T0)
        stale, _note = autoland.web_follows(got, T0 + 301)
        self.assertIn("0" * 12, stale)

    def test_a_web_that_cannot_be_read_is_recorded_and_not_posted(self):  # noqa: VACUOUS_ASSERTION — no post is the contract; the UNKNOWN note is asserted in the archived history
        self.ops.changes_by_tip = {TIP2: dict(self.WEB)}
        self.land()
        self.ops.clock += autoland.WEB_FOLLOW_S
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.stale_posts(), [])
        self.assertEqual(self.ended(), [autoland.DONE])
        notes = [h["note"] for h in self.archived()[0]["history"]]
        self.assertTrue(any("UNKNOWN" in n and "stop-facts" in n
                            for n in notes), notes)
        # a dead resident and another tree's resident are not this land's
        for got in (dict(self.ops.web or {}, why=None, served="0" * 32,
                         trunk="f" * 32, alive=False, started_at=T0,
                         code_root="/x/helm", pkg="/x/helm"),
                    dict(why=None, served="0" * 32, trunk="f" * 32,
                         alive=True, started_at=T0, code_root="/y/helm",
                         pkg="/x/helm")):
            stale, note = autoland.web_follows(got, T0 + 301)
            self.assertIsNone(stale, note)

    def test_a_land_that_touches_no_web_path_never_waits_on_the_web(self):  # noqa: VACUOUS_ASSERTION — no web read is the contract; the land is asserted DONE in the same tick
        self.web("0" * 32, "f" * 32, started_at=T0 - 3600)
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("web_code", self.ops.calls)
        self.assertEqual(self.ended(), [autoland.DONE])

    def test_the_read_waits_as_long_as_a_resident_may_take_to_follow(self):  # noqa: VACUOUS_ASSERTION — a comparison of two constants, no observable can be empty
        from helm import stopfacts_resident
        self.assertGreaterEqual(autoland.WEB_FOLLOW_S,
                                stopfacts_resident.REEXEC_WITHIN_S)

    # -- the lane lease, released at land (task/3674) ---------------------
    def hold(self, **lanes):
        for lane, (holder, fence) in lanes.items():
            self.ops.leases[lane] = {"holder": holder, "lease": "L%d" % fence,
                                     "session": "s-%s" % holder,
                                     "fence": fence}

    def test_a_landed_cars_lease_is_released_and_named_on_the_land_line(
            self):
        self.hold(one=("builder-a", 1), two=("builder-b", 2))
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ops.released, [("one", "builder-a", "L1"),
                                             ("two", "builder-b", "L2")])
        self.assertEqual(self.ops.leases, {})
        self.assertLess(self.ops.calls.index("lr_close"),
                        self.ops.calls.index("release_lease"))
        land = self.land_line()
        self.assertIn("leases released: one, two", land)
        # the lease token is never posted
        self.assertNotIn("L1", land.replace("LAND", ""))
        self.assertEqual(self.ended(), [autoland.DONE])

    def test_a_lease_on_a_lane_whose_tip_is_not_on_trunk_stays_held(self):
        self.hold(one=("builder-a", 1), two=("builder-b", 2))
        self.ops.landed = {"two": vcs.NOT_ANCESTOR}
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ops.released, [("one", "builder-a", "L1")])
        self.assertEqual(self.ops.leases["two"]["holder"], "builder-b")
        land = self.land_line()
        self.assertIn("leases released: one", land)
        self.assertNotIn("one, two", land)
        self.assertEqual(self.ended(), [autoland.DONE])
        car = [c for c in self.archived()[0]["cars"] if c["lane"] == "two"][0]
        self.assertEqual(car["lease"]["state"], "kept")

    def test_a_release_that_refuses_is_named_and_the_land_completes(self):
        self.hold(one=("builder-a", 1), two=("builder-b", 2))
        occupied = ("helm work: /x-wt/one is OCCUPIED — room and lease "
                    "kept. Each process below is live work:\n  pid 7 ...")
        self.ops.release_answers = {"one": (False, occupied)}
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        land = self.land_line()
        self.assertIn("lease release refused: lane one", land)
        self.assertIn("OCCUPIED", land)
        self.assertIn("live work. CL", land)
        self.assertNotIn("pid 7", land)
        self.assertIn("leases released: two", land)
        self.assertEqual(self.ops.leases["one"]["holder"], "builder-a")
        self.assertEqual(self.ended(), [autoland.DONE])
        self.assertEqual(autoland.read_counter(self.root)["n"], 384)

    def test_a_re_run_of_the_step_releases_nothing_twice(self):  # noqa: VACUOUS_ASSERTION — no second release is the contract; the released list is asserted whole before and after
        """A tick killed after a release and before its answer was
        recorded: the next one asks the ledger again, releases nothing
        the land already released, and never a lease granted since."""
        self.hold(one=("builder-a", 1), two=("builder-b", 2))
        self.ops.release_raises = {"one"}
        with self.assertRaises(RuntimeError):
            self.land()
        self.assertEqual(self.current()["step"], "numbered")
        self.assertEqual(self.ops.released, [("one", "builder-a", "L1")])
        # the lane is claimed again before the next tick
        self.hold(one=("builder-c", 9))
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ops.released, [("one", "builder-a", "L1"),
                                             ("two", "builder-b", "L2")])
        self.assertEqual(self.ops.leases["one"]["holder"], "builder-c")
        land = self.land_line()
        self.assertIn("leases released: two", land)
        self.assertNotIn("builder-c", land)
        self.assertEqual(self.ended(), [autoland.DONE])
        # and a step asked again once every answer is recorded asks nothing
        st = self.archived()[0]
        self.ops.calls = []
        tick = autoland._Tick(self.root, True, self.ops, io.StringIO())
        self.assertIsNone(tick.releases(st))
        self.assertNotIn("release_lease", self.ops.calls)
        self.assertNotIn("lane_lease", self.ops.calls)

    def test_a_car_whose_lane_holds_no_lease_releases_nothing(self):  # noqa: VACUOUS_ASSERTION — no release is the contract; land_line() asserts the one LAND post exists
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ops.released, [])
        self.assertNotIn("leases released", self.land_line())


class Classify(unittest.TestCase):
    def test_restart_is_decided_by_touched_paths(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed non-empty tuple of cases
        cases = (({"helm/hooks.py": "+x\n"}, "hooks"),
                 ({"helm/chatnode.py": "+x\n"}, "chat node"),
                 ({"helm/proxywatch.py": "+x\n"}, "proxywatch"),
                 ({"helm/seat_catalog.py": ' "sidecar": {\n+  "pin": 1\n'},
                  "sidecar"),
                 ({"helm/gc.py": "+OnUnitActiveSec=%(i)ss\n"}, "timer"))
        for changes, word in cases:
            got = autoland.needs_restart(changes)
            self.assertTrue(got and word in got[0], (changes, got))
        self.assertEqual(autoland.needs_restart(
            {"helm/pi.py": "+x = 1\n", "tests/test_pi.py": "+y\n"}), [])
        self.assertEqual(autoland.needs_restart(
            {"helm/seat_catalog.py": "+  window = 1\n"}), [])

    def test_lead_posture_owes_a_relaunch_of_the_leads(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed non-empty tuple of cases, each asserting the relaunch words; the empty answers are the controls
        """A running lead keeps the hooks, skills, WHO digest and settings it
        started with, so a land that changes them is not LIVE until the
        leads relaunch, and the owed line says so."""
        cases = ({"helm/hooks.py": "+x\n"},
                 {"bin/helm-hook": "+x\n"},
                 {"agents/claudecode/skills/build/SKILL.md": "+x\n"},
                 {"helm/inject/_entries.py": "+x\n"},
                 {"helm/whoami.py": "+x\n"},
                 {"helm/seat_catalog.py":
                  "@@ -1 +1 @@ def lead_lean_settings():\n+    x = 1\n"},
                 {"helm/seat_launch_assets.py":
                  "@@ -1 +1 @@ def _apply_lead_lean(s):\n+    x = 1\n"},
                 {"helm/launch.py":
                  "@@ -1 +1 @@ def _lead_lean_args(seat, role):\n+ x\n"})
        for changes in cases:
            with self.subTest(changes=sorted(changes)):
                got = "; ".join(autoland.needs_restart(changes))
                self.assertIn("relaunch the leads", got)
                self.assertIn("not LIVE until they do", got)
        # CONTROL: a seat_catalog change away from the lead settings, and a
        # plain module, owe no relaunch
        for changes in ({"helm/seat_catalog.py": "+  window = 1\n"},
                        {"helm/pi.py": "+x = 1\n"}):
            with self.subTest(changes=sorted(changes)):
                self.assertEqual(autoland.needs_restart(changes), [])

    def test_the_web_follows_its_code_and_owes_no_restart(self):
        """`helm web` re-execs itself onto a changed tree (task/3132), so a
        web path owes the fleet no restart (task/3796): it is read a minute
        after the land instead. A path beside it that does not follow its
        code still owes one."""
        web = {"helm/web_board.py": "+x\n", "helm/web_ui/x.js.part": "+y\n"}
        self.assertEqual(autoland.needs_restart(web), [])
        self.assertEqual(autoland.follows_code(web), ["the web board"])
        both = dict(web, **{"helm/proxywatch.py": "+x\n"})
        owed = autoland.needs_restart(both)
        self.assertEqual(len(owed), 1, owed)
        self.assertIn("proxywatch", owed[0])
        self.assertEqual(autoland.follows_code({"helm/pi.py": "+x\n"}), [])
        self.assertEqual(autoland.follows_code(
            {"tests/test_web_board.py": "+x\n"}), [])

    def test_the_merge_subject_leads_with_the_task_title(self):
        """Trunk's subject is where the morning report reads a land's plain
        words, so auto-land's own merge carries the task's title first;
        with no title the subject carries its labels only."""
        car = {"id": ROW1, "task": "task/9", "title": "the fleet got nine",
               "priority": "P2", "doors": [], "author": "a", "reader": "r",
               "model": "m", "basis": "source-clean"}
        self.assertEqual(autoland._merge_detail(car),
                         "task/9: the fleet got nine; P2, not a door")
        del car["title"]
        self.assertEqual(autoland._merge_detail(car),
                         "task/9, P2, not a door")

    def test_a_merge_subject_names_no_seat_and_no_model(self):  # noqa: VACUOUS_ASSERTION — each absence sits beside an exact assertEqual on the same detail, and the control pins the task words positively
        """task/4033: commit metadata is akapug's alone (the owner's canon), so
        a car's merge subject says the task, its title, the priority and the
        door, and never who built the lane, who read it or on which model.
        That provenance stays in the ledger: the row, its verdict or hold,
        and the AUTHORS line a landed close prints."""
        seats, model = ("seat-a", "seat-b"), "claude-opus-5-5"
        for basis, doors, title, said in (
                ("source-clean", [], "the fleet got nine",
                 "task/9: the fleet got nine; P1, not a door"),
                ("approved", ["guard"], None, "task/9, P1, a DOOR: guard"),
                ("source-clean", None, None, "task/9, P1, doors UNKNOWN")):
            car = {"id": ROW1, "lane": "one", "task": "task/9",
                   "title": title, "priority": "P1", "doors": doors,
                   "author": seats[0], "reader": seats[1], "model": model,
                   "basis": basis}
            with self.subTest(basis=basis, doors=doors, title=title):
                detail = autoland._merge_detail(car)
                self.assertEqual(detail, said)
                for name in seats + (model, "author", "read", ROW1[:12]):
                    self.assertNotIn(name, detail)
        # CONTROL: the car's own task words still ride, so the subject still
        # says what landed; only the provenance left it.
        self.assertEqual(autoland._merge_detail(
            {"id": ROW1, "lane": "one", "task": None, "doors": [],
             "author": seats[0], "reader": seats[1], "model": model}),
            "no task, P?, not a door")

    def test_one_diff_splits_by_path(self):
        text = ("diff --git a/helm/a.py b/helm/a.py\n@@ -1 +1 @@\n-x\n+y\n"
                "diff --git a/tests/test_a.py b/tests/test_a.py\n+z\n")
        got = autoland.split_diff(text)
        self.assertEqual(sorted(got), ["helm/a.py", "tests/test_a.py"])
        self.assertIn("+y", got["helm/a.py"])
        self.assertNotIn("+z", got["helm/a.py"])

    def test_test_methods_are_counted_off_class_bodies(self):
        text = ("import unittest\n"
                "class A(unittest.TestCase):\n"
                "    def test_one(self): pass\n"
                "    def helper(self): pass\n"
                "    async def test_two(self): pass\n"
                "def test_free(): pass\n"
                "class B:\n"
                "    class C:\n"
                "        def test_three(self): pass\n")
        self.assertEqual(autoland.count_test_methods(text), 3)
        self.assertIsNone(autoland.count_test_methods("def (:"))


class Surface(Base):
    def test_unknown_args_refuse(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(autoland.cmd(["--bogus"]), 2)
            self.assertEqual(landwindow.cmd_train(["auto", "--bogus"]), 2)
            self.assertEqual(landwindow.cmd_train(["veto"]), 2)

    def test_status_prints_the_machine(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = autoland.cmd(["--status", "--repo", self.repo])
        self.assertEqual(rc, 0)
        self.assertIn("train7", out.getvalue())
        self.assertIn("INTENT", out.getvalue())

    def test_the_timer_install_has_an_off_switch(self):
        with mock.patch.dict(os.environ, {autoland.TIMER_ENV: "0"}):
            ok, detail = autoland.ensure_timer()
        self.assertIsNone(ok)
        self.assertIn(autoland.TIMER_ENV, detail)

    def test_the_timer_runs_every_two_minutes_through_the_shared_installer(
            self):
        units = os.path.join(self.tmp, "units")
        seen = []

        class Proc:
            returncode, stdout, stderr = 0, "", ""

        fake = mock.Mock()
        fake.run = lambda cmd, **kw: seen.append(cmd) or Proc()
        fake.TimeoutExpired = subprocess.TimeoutExpired
        with mock.patch.dict(os.environ, {"HELM_USER_UNIT_DIR": units}), \
                mock.patch.object(autoland, "subprocess", fake), \
                mock.patch("shutil.which", lambda name: "/bin/" + name):
            os.environ.pop(autoland.TIMER_ENV, None)
            ok, detail = autoland.ensure_timer()
        self.assertTrue(ok, detail)
        with open(os.path.join(units, autoland.TIMER_NAME)) as fh:
            self.assertIn("OnUnitActiveSec=120s", fh.read())
        with open(os.path.join(units, autoland.SERVICE_NAME)) as fh:
            self.assertIn("train auto --apply", fh.read())
        self.assertIn(["/bin/systemctl", "--user", "enable", "--now",
                       autoland.TIMER_NAME], seen)


class RaceArms(Base):
    """A verb another process runs WHILE a tick is running: the tick read the
    store before the verb wrote it, and must not act past it or write over
    it. Each arm runs the verb from inside a seam the tick calls, which is
    exactly where a second process's write lands."""

    def test_an_abandon_while_the_land_is_checked_is_never_pushed_past(self):
        st = self.to_gating()
        self.green(st)
        root, ops, real = self.root, self.ops, self.ops.foldcheck
        said = []

        def fold_then_abandon(where, head, gid, target=None):
            said.append(autoland.abandon(root, "the-integrator", "stop it",
                                         ops=ops))
            return real(where, head, gid, target)

        self.ops.foldcheck = fold_then_abandon
        self.tick()
        self.assertIsNone(said[0][1], said)
        self.assertEqual(said[0][0]["state"], autoland.ABANDONED)
        # THE ABANDON WAS ACKNOWLEDGED, so nothing is pushed after it and the
        # train does not come back.
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertIsNone(self.current())
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.ABANDONED])

    def test_an_abandon_while_the_audits_run_does_not_come_back(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        self.ops.clock += 301
        root, ops, real = self.root, self.ops, self.ops.audits

        def audits_then_abandon(where, room, trunk):
            autoland.abandon(root, "the-integrator", "not this one", ops=ops)
            return real(where, room, trunk)

        self.ops.audits = audits_then_abandon
        self.tick()
        self.assertNotIn("launch", self.ops.calls)
        self.assertIsNone(self.current())
        # the control: the next tick plans afresh; the abandoned train stays
        # ended
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.ABANDONED])

    def test_a_pause_taken_while_a_tick_runs_survives_the_tick(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        self.ops.clock += 301
        root = self.root

        def audits_cannot_run_and_the_integrator_pauses(where, room, trunk):
            autoland.pause(root, "the-integrator", "hands off")
            return None, "fab could not run", None

        self.ops.audits = audits_cannot_run_and_the_integrator_pauses
        self.tick()
        control, why = autoland.read_control(self.root)
        self.assertIsNone(why, why)
        self.assertIsNotNone(control["paused"], control)
        self.assertEqual(control["paused"]["reason"], "hands off")
        # the refusal the tick posted is still remembered beside the pause
        self.assertTrue(any(k.startswith("audits:") for k in
                            control["posted"]), control)
        self.ops.calls = []
        _rc, out = self.tick()
        self.assertIn("paused", out)
        self.assertEqual(self.ops.calls, [])

    def test_a_pause_taken_before_the_push_withholds_it(self):
        st = self.to_gating()
        self.green(st)
        root, real = self.root, self.ops.foldcheck

        def fold_then_pause(where, head, gid, target=None):
            autoland.pause(root, "the-integrator", "wait")
            return real(where, head, gid, target)

        self.ops.foldcheck = fold_then_pause
        self.tick()
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.LANDING)
        # the control: once resumed, the same land pushes exactly once
        self.ops.foldcheck = real
        autoland.resume(self.root, "the-integrator")
        self.tick()
        self.assertEqual(self.ops.pushes, 1)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_an_abandon_after_a_push_is_refused_and_the_fold_still_runs(self):
        autoland.seed_counter(self.root, 383, TRUNK)
        st = self.to_gating()
        self.green(st)
        self.ops.ff_raises = 1
        with self.assertRaises(RuntimeError):
            self.tick()
        self.assertEqual(self.ops.pushes, 1)
        row, why = autoland.abandon(self.root, "the-integrator", "oops",
                                    ops=self.ops)
        self.assertIsNone(row)
        self.assertIn("push", why)
        self.assertEqual(self.archived(), [])
        # the control: the next tick finishes the land it pushed
        self.ops.calls = []
        self.tick()
        self.assertIn("fold_apply", self.ops.calls)
        self.assertEqual(self.ops.pushes, 1)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_an_abandon_of_an_unpushed_landing_waits_for_no_tick(self):  # noqa: VACUOUS_ASSERTION — after the injected crash, the no-tick control abandons the same train and a later tick proves zero pushes
        """A tick killed at `pushing` BEFORE its push: nothing is on the
        remote, so the train may be abandoned, but never while a tick holds
        the tick lock (it may be pushing it)."""
        st = self.to_gating()
        self.green(st)

        def die_before_the_push(where, head, target, keep=(), lease=None):
            raise RuntimeError("killed before the push")

        self.ops.push = die_before_the_push
        with self.assertRaises(RuntimeError):
            self.tick()
        head = self.current()["head"]
        self.assertEqual(self.current()["step"], "pushing")
        self.ops.ancestry = lambda where, older, newer: (
            vcs.NOT_ANCESTOR if (older, newer) == (head, TRUNK)
            else vcs.ANCESTOR)
        with autoland._flock(autoland._lock_path(self.root, "tick"),
                             0) as held:
            self.assertTrue(held)
            row, why = autoland.abandon(self.root, "the-integrator", "no",
                                        ops=self.ops)
        self.assertIsNone(row)
        self.assertIn("a tick is landing it now", why)
        # the control: no tick running and nothing on the remote, it ends
        row, why = autoland.abandon(self.root, "the-integrator", "no",
                                    ops=self.ops)
        self.assertIsNone(why, why)
        self.assertEqual(row["state"], autoland.ABANDONED)
        self.tick()
        self.assertEqual(self.ops.pushes, 0)

    def test_a_crash_after_the_push_with_trunk_moved_on_still_folds(self):  # noqa: VACUOUS_ASSERTION — the train positively stops AMBIGUOUS naming the moved trunk after exactly one settle read, and the resumed control positively folds it and archives it DONE
        """A tick killed after its push applied, and another land went on
        top before the next tick. The push was sent and its answer never
        recorded, so it is AMBIGUOUS (the integrator's ruling on a dead
        tick's push): the next tick reads trunk once under the locks
        (`settle`), and a trunk that reads neither the head nor the gated
        trunk stops the train for a person, nothing pushed and nothing
        folded. The control: a person's `--resume` finds trunk carrying the
        head, and the fold still runs, with no push."""
        autoland.seed_counter(self.root, 383, TRUNK)
        st = self.to_gating()
        self.green(st)
        real_push = self.ops.push

        def push_then_die(where, head, target, keep=(), lease=None):
            real_push(where, head, target, keep, lease=lease)
            raise RuntimeError("killed after the push, before its save")

        self.ops.push = push_then_die
        with self.assertRaises(RuntimeError):
            self.tick()
        self.assertEqual(self.ops.pushes, 1)
        self.assertEqual(self.current()["step"], "pushing")
        head, later = self.current()["head"], "8" * 40
        # another land went on top of the pushed head before the next tick
        self.ops.remote = later
        self.ops.ancestry = lambda where, older, newer: (
            vcs.NOT_ANCESTOR if (older, newer) == (later, head)
            else vcs.ANCESTOR)
        self.ops.push = real_push
        self.ops.calls = []
        self.tick()
        self.assertEqual(self.ops.pushes, 1)
        self.assertEqual(self.ops.calls.count("settled_trunk"), 1,
                         self.ops.calls)
        self.assertNotIn("push", self.ops.calls)
        self.assertNotIn("fold_apply", self.ops.calls)
        st = self.current()
        self.assertEqual(st["state"], autoland.STOPPED, st)
        self.assertIn("AMBIGUOUS", st["stopped"]["why"])
        self.assertIn(later[:12], st["stopped"]["why"])
        # the control: a person's --resume, and the fold still runs
        _said, why = autoland.resume(self.root, "the-integrator",
                                     now=self.ops.clock)
        self.assertIsNone(why, why)
        self.ops.calls = []
        self.tick()
        self.assertEqual(self.ops.pushes, 1)
        self.assertNotIn("push", self.ops.calls)
        self.assertIn("fold_apply", self.ops.calls)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])



class ReadinessAtThePush(Base):
    """THE INTEGRATOR'S RULING R1: the last word before the push asks the
    plan again for the train's own cars — still READY at the EXACT tip, and
    admitted (the door tier). A car that is not (a FIX, a withdrawal, a new
    tip, a holder the tier no longer admits since the compose) stops the
    train at LANDING/verified, posted to the integrator naming the car and
    why. Nothing is pushed and no car is ejected automatically."""

    def landing_with(self, change):
        st = self.to_gating([_car(ROW1, "one", TIP1), _car(ROW2, "two", TIP2)])
        self.green(st)
        change()
        self.ops.calls = []
        rc, out = self.tick()
        return rc, out

    def assert_stopped_before_the_push(self, lane, row, word):
        st = self.current()
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertNotIn("push", self.ops.calls)
        self.assertIn("push_guard", self.ops.calls)
        self.assertEqual(st["state"], autoland.STOPPED, st)
        self.assertEqual((st["stopped"]["state"], st["stopped"]["step"]),
                         (autoland.LANDING, "verified"))
        # NEVER EJECTED AUTOMATICALLY: the train keeps both cars
        self.assertNotIn("blame", self.ops.calls)
        self.assertFalse(st.get("ejected"))
        self.assertEqual([c["id"] for c in st["cars"]], [ROW1, ROW2])
        stops = [p for p in self.ops.posts if "STOPPED" in p]
        self.assertEqual(len(stops), 1, self.ops.posts)
        self.assertTrue(stops[0].startswith("@integrator"), stops[0])
        self.assertIn("lane %s" % lane, stops[0])
        self.assertIn(row[:12], stops[0])
        self.assertIn(word, stops[0])

    def resume_and_land(self):
        self.ops.cars = [_car(ROW1, "one", TIP1), _car(ROW2, "two", TIP2)]
        self.ops.excluded = []
        _said, why = autoland.resume(self.root, "the-integrator",
                                     now=self.ops.clock)
        self.assertIsNone(why, why)
        self.assertEqual(self.current()["step"], "verified")
        self.ops.calls = []
        self.tick()
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        calls = self.ops.calls
        # the readiness is asked after the push guard and before the push
        self.assertIn("plan", calls[calls.index("push_guard"):
                                    calls.index("push")])
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_the_final_readiness_and_push_hold_the_dispatch_ledger_lock(self):
        st = self.to_gating()
        self.green(st)
        real = self.ops.plan
        contender = []

        def try_the_writer_lock():
            with eventledger.locked(dispatches.ledger_path(), timeout=0) as held:
                contender.append(held)

        def plan_under_the_lock(root):
            thread = threading.Thread(target=try_the_writer_lock)
            thread.start()
            thread.join()
            return real(root)

        self.ops.plan = plan_under_the_lock
        self.tick()
        self.assertEqual(contender, [False])
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)

    def test_a_car_withdrawn_after_the_compose_is_never_pushed(self):  # noqa: VACUOUS_ASSERTION — no push is the ruling
        def withdraw():
            self.ops.cars = [_car(ROW1, "one", TIP1)]
            self.ops.excluded = [dict(_car(ROW2, "two", TIP2),
                                      why="is READY-CONTESTED: a FIX verdict "
                                          "since the approve")]
        self.landing_with(withdraw)
        self.assert_stopped_before_the_push("two", ROW2, "FIX verdict")
        self.assertEqual(self.ops.pushes, 0)
        # the control: READY again at the same tip, `--resume` retries from
        # verified and the same land pushes once
        self.resume_and_land()
        self.assertEqual(self.ops.pushes, 1)

    def test_a_car_retipped_after_the_compose_is_never_pushed(self):  # noqa: VACUOUS_ASSERTION — no push is the ruling
        def retip():
            self.ops.cars = [_car(ROW1, "one", TIP1), _car(ROW2, "two", TIP3)]
        self.landing_with(retip)
        self.assert_stopped_before_the_push("two", ROW2, TIP3[:12])
        self.assertEqual(self.ops.pushes, 0)
        self.resume_and_land()
        self.assertEqual(self.ops.pushes, 1)

    def test_a_door_car_the_tier_no_longer_admits_is_never_pushed(self):  # noqa: VACUOUS_ASSERTION — no push is the ruling
        real = self.ops.car_facts

        def bar_two():
            def facts(root, car):
                got = real(root, car)
                if car["id"] == ROW2:
                    got["admit"] = (False, "a DOOR whose holder is outside "
                                           "the approval tier")
                return got
            self.ops.car_facts = facts
        self.landing_with(bar_two)
        self.assert_stopped_before_the_push("two", ROW2, "outside")
        self.assertEqual(self.ops.pushes, 0)
        self.ops.car_facts = real
        self.resume_and_land()
        self.assertEqual(self.ops.pushes, 1)

    def test_an_unreadable_plan_at_the_push_pushes_nothing(self):
        real = self.ops.plan

        def unreadable():
            self.ops.plan = lambda root: (None, "the land-request projection "
                                                "is unavailable")
        rc, out = self.landing_with(unreadable)
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.ops.pushes, 0, self.ops.calls)
        self.assertEqual(self.current()["state"], autoland.LANDING)
        self.assertTrue(any("projection is unavailable" in p
                            for p in self.ops.posts), self.ops.posts)
        # the control: the plan reads again, and the next tick pushes once
        self.ops.plan = real
        self.tick()
        self.assertEqual(self.ops.pushes, 1, self.ops.calls)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])


class AbandonAfterAPush(Base):
    """THE INTEGRATOR'S RULING R3: `--abandon` refuses any train whose head
    is on origin, or whose remote cannot be read, and names the owed fold
    and the task closes; `--abandon --force --reason R` ends it anyway and
    records the owed fold in the archive and in a post."""

    def stopped_after_the_push(self):
        autoland.seed_counter(self.root, 383, TRUNK)
        st = self.to_gating()
        self.green(st)
        self.ops.ff_answer = (False, "the shared checkout is dirty")
        self.tick()
        st = self.current()
        self.assertEqual(self.ops.pushes, 1)
        self.assertEqual(st["state"], autoland.STOPPED)
        self.assertEqual(st["stopped"]["step"], "pushed")
        return st

    def test_an_abandon_of_a_stopped_pushed_train_names_the_owed_fold(self):
        st = self.stopped_after_the_push()
        row, why = autoland.abandon(self.root, "the-integrator", "drop it",
                                    ops=self.ops)
        self.assertIsNone(row)
        self.assertIn("helm lr foldcheck %s --gate gate:%s --apply"
                      % (st["head"], GID), why)
        # the owed lines are the fold alone: no task is closed at land
        self.assertNotIn("helm task close", why)
        self.assertIn("--force", why)
        self.assertEqual(self.archived(), [])
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        # the control on the same observables: forced, it ends
        row, why = autoland.abandon(self.root, "the-integrator", "drop it",
                                    ops=self.ops, force=True)
        self.assertIsNone(why, why)
        self.assertEqual(row["state"], autoland.ABANDONED)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.ABANDONED])

    def test_a_forced_abandon_records_the_owed_fold(self):  # noqa: VACUOUS_ASSERTION — the owed fold is asserted in the same post; no task close is the point
        st = self.stopped_after_the_push()
        posts = len(self.ops.posts)
        row, why = autoland.abandon(self.root, "the-integrator", "drop it",
                                    ops=self.ops, force=True)
        self.assertIsNone(why, why)
        self.assertEqual(row["state"], autoland.ABANDONED)
        owed = "helm lr foldcheck %s --gate gate:%s --apply" % (st["head"],
                                                                GID)
        (done,) = self.archived()
        self.assertEqual(done["state"], autoland.ABANDONED)
        self.assertIn(owed, done["abandoned"]["owed"])
        self.assertTrue(done["abandoned"]["forced"])
        told = self.ops.posts[posts:]
        self.assertEqual(len(told), 1, told)
        self.assertTrue(told[0].startswith("@integrator"), told[0])
        self.assertIn(owed, told[0])
        self.assertNotIn("helm task close", told[0])

    def test_an_abandon_whose_remote_cannot_be_read_is_refused(self):
        self.to_gating()
        self.ops.remote = None
        row, why = autoland.abandon(self.root, "the-integrator", "drop it",
                                    ops=self.ops)
        self.assertIsNone(row)
        self.assertIn("cannot be read", why)
        self.assertEqual(self.current()["state"], autoland.GATING)
        # the control: the remote reads, the head is not on it, and the same
        # abandon ends the train
        self.ops.remote = TRUNK
        row, why = autoland.abandon(self.root, "the-integrator", "drop it",
                                    ops=self.ops)
        self.assertIsNone(why, why)
        self.assertEqual(row["state"], autoland.ABANDONED)
        self.assertNotIn("owed", row["abandoned"] or {})

    def test_a_train_with_no_head_is_abandoned_without_the_remote(self):
        """An INTENT train composed nothing, so nothing of it can be on
        origin: its abandon asks no remote."""
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        self.ops.remote = None
        row, why = autoland.abandon(self.root, "the-integrator", "drop it",
                                    ops=self.ops)
        self.assertIsNone(why, why)
        self.assertEqual(row["state"], autoland.ABANDONED)

    def test_the_abandon_verb_takes_force(self):
        self.stopped_after_the_push()
        with mock.patch.object(autoland, "Ops", lambda: self.ops):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = autoland.cmd(["--abandon", "--reason", "drop it",
                                   "--repo", self.repo])
            self.assertEqual(rc, 1, err.getvalue())
            self.assertIn("helm lr foldcheck", err.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(autoland.cmd(["--force", "--repo",
                                               self.repo]), 2)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = autoland.cmd(["--abandon", "--force", "--reason",
                                   "drop it", "--repo", self.repo])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("ABANDONED", out.getvalue())
        self.assertIn("helm lr foldcheck", out.getvalue())



class AbandonDrop(Base):
    """Blame that names no car leaves the train STOPPED. Ending it with the
    car named keeps that tip out of the next plan, and the other cars stay."""

    def test_a_one_car_drop_is_not_composed_again(self):  # noqa: VACUOUS_ASSERTION — the recorded tip and the IDLE line are the positive controls; no active train is that same outcome
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        self.ops.real_ejections = True
        row, why = autoland.abandon(
            self.root, "the-integrator", "the audit named nobody",
            ops=self.ops, drop="one")
        self.assertIsNone(why, why)
        self.assertEqual(row["abandoned"]["dropped"]["tip"], TIP1)
        standing, err = landwindow.read_ejections(self.root)
        self.assertIsNone(err, err)
        self.assertIn(TIP1, standing)
        _rc, out = self.tick()
        self.assertIsNone(self.current())
        self.assertIn("IDLE", out)

    def test_the_other_cars_of_a_dropped_train_stay_ready(self):  # noqa: VACUOUS_ASSERTION — the next train carrying the other tip is the positive control; the ejection set is the dropped car
        self.ops.cars = [_car(ROW1, "one", TIP1), _car(ROW2, "two", TIP2)]
        self.tick()
        self.ops.real_ejections = True
        row, why = autoland.abandon(
            self.root, "the-integrator", "keep the rest",
            ops=self.ops, drop=ROW1)
        self.assertIsNone(why, why)
        self.tick()
        st = self.current()
        self.assertEqual([c["tip"] for c in st["cars"]], [TIP2])
        standing, err = landwindow.read_ejections(self.root)
        self.assertIsNone(err, err)
        self.assertEqual(set(standing), {TIP1})

    def test_a_drop_that_names_nobody_abandons_nothing(self):  # noqa: VACUOUS_ASSERTION — the train still INTENT is the positive control; an empty ejection store is that same refusal
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.tick()
        row, why = autoland.abandon(
            self.root, "the-integrator", "no such car",
            ops=self.ops, drop="missing")
        self.assertIsNone(row)
        self.assertIn("no car", why)
        self.assertEqual(self.current()["state"], autoland.INTENT)
        standing, err = landwindow.read_ejections(self.root)
        self.assertIsNone(err, err)
        self.assertEqual(standing, {})

    def test_drop_is_only_an_abandon_flag(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = autoland.cmd(["--drop", "one", "--repo", self.repo])
        self.assertEqual(rc, 2)
        self.assertIn("--abandon", err.getvalue())


class SingleFlight(Base):
    """THE INTEGRATOR'S RULING R4: a STOPPED auto-land train still holds the
    flight, so `helm train --apply` refuses beside it and names `--resume`
    and `--abandon`."""

    def test_the_flight_refusal_names_its_way_out(self):
        """A damaged store blocks the manual train, and no verb reads past it
        (--resume and --abandon read the same store), so the refusal names the
        directory to repair. Each suggested verb carries --repo, so it acts on
        this repository from wherever the integrator stands (task/3265 door
        read)."""
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.ops.audit_ok = False
        self.tick()
        self.ops.clock += 301
        self.tick()
        why = autoland.flight_refusal(self.root)
        self.assertIn("--resume --repo %s" % self.root, why)
        self.assertIn("--abandon --repo %s" % self.root, why)
        self.assertIn("--reason R` ends it;", why)
        self.assertIn("add `--drop ROW|LANE` to keep", why)
        _row, err = autoland.abandon(self.root, "the-integrator", "clear it",
                                     ops=self.ops)
        self.assertIsNone(err, err)
        self.assertIsNone(autoland.flight_refusal(self.root))   # the control
        with open(os.path.join(autoland.state_dir(self.root), "torn.json"),
                  "w") as fh:
            fh.write("{")
        why = autoland.flight_refusal(self.root)
        self.assertIn("UNKNOWN", why)
        self.assertIn(autoland.state_dir(self.root), why)

    def test_a_manual_helm_train_apply_refuses_beside_a_stopped_train(self):
        self.ops.cars = [_car(ROW1, "one", TIP1)]
        self.ops.audit_ok = False
        self.tick()
        self.ops.clock += 301
        self.tick()
        self.assertEqual(self.current()["state"], autoland.STOPPED)
        why = autoland.flight_refusal(self.root)
        self.assertIsNotNone(why)
        for word in ("STOPPED", "--resume", "--abandon"):
            self.assertIn(word, why)
        out = io.StringIO()
        rc = landwindow.compose(self.repo, apply=True,
                                project=lambda: ({}, None), out=out)
        self.assertEqual(rc, 1)
        self.assertIn("auto-land", out.getvalue())
        self.assertIn("--resume", out.getvalue())
        # the control: abandoned, the same compose does not name auto-land
        _row, why = autoland.abandon(self.root, "the-integrator", "clear it",
                                     ops=self.ops)
        self.assertIsNone(why, why)
        self.assertIsNone(autoland.flight_refusal(self.root))
        out = io.StringIO()
        landwindow.compose(self.repo, apply=True, project=lambda: ({}, None),
                           out=out)
        self.assertNotIn("auto-land", out.getvalue())


class FoldProof(Base):
    """THE INTEGRATOR'S RULING R5: the fold proof fails closed. A fold whose
    composition authority is UNKNOWN, or that carries any `????` rung, is
    NOT proven: the train stops at LANDING/guarded, takes no LAND number,
    closes nothing and announces nothing."""

    FIVE = ["ok    tip-exists     x", "ok    tree-vs-gate   x",
            "ok    ff-able        x", "ok    head-clean     x",
            "ok    origin-has-it  x is on origin/main"]
    CLOSED_OWN = ["  CLOSED    %s  tip %s  held by reader-seat — ok"
                  % (rid, tip[:12]) for rid, tip in
                  ((ROW1, TIP1), (ROW2, TIP2))]

    def fold(self, text):
        autoland.seed_counter(self.root, 383, TRUNK)
        st = self.to_gating()
        self.green(st)
        self.ops.fold_text = "\n".join(text)
        self.tick()
        return st

    def assert_not_proven(self):
        st = self.current()
        self.assertIsNotNone(st, "the fold was read as proven and the train "
                                 "landed: %s" % self.archived())
        self.assertEqual(self.ops.pushes, 1)
        self.assertEqual(st["state"], autoland.STOPPED, st)
        self.assertEqual(st["stopped"]["step"], "guarded")
        self.assertEqual(autoland.read_counter(self.root)["n"], 383)
        self.assertEqual(self.ops.closed_tasks, [])
        self.assertEqual(self.ops.closed_rows, [])
        self.assertFalse(any("LAND 384" in p for p in self.ops.posts),
                         self.ops.posts)

    def test_an_unknown_composition_authority_is_not_proven(self):  # noqa: VACUOUS_ASSERTION — no LAND number is the ruling
        self.fold(self.FIVE + [
            "all five PASSED — composition authority is UNKNOWN", "",
            "????  composition-proof                project registry could "
            "not be read — UNKNOWN is not consent"])
        self.assert_not_proven()
        self.assertEqual(autoland.read_counter(self.root)["n"], 383)
        # the control: the registry reads, and the same land folds
        said, why = autoland.resume(self.root, "the-integrator",
                                    now=self.ops.clock)
        self.assertIsNone(why, why)
        self.assertIn("retries from LANDING/guarded", said)
        self.ops.fold_text = "\n".join(
            self.FIVE + ["all five PASSED — this fold is provable"]
            + self.CLOSED_OWN)
        self.tick()
        self.assertEqual(autoland.read_counter(self.root)["n"], 384)

    def test_a_refused_source_clean_row_cannot_be_a_proven_fold(self):
        self.fold(self.FIVE + [
            "all five PASSED — this fold is provable", "",
            "  REFUSED d1d1d1d1d1d1  holder not admitted by current policy"])
        self.assert_not_proven()
        self.assertIn("REFUSED", self.current()["stopped"]["why"])
        _said, why = autoland.resume(self.root, "the-integrator",
                                    now=self.ops.clock)
        self.assertIsNone(why, why)
        self.ops.fold_text = "\n".join(
            self.FIVE + ["all five PASSED — this fold is provable"]
            + self.CLOSED_OWN)
        self.tick()
        self.assertEqual(autoland.read_counter(self.root)["n"], 384)

    def test_missing_own_close_stops_before_numbering(self):
        self.fold(self.FIVE + ["all five PASSED — this fold is provable"])
        self.assert_not_proven()
        self.assertIn(ROW1, self.current()["stopped"]["why"])
        self.assertIn(ROW2, self.current()["stopped"]["why"])
        _said, why = autoland.resume(self.root, "the-integrator",
                                    now=self.ops.clock)
        self.assertIsNone(why, why)
        self.ops.preclosed.update((ROW1, ROW2))
        self.tick()
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_one_missing_own_close_is_not_hidden_by_another(self):
        self.fold(self.FIVE + ["all five PASSED — this fold is provable"]
                  + self.CLOSED_OWN[:1])
        self.assert_not_proven()
        self.assertIn(ROW2, self.current()["stopped"]["why"])

    def test_a_closed_foreign_row_with_the_same_prefix_is_not_an_own_close(self):
        foreign = ROW1[:12] + ("f" * 20)
        self.fold(self.FIVE + ["all five PASSED — this fold is provable",
                               "  CLOSED    %s  tip %s  held by reader-seat — ok"
                               % (foreign, TIP1[:12])] + self.CLOSED_OWN[1:])
        self.assert_not_proven()
        self.assertIn(ROW1, self.current()["stopped"]["why"])

    def test_a_foreign_refusal_is_reported_without_stopping_own_cars(self):
        self.fold(self.FIVE + [
            "all five PASSED — this fold is provable", "",
            "SOURCE-CLEAN HOLDS THIS HEAD LANDS (gate:%s, APPLY):" % GID,
            "  REPORTED  %s  tip %s  held by reader-seat — REFUSED: no hold proof"
            % (ROW3, TIP3[:12])] + self.CLOSED_OWN)
        self.assertEqual([d["state"] for d in self.archived()], [autoland.DONE])
        self.assertEqual(autoland.read_counter(self.root)["n"], 384)
        self.assertIn(ROW3[:12], self.ops.posts[-1])
        self.assertIn("foreign source-clean holds", self.ops.posts[-1])

    def test_a_foreign_failed_close_is_reported_without_stopping_own_cars(self):
        self.fold(self.FIVE + [
            "all five PASSED — this fold is provable", "",
            "SOURCE-CLEAN HOLDS THIS HEAD LANDS (gate:%s, APPLY):" % GID,
            "  REPORTED  %s  tip %s  held by reader-seat — FAILED: write refused"
            % (ROW3, TIP3[:12])] + self.CLOSED_OWN)
        self.assertEqual([d["state"] for d in self.archived()], [autoland.DONE])
        self.assertEqual(autoland.read_counter(self.root)["n"], 384)
        self.assertIn(ROW3[:12], self.ops.posts[-1])

    def test_an_own_car_labeled_foreign_stops_even_with_closed_lines(self):
        self.fold(self.FIVE + ["all five PASSED — this fold is provable",
                               "  REPORTED  %s  tip %s — REFUSED: no proof"
                               % (ROW1, TIP1[:12])] + self.CLOSED_OWN)
        self.assert_not_proven()
        self.assertIn("own car as foreign", self.current()["stopped"]["why"])

    def test_a_nonzero_fold_with_a_foreign_report_still_stops(self):
        self.ops.fold_rc = 1
        self.fold(self.FIVE + [
            "all five PASSED — this fold is provable", "",
            "SOURCE-CLEAN HOLDS THIS HEAD LANDS (gate:%s, APPLY):" % GID,
            "  REPORTED  %s  tip %s  held by reader-seat — FAILED: write refused"
            % (ROW3, TIP3[:12])])
        self.assert_not_proven()
        self.assertIn("exited 1", self.current()["stopped"]["why"])

    def test_own_car_missing_from_the_head_stops_the_fold(self):
        self.fold(self.FIVE + [
            "all five PASSED — this fold is provable", "",
            "SOURCE-CLEAN HOLDS THIS HEAD LANDS (gate:%s, APPLY):" % GID,
            "  REFUSED   %s  tip %s  held by reader-seat — own train car "
            "NOT-IN-HEAD" % (ROW1[:12], TIP1[:12])])
        self.assert_not_proven()
        self.assertIn("NOT-IN-HEAD", self.current()["stopped"]["why"])

    def test_own_car_refusal_still_stops_the_fold(self):
        self.fold(self.FIVE + [
            "all five PASSED — this fold is provable", "",
            "SOURCE-CLEAN HOLDS THIS HEAD LANDS (gate:%s, APPLY):" % GID,
            "  REFUSED   %s  tip %s  held by reader-seat — no hold proof"
            % (ROW1[:12], TIP1[:12])])
        self.assert_not_proven()
        self.assertIn(ROW1[:12], self.current()["stopped"]["why"])

    def test_a_nonzero_fold_apply_is_not_proven(self):  # noqa: VACUOUS_ASSERTION — the zero-rc control immediately resumes and completes the same pushed land
        self.ops.fold_rc = 1
        self.fold(self.FIVE + ["all five PASSED — this fold is provable"])
        self.assert_not_proven()
        self.assertIn("exited 1", self.current()["stopped"]["why"])
        # the control: the same proof on a successful apply completes the land
        _said, why = autoland.resume(self.root, "the-integrator",
                                    now=self.ops.clock)
        self.assertIsNone(why, why)
        self.ops.fold_rc = 0
        self.ops.fold_text += "\n" + "\n".join(self.CLOSED_OWN)
        self.tick()
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_any_unknown_rung_is_not_proven(self):
        proof = self.FIVE + [
            "all five PASSED — active composition proof follows", "",
            "composition proof:",
            "ok    base-gap                           0 behind",
            "????  manifest-matches                   git could not answer",
            "composition proof PASSED — this fold is provable"]
        self.fold(proof)
        self.assert_not_proven()
        # the control: the same proof with every control ok lands
        _said, why = autoland.resume(self.root, "the-integrator",
                                     now=self.ops.clock)
        self.assertIsNone(why, why)
        self.ops.fold_text = "\n".join(
            [ln for ln in proof if not ln.startswith("????")]
            + self.CLOSED_OWN)
        self.tick()
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])
        self.assertEqual(autoland.read_counter(self.root)["n"], 384)


# The tick OrphanedPush SIGKILLs: the shipped state machine from LANDING/
# verified, with every seam but the push guard's git reads and the push itself
# faked. Its signatures take anything, so it drives every revision of the seams.
_ORPHAN_TICK = r'''
import sys
sys.path.insert(0, sys.argv[1])
from helm import autoland, foldcheck, hostpath_guard
repo, base, row, tip = sys.argv[2:6]
hostpath_guard._visibility = lambda url: (hostpath_guard.PRIVATE, None)


class Ops(autoland.Ops):
    def now(self):
        return 1000000.0

    def post(self, text):
        return {"id": "row"}, None

    def address(self, text):
        return text

    def plan(self, root):
        return {"cars": [{"id": row, "lane": "one", "tip": tip,
                          "basis": "approved"}],
                "excluded": [], "ejections_unknown": None}, None

    def car_facts(self, root, car):
        return {"task": None, "priority": None, "doors": [], "author": "a",
                "reader": "r", "model": "m", "admit": (True, None)}

    def foldcheck(self, *args, **kw):
        return [foldcheck.Rung(name, foldcheck.PASS, "ok")
                for name in ("tip-exists", "tree-vs-gate", "ff-able")]

    def verify(self, *args, **kw):
        return True, None

    def remote_head(self, root):
        return base


sys.exit(autoland.tick(repo, apply=True, ops=Ops()))
'''

# The `git` first on the tick's PATH: every call goes to the real git, except a
# push, which records that it started and waits for the arm's release first.
_BLOCKING_GIT = """#!/bin/sh
for a in "$@"; do
  if [ "$a" = push ]; then
    : > '%(dir)s/push.started'
    while [ ! -e '%(dir)s/push.release' ]; do sleep 0.05; done
    '%(git)s' "$@" >>'%(dir)s/push.out' 2>&1
    rc=$?
    : > '%(dir)s/push.done'
    exit $rc
  fi
done
exec '%(git)s' "$@"
"""


class OrphanedPush(unittest.TestCase):
    """F4 (task/3265 races): a tick SIGKILLed while its `git push` runs. The
    tick, store and dispatch-ledger locks are what order an abandon, a
    verdict and an ejection against that push, so they live until the push
    ends: the push child holds them on descriptors it inherited, and a
    killed parent lets none of them go. Run for real: a child python drives
    one LANDING tick against a temp repository and a local bare remote,
    through a `git` that stops inside `push` until the arm releases it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-autoland-orphan-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home")})
        env.start()
        self.addCleanup(env.stop)
        self.remote = os.path.join(self.tmp, "remote.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", self.remote)
        self.repo = os.path.realpath(os.path.join(self.tmp, "proj"))
        _git(self.tmp, "clone", "-q", self.remote, self.repo)
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
        _git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(self.repo, "config", "helm.trunkRef", "refs/heads/main")
        _git(self.repo, "config", "helm.trunkRemote", "origin")
        _git(self.repo, "config", "helm.trunkUrl", self.remote)
        self.base = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "land")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        fake = os.path.join(self.bin, "git")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write(_BLOCKING_GIT % {"dir": self.tmp,
                                      "git": shutil.which("git")})
        os.chmod(fake, 0o755)
        autoland._write_json(autoland.train_path(self.repo, "train9"), {
            "v": 1, "train": "train9", "name": "train9",
            "state": autoland.LANDING, "step": "verified", "rev": 1,
            "head": self.head, "trunk": self.base, "ref": "origin/main",
            "receipt": {"id": GID, "ran": 1, "delta": 0, "ast": 0},
            "cars": [{"id": ROW1, "lane": "one", "tip": TIP1,
                      "basis": "approved"}],
            "rooms": [], "posted": [], "history": [], "dropped": [],
            "red": []})

    def locks(self):
        return (("tick", autoland._lock_path(self.repo, "tick")),
                ("store", autoland._lock_path(self.repo, "state")),
                ("dispatch ledger", dispatches.ledger_path() + ".lock"))

    def held(self):
        """{lock: whether another process holds it}, probed without
        waiting."""
        got = {}
        for name, path in self.locks():
            fd = os.open(path, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got[name] = False
            except BlockingIOError:
                got[name] = True
            finally:
                os.close(fd)
        return got

    def released(self, deadline_s):
        """Release the blocked push, wait for it to end, then for every
        lock to be free."""
        with open(os.path.join(self.tmp, "push.release"), "w"):
            pass
        done = os.path.join(self.tmp, "push.done")
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            if os.path.exists(done) and not any(self.held().values()):
                return True
            time.sleep(0.05)
        return False

    def test_a_killed_tick_leaves_its_locks_held_until_its_push_ends(self):
        script = os.path.join(self.tmp, "tick.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_ORPHAN_TICK)
        log_path = os.path.join(self.tmp, "tick.log")
        log = open(log_path, "w", encoding="utf-8")
        self.addCleanup(log.close)
        proc = subprocess.Popen(
            [sys.executable, script, SRC, self.repo, self.base, ROW1, TIP1],
            env=dict(os.environ, PATH=self.bin + os.pathsep
                     + os.environ.get("PATH", "")),
            stdout=log, stderr=subprocess.STDOUT)

        started = os.path.join(self.tmp, "push.started")

        def reap():
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if os.path.exists(started):
                self.released(60)

        self.addCleanup(reap)
        end = time.monotonic() + 120
        while not os.path.exists(started):
            if proc.poll() is not None or time.monotonic() > end:
                with open(log_path, encoding="utf-8") as fh:
                    self.fail("the tick never reached its push (exit %s): %s"
                              % (proc.poll(), fh.read()[-2000:]))
            time.sleep(0.05)
        every = {name: True for name, _path in self.locks()}
        # the control: while the tick lives, it holds every lock
        self.assertEqual(self.held(), every)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        # the parent is gone and its push runs on: every lock still stands
        self.assertEqual(self.held(), every)
        # the push ends, the locks go with it, and the push it ran landed
        self.assertTrue(self.released(60), self.held())
        self.assertEqual(_git(self.remote, "rev-parse", "main"), self.head)


class TheLandClosesTheReviewFindingsOfItsCarsTest(Base):
    """task/3742: the LAND step closes every open finding a car's chain
    filed, "cured in LAND N" (helm/review_findings.py); a finding of a chain
    the land did not carry stays open, and no car's task is closed."""

    CHAIN, OTHER = "c1" * 8, "c9" * 8
    FIX = "f1" * 8

    def setUp(self):
        super().setUp()
        self.earlier_tip = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "held tip")
        self.held_tip = _git(self.repo, "rev-parse", "HEAD")
        facts = self.ops.car_facts

        def car_facts(root, car):
            return dict(facts(root, car),
                        chain=self.CHAIN if car["id"] == ROW1 else None)
        self.ops.car_facts = car_facts
        # THE LAND READS THE DISPATCH LEDGER AGAIN for the chain's FIX
        # at an ancestor tip. Both tips exist in this scratch Git repository:
        # an unrecorded FIX or invented SHA cannot prove a cure.
        ledger = {self.CHAIN: {"id": self.CHAIN, "lane": "one"},
                  ROW1: {"id": ROW1, "chain_root": self.CHAIN, "lane": "one",
                         "tip": self.held_tip, "status": "held",
                         "source_clean_tip": self.held_tip},
                  self.FIX: {"id": self.FIX, "chain_root": self.CHAIN,
                             "polarity": "fix", "reviewed_tip": self.earlier_tip,
                             "status": "verdict", "lane": "one"}}
        self.ledger = ledger
        read = mock.patch.object(dispatches, "snapshot",
                                 return_value=(self.ledger, None))
        read.start()
        self.addCleanup(read.stop)

    def to_gating(self, cars=None):
        cars = [_car(c["id"], c["lane"], self.held_tip, c["basis"])
                if c["id"] == ROW1 and c["tip"] == TIP1 else c
                for c in cars or ()]
        return super().to_gating(cars)

    def finding(self, title, chain):
        row, why = tasks.add(title, "builder-seat", force_new=True,
                             found_in=self.FIX if chain == self.CHAIN
                             else chain, found_chain=chain)
        self.assertIsNone(why, why)
        return row

    def test_a_land_closes_the_findings_of_the_chain_it_landed(self):  # noqa: VACUOUS_ASSERTION — no task close is the product law; the finding close is asserted exactly on the same land
        ours = self.finding("the retry drops the lock", self.CHAIN)
        theirs = self.finding("a finding of a chain that did not land",
                              self.OTHER)
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        self.assertEqual([c.get("chain") for c in st["cars"]],
                         [self.CHAIN, None])
        self.green(st)
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])
        rows = tasks.rows()
        self.assertEqual((rows[ours["id"]]["status"],
                          rows[ours["id"]]["closed_reason"]),
                         ("closed", "cured in LAND 384"))
        self.assertEqual(rows[theirs["id"]]["status"], "open")
        self.assertEqual(self.ops.closed_tasks, [])

    def test_a_missing_FIX_after_compose_cannot_close_its_orphaned_finding(self):  # noqa: VACUOUS_ASSERTION — missing namer is paired with a real ancestral FIX control proving eligibility
        from helm import review_findings
        ours = self.finding("the orphaned finding stays open", self.CHAIN)
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        self.assertEqual(st["cars"][0].get("chain"), self.CHAIN)
        del self.ledger[self.FIX]
        self.green(st)
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual(tasks.rows()[ours["id"]]["status"], "open")
        self.assertIn("no surviving FIX names open finding(s) %s"
                      % ours["id"], " ".join(self.ops.posts))
        # CONTROL: with its naming FIX, the same land closes the finding.
        self.ledger[self.FIX] = {"id": self.FIX, "chain_root": self.CHAIN,
                                 "polarity": "fix",
                                 "reviewed_tip": self.earlier_tip,
                                 "status": "verdict", "lane": "one"}
        again, why = review_findings.named_at(
            self.CHAIN, self.held_tip, self.ledger, self.root)
        self.assertIsNone(why, why)
        self.assertNotIn(ours["id"], again)

    def test_a_land_never_closes_the_car_s_own_task(self):
        """task/3626/3643: the car's task is its owner's to close, even when
        that task is a finding its own chain filed."""
        mine = self.finding("the lane took this finding as its task",
                            self.CHAIN)
        facts = self.ops.car_facts

        def car_facts(root, car):
            got = facts(root, car)
            return dict(got, task=mine["id"]) if car["id"] == ROW1 else got
        self.ops.car_facts = car_facts
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        self.green(st)
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])
        self.assertEqual(tasks.rows()[mine["id"]]["status"], "open")

    def land(self):
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        self.green(st)
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])

    def test_a_land_closes_no_task_of_any_car_it_carries(self):
        """ROW2's task is a finding ROW1's chain filed: ROW1's LAND leaves
        it open, and closes the chain's other finding."""
        theirs = self.finding("the other lane took this finding", self.CHAIN)
        ours = self.finding("the retry drops the lock", self.CHAIN)
        facts = self.ops.car_facts

        def car_facts(root, car):
            got = facts(root, car)
            return dict(got, task=theirs["id"]) if car["id"] == ROW2 else got
        self.ops.car_facts = car_facts
        self.land()
        self.assertEqual(tasks.rows()[theirs["id"]]["status"], "open")
        self.assertEqual(tasks.rows()[ours["id"]]["closed_reason"],
                         "cured in LAND 384")

    def test_a_land_that_cannot_reread_the_dispatch_ledger_closes_nothing(self):
        ours = self.finding("the retry drops the lock", self.CHAIN)
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(None, "a torn line")):
            self.land()
        self.assertEqual(tasks.rows()[ours["id"]]["status"], "open")
        said = [p for p in self.ops.posts
                if "the review findings of lane one were not all closed" in p]
        self.assertEqual(len(said), 1, self.ops.posts)
        self.assertIn("could not be read at the LAND (a torn line)", said[0])
        self.assertIn("nothing was closed", said[0])

    def test_a_land_keeps_every_task_an_unknown_car_s_records_name(self):
        theirs = self.finding("the other lane took this finding", self.CHAIN)
        facts = self.ops.car_facts

        def car_facts(root, car):
            got = facts(root, car)
            return dict(got, task=None, tasks_named=[theirs["id"]]) \
                if car["id"] == ROW2 else got
        self.ops.car_facts = car_facts
        self.land()
        self.assertEqual(tasks.rows()[theirs["id"]]["status"], "open")


class TheLandLeavesAFindingOpenAtTheTipItWasFoundInTest(Base):
    """A LAND of the exact tree a FIX found work in answers nothing: the
    compose records, through the real car facts, which findings of the
    car's chain were named at its tip, and the LAND leaves them open. The
    dispatch ledger is `self.ledger` for the whole test, so a row an arm
    adds after the compose is what the LAND step reads."""

    CHAIN = "c1" * 8
    FIX_HERE, FIX_EARLIER = "f1" * 8, "f2" * 8
    FIX_PAST, FIX_BEFORE = "f3" * 8, "f4" * 8

    def setUp(self):
        super().setUp()
        from helm import review_door, reviewer_eligibility, trainblame
        # These findings ask REAL git ancestry: the train's symbolic TIP1/TIP3
        # cannot prove a cure and must not silently act as an empty keep-set.
        self.earlier_tip = _git(self.repo, "rev-parse", "HEAD")
        self.held_tip = self.commit("the held tip")
        self.ledger = {
            self.CHAIN: {"id": self.CHAIN, "lane": "one",
                         "tip": self.earlier_tip, "repo_root": self.root},
            ROW1: {"id": ROW1, "chain_root": self.CHAIN,
                   "tip": self.held_tip, "lane": "one", "status": "held",
                   "source_clean_tip": self.held_tip},
            self.FIX_HERE: {"id": self.FIX_HERE, "chain_root": self.CHAIN,
                            "status": "verdict", "polarity": "fix",
                            "reviewed_tip": self.held_tip, "lane": "one"},
            self.FIX_EARLIER: {"id": self.FIX_EARLIER,
                               "chain_root": self.CHAIN, "status": "verdict",
                               "polarity": "fix",
                               "reviewed_tip": self.earlier_tip,
                               "lane": "one"}}
        read = mock.patch.object(dispatches, "snapshot",
                                 side_effect=lambda *a, **k: (self.ledger,
                                                              None))
        read.start()
        self.addCleanup(read.stop)
        ops = self.ops

        def car_facts(root, car):
            with mock.patch.object(review_door, "lane_doors",
                                   return_value={"doors": []}), \
                    mock.patch.object(trainblame, "lane_task",
                                      return_value=(None, None, None)), \
                    mock.patch.object(reviewer_eligibility, "read_model",
                                      return_value="a-model"):
                return autoland.Ops.car_facts(ops, root, car)
        self.ops.car_facts = car_facts

    def to_gating(self, cars=None):
        # Only this fixture's ROW1 is a real finding-closure ancestry probe.
        cars = [_car(c["id"], c["lane"], self.held_tip, c["basis"])
                if c["id"] == ROW1 and c["tip"] == TIP1 else c
                for c in cars or ()]
        return super().to_gating(cars)

    def fix(self, rid, tip):
        """A FIX of the chain, read at `tip`."""
        self.ledger[rid] = {"id": rid, "chain_root": self.CHAIN,
                             "status": "verdict", "polarity": "fix",
                             "reviewed_tip": tip, "lane": "one"}

    def commit(self, text):
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", text)
        return _git(self.repo, "rev-parse", "HEAD")

    def land_cars(self, cars):
        st = self.to_gating(cars)
        self.assertEqual(st["cars"][0].get("chain"), self.CHAIN)
        return st

    def landed(self, st):
        self.green(st)
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])
        return tasks.rows()

    def test_a_land_never_closes_a_finding_another_live_lane_took(self):  # noqa: VACUOUS_ASSERTION — the open finding is paired with the chain's other finding closed exactly by the same LAND
        theirs = self.finding("another lane took this finding",
                              self.FIX_EARLIER)
        ours = self.finding("the cap is off by one", self.FIX_EARLIER)
        _git(self.repo, "branch", "lane/finding-lane")
        _wrote, why = taskkey.record_lane(self.root, "finding-lane",
                                          theirs["id"])
        self.assertIsNone(why, why)
        rows = self.landed(self.land_cars([
            _car(ROW1, "one", TIP1),
            _car(ROW2, "two", TIP2, basis="approved")]))
        self.assertEqual(rows[theirs["id"]]["status"], "open")
        # CONTROL: the chain's other finding closes at the same LAND
        self.assertEqual(rows[ours["id"]]["closed_reason"],
                         "cured in LAND 384")

    def test_a_numbered_lane_without_a_record_is_protected_at_compose_and_land(self):
        mine, why = tasks.add("numbered lane took this finding", "builder-seat",
                              tid="699999", force_new=True,
                              found_in=self.FIX_EARLIER,
                              found_chain=self.CHAIN)
        self.assertIsNone(why, why)
        other = self.finding("other chain work", self.FIX_EARLIER)
        _git(self.repo, "branch", "lane/review-699999")
        st = self.land_cars([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        self.assertIn(mine["id"], st["cars"][0]["tasks_named"])
        rows = self.landed(st)
        self.assertEqual(rows[mine["id"]]["status"], "open")
        self.assertEqual(rows[other["id"]]["closed_reason"],
                         "cured in LAND 384")

    def test_a_numbered_lane_claimed_after_compose_is_protected_at_land(self):  # noqa: VACUOUS_ASSERTION — the absent compose task is paired with the open task and other finding closed by the same LAND
        mine, why = tasks.add("numbered lane took this finding", "builder-seat",
                              tid="699999", force_new=True,
                              found_in=self.FIX_EARLIER,
                              found_chain=self.CHAIN)
        self.assertIsNone(why, why)
        other = self.finding("other chain work", self.FIX_EARLIER)
        st = self.land_cars([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        self.assertNotIn(mine["id"], st["cars"][0]["tasks_named"])
        _git(self.repo, "branch", "lane/review-699999")
        rows = self.landed(st)
        self.assertEqual(rows[mine["id"]]["status"], "open")
        self.assertEqual(rows[other["id"]]["closed_reason"],
                         "cured in LAND 384")

    def test_a_FIX_recorded_after_the_compose_past_the_landed_tip_stays(self):  # noqa: VACUOUS_ASSERTION — the open finding is paired with the finding named before the landed tip closed exactly by the same LAND
        before = _git(self.repo, "rev-parse", "HEAD")
        tip = self.commit("the held tip")
        past = self.commit("a round past the held tip")
        self.ledger[ROW1] = dict(self.ledger[ROW1], tip=tip,
                                  source_clean_tip=tip)
        st = self.land_cars([_car(ROW1, "one", tip),
                             _car(ROW2, "two", TIP2, basis="approved")])
        # AFTER THE COMPOSE: a later round reads a tree past the held tip
        # and files work; another FIX read the tree before it
        self.fix(self.FIX_PAST, past)
        self.fix(self.FIX_BEFORE, before)
        later = self.finding("a round past the land found this",
                             self.FIX_PAST)
        earlier = self.finding("a round before the land found this",
                               self.FIX_BEFORE)
        rows = self.landed(st)
        self.assertEqual(rows[later["id"]]["status"], "open")
        # CONTROL: work read in a tree the LAND contains closes
        self.assertEqual(rows[earlier["id"]]["closed_reason"],
                         "cured in LAND 384")

    def finding(self, title, fix):
        row, why = tasks.add(title, "builder-seat", force_new=True,
                             found_in=fix, found_chain=self.CHAIN)
        self.assertIsNone(why, why)
        return row

    def unread_posts(self, lane):
        return [p for p in self.ops.posts
                if "the review findings of lane %s were not all closed"
                % lane in p]

    def test_unknown_ancestry_at_compose_reports_and_keeps_the_chain(self):  # noqa: VACUOUS_ASSERTION — the missing chain is paired with the open finding and exact ancestry refusal posted at the same LAND
        from helm import vcs
        ours = self.finding("the cap is off by one", self.FIX_EARLIER)
        with mock.patch.object(vcs.backend(self.root), "ancestry",
                               return_value=vcs.UNKNOWN):
            st = self.to_gating([_car(ROW1, "one", TIP1),
                                 _car(ROW2, "two", TIP2, basis="approved")])
        self.assertIsNone(st["cars"][0].get("chain"))
        rows = self.landed(st)
        self.assertEqual(rows[ours["id"]]["status"], "open")
        self.assertIn("git ancestry could not prove", " ".join(
            self.unread_posts("one")))

    def test_unknown_ancestry_at_land_reports_and_keeps_the_chain(self):
        from helm import vcs
        ours = self.finding("the cap is off by one", self.FIX_EARLIER)
        st = self.land_cars([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        with mock.patch.object(vcs.backend(self.root), "ancestry",
                               return_value=vcs.UNKNOWN):
            rows = self.landed(st)
        self.assertEqual(rows[ours["id"]]["status"], "open")
        self.assertIn("git ancestry could not prove", " ".join(
            self.unread_posts("one")))

    def test_a_compose_that_cannot_read_the_findings_says_so_at_the_land(self):
        """task/3862 L2: the car's doors are read before its findings, so a
        findings read that raises at the compose leaves the doors known; and
        the LAND says the failure, instead of closing nothing in silence."""
        from helm import review_findings
        ours = self.finding("the cap is off by one", self.FIX_EARLIER)
        with mock.patch.object(review_findings, "named_at",
                               side_effect=OSError("a torn read")):
            st = self.to_gating([_car(ROW1, "one", TIP1),
                                 _car(ROW2, "two", TIP2, basis="approved")])
        car = st["cars"][0]
        self.assertEqual((car["id"], car.get("chain"), car.get("doors")),
                         (ROW1, None, []))
        rows = self.landed(st)
        self.assertEqual(rows[ours["id"]]["status"], "open")
        said = self.unread_posts("one")
        self.assertEqual(len(said), 1, self.ops.posts)
        self.assertIn("the compose could not read them (OSError: a torn "
                      "read); nothing was closed", said[0])

    def test_a_compose_read_that_names_an_unknown_lane_is_said(self):
        """A read that answers with a reason, not a raise, is said too."""
        from helm import review_findings
        ours = self.finding("the cap is off by one", self.FIX_EARLIER)
        with mock.patch.object(review_findings, "lane_tasks",
                               return_value=(set(), "the lane records of r "
                                             "could not be read (torn)")):
            st = self.to_gating([_car(ROW1, "one", TIP1),
                                 _car(ROW2, "two", TIP2, basis="approved")])
        rows = self.landed(st)
        self.assertEqual(rows[ours["id"]]["status"], "open")
        said = self.unread_posts("one")
        self.assertEqual(len(said), 1, self.ops.posts)
        self.assertIn("the compose could not read them: the lane records of "
                      "r could not be read (torn); nothing was closed",
                      said[0])

    def test_a_compose_that_cannot_read_the_land_step_facts_says_so(self):
        """task/3862 L2, for the land step's own compose reads (task/3746):
        whole or part, the task's room and the chain's other rows. A raise
        there leaves the doors and the findings known, and the LAND says
        its land step did not finish."""
        from helm import landtask
        with mock.patch.object(landtask, "chain_facts",
                               side_effect=OSError("a torn read")):
            st = self.to_gating([_car(ROW1, "one", TIP1),
                                 _car(ROW2, "two", TIP2, basis="approved")])
        car = st["cars"][0]
        self.assertEqual((car["id"], car.get("chain"), car.get("doors")),
                         (ROW1, self.CHAIN, []))
        self.landed(st)
        said = [p for p in self.ops.posts
                if "the land step of lane one did not finish" in p]
        self.assertEqual(len(said), 1, self.ops.posts)
        self.assertIn("the compose could not read whether the lane carried "
                      "the whole ask, its task's room or its chain's other "
                      "rows (OSError: a torn read)", said[0])

    def test_a_finding_named_at_the_landed_tip_stays_open(self):  # noqa: VACUOUS_ASSERTION — the open finding is paired with the earlier-tip finding closed exactly by the same LAND
        here = self.finding("the retry drops the lock", self.FIX_HERE)
        earlier = self.finding("the cap is off by one", self.FIX_EARLIER)
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        self.assertEqual(st["cars"][0].get("chain"), self.CHAIN)
        self.green(st)
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])
        rows = tasks.rows()
        self.assertEqual(rows[here["id"]]["status"], "open")
        # CONTROL: the finding named at an earlier tip closes at the LAND
        self.assertEqual(rows[earlier["id"]]["closed_reason"],
                         "cured in LAND 384")


class TheLandClosesOrAsksTheCarsTaskTest(Base):
    """task/3746: the LAND step closes a car's task when its lane carried
    the whole ask (helm/landtask.py), unless open sub-tasks stay; any other
    land comments the one question on the task and asks it in the task's
    room, addressed to the lane's author. The chain's other open rows are
    offered to their own doors, and a same-lane row with no chain link is
    flagged. The land line says what each car's step did."""

    def file(self, title, **kw):
        row, why = tasks.add(title, "builder-seat", force_new=True, **kw)
        self.assertIsNone(why, why)
        return row["id"]

    def facts(self, **by_row):
        """car_facts answering the fake's facts updated by `by_row`."""
        base = self.ops.car_facts

        def car_facts(root, car):
            return dict(base(root, car), **by_row.get(car["id"], {}))
        self.ops.car_facts = car_facts

    def land(self):
        st = self.to_gating([_car(ROW1, "one", TIP1),
                             _car(ROW2, "two", TIP2, basis="approved")])
        self.green(st)
        rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual([d["state"] for d in self.archived()],
                         [autoland.DONE])
        return st["head"], self.ops.posts[-1]

    def said(self, tid):
        return [c["text"] for c in tasks.comments_of(tasks.get(tid))]

    def test_a_whole_car_leaves_a_landed_task_owing_a_check(self):  # noqa: VACUOUS_ASSERTION — status, owner and land are asserted exactly beside the empty room; noqa: ORPHANED_MOCK — the doubles are reached through land_step -> landtask.run -> observed.stamp -> owner_for, cross-module calls the walker does not follow
        """helm/observed.py: a whole land is not done until someone named
        has seen it working, so the task stays open, LANDED, and the
        announcement @mentions its one check owner with the command."""
        tid = self.file("the lane carries all of this", source="asker-seat")
        self.facts(**{ROW1: {"task": tid, "whole": True, "scope": "helm"}})
        with mock.patch.object(observed, "_roster",
                               lambda: {"asker-seat"}), \
                mock.patch.object(observed, "_dark", lambda seat: None), \
                mock.patch.object(observed, "_role", lambda seat: None):
            head, announce = self.land()
        got = tasks.get(tid)
        self.assertEqual((got["status"], got["landed"]["owner"],
                          got["landed"]["land"]),
                         ("open", "asker-seat", "LAND 384"))
        self.assertIn("%s LANDED — owes a seen-working check by @asker-seat"
                      % tid, announce)
        self.assertIn("helm task observed %s --evidence" % tid, announce)
        self.assertEqual(self.ops.room_posts, [])

    def test_an_unobserved_task_never_holds_the_train(self):  # noqa: ORPHANED_MOCK — observed.stamp reads _roster through owner_for, a call inside the module the walker does not follow
        """A task that owes a check, even one past its 24 hours, never holds
        a land: the next train lands, and a sweep that raises is said and
        changes nothing about the tick."""
        owed = self.file("an earlier land nobody has seen working",
                         source="asker-seat")
        with mock.patch.object(observed, "_roster", lambda: set()):
            observed.stamp(owed, tasks.get(owed), {
                "label": "LAND 300", "sha": TRUNK, "lane": "old",
                "tip": TIP3}, now=T0 - 3 * 86400)
        tid = self.file("the lane carries all of this")
        self.facts(**{ROW1: {"task": tid, "whole": True, "scope": "helm"}})
        with mock.patch.object(observed, "sweep",
                               side_effect=RuntimeError("injected")):
            head, announce = self.land()
        self.assertIn("LAND 384", announce)
        self.assertEqual(tasks.get(owed)["status"], "open")
        self.assertEqual(tasks.get(tid)["status"], "open")
        self.assertIn("owes a seen-working check", announce)

    def test_the_tick_sweeps_checks_past_their_day(self):  # noqa: ORPHANED_MOCK — stamp and the tick's sweep reach every double through owner_for and Ops.observed_sweep, calls the walker does not follow; the moved owner asserted exactly proves they fired
        """The auto-land tick is the one actor that moves a check past 24h
        to its fallback owner; it posts the one @mention itself."""
        owed = self.file("an earlier land nobody has seen working",
                         source="asker-seat")
        with mock.patch.object(observed, "_roster",
                               lambda: {"asker-seat", "lead-seat"}), \
                mock.patch.object(observed, "_dark", lambda seat: None), \
                mock.patch.object(observed, "_role", lambda seat: None), \
                mock.patch.object(observed, "_lead",
                                  lambda project: ("lead-seat", None)):
            observed.stamp(owed, tasks.get(owed), {
                "label": "LAND 300", "sha": TRUNK, "lane": "old",
                "tip": TIP3}, now=T0 - 3 * 86400)
            rc, out = self.tick()
        self.assertEqual(rc, 0, out)
        self.assertEqual(tasks.get(owed)["landed"]["owner"], "lead-seat")
        told = [p for p in self.ops.posts if owed in p]
        self.assertEqual(len(told), 1, self.ops.posts)
        self.assertTrue(told[0].startswith("@lead-seat %s " % owed), told)
        # the next tick moves nothing and says nothing
        self.ops.clock += 3600
        self.tick()
        self.assertEqual(len([p for p in self.ops.posts if owed in p]), 1)

    def test_a_whole_car_over_an_open_sub_task_leaves_it_open(self):
        tid = self.file("the lane carries all of this")
        kid = self.file("a remainder filed on it", continues=tid)
        self.facts(**{ROW1: {"task": tid, "whole": True, "scope": "helm"}})
        head, announce = self.land()
        self.assertEqual(tasks.get(tid)["status"], "open")
        self.assertEqual(self.said(tid), [
            "landed whole in LAND 384 %s (lane one at %s): not closed, "
            "because its sub-task %s is open" % (head[:12], TIP1[:12], kid)])
        self.assertIn("%s stays open" % tid, announce)

    def test_a_part_car_asks_its_task_room_whether_the_whole_ask_is_done(self):
        tid = self.file("the lane carries part of this")
        self.facts(**{ROW1: {"task": tid, "whole": False, "scope": "helm"}})
        head, announce = self.land()
        self.assertEqual(tasks.get(tid)["status"], "open")
        question = ("landed LAND 384 %s (lane one at %s): is the whole ask "
                    "done? close it, narrow its title, or file the remainder "
                    "with --continues %s" % (head[:12], TIP1[:12], tid))
        self.assertEqual(self.said(tid), [question])
        room = "helm-%s" % tid.split("/")[1]
        self.assertEqual([(r, t.split(" ")[:2]) for r, t, _k
                          in self.ops.room_posts],
                         [(room, ["@builder-seat", tid])])
        self.assertIn("asked in %s" % room, announce)
        # THE STEP AGAIN, as a retried tick would run it: nothing twice
        car = self.archived()[0]["cars"][0]
        self.ops.land_step(self.root, car, "LAND 384", head, [])
        self.assertEqual(self.said(tid), [question])
        self.assertEqual(len(self.ops.room_posts), 1)

    def test_the_chain_rows_are_discharged_and_a_same_lane_row_flagged(self):
        tid = self.file("the lane's task")
        build, stray = "e1" * 16, "e2" * 16
        self.ops.row_close_ok = {(build, "landed")}
        self.facts(**{ROW1: {
            "task": tid, "whole": False, "scope": "helm",
            "chain_rows": [{"id": build, "kind": "build", "status": "open",
                            "polarity": None}],
            "lane_rows": [{"id": stray, "task": tid}]}})
        _head, announce = self.land()
        self.assertEqual(self.ops.row_closes, [(build, "landed")])
        self.assertIn("discharged row %s" % build[:12], announce)
        self.assertIn("FLAGGED row %s" % stray[:12], announce)
        flag = [t for t in self.said(tid) if stray[:12] in t]
        self.assertEqual(len(flag), 1, self.said(tid))
        self.assertIn("no chain link", flag[0])

    def test_a_car_with_no_task_says_so_and_closes_nothing(self):  # noqa: VACUOUS_ASSERTION — the land line's no-task words are asserted; no question is the point
        self.facts(**{ROW1: {"task": None, "task_unknown": None}})
        _head, announce = self.land()
        self.assertIn("lane one: no task", announce)
        self.assertEqual(self.ops.room_posts, [])

    def test_the_verbs_auto_land_runs_leave_the_land_step_to_it(self):
        """Its fold and closes are the hand verbs, run as children: each
        child is told the LAND step runs the land step, with the number."""
        from helm import landtask
        seen = []

        def child(argv, **kw):
            seen.append((argv[3], (kw.get("env") or {}).get(
                landtask.DEFER_ENV), argv))
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        ops = autoland.Ops()
        with mock.patch.object(autoland.subprocess, "run", child), \
                mock.patch.object(ops, "declared",
                                  return_value=("r", "origin", "main")):
            answers = [ops.fold_apply(self.root, TIP1, GID, (ROW1, ROW2)),
                       ops.lr_close(ROW1, True, None),
                       ops.lr_row_close(ROW2, "discharged", "LAND 384 x")]
        self.assertEqual(answers, [(0, "{}"), ({}, None), None])
        self.assertEqual([(verb, env) for verb, env, _argv in seen],
                         [("foldcheck", landtask.AUTO_LAND),
                          ("close", landtask.AUTO_LAND),
                          ("close", landtask.AUTO_LAND)])
        self.assertEqual(seen[0][2][-2:],
                         ["--train-cars", "%s,%s" % (ROW1, ROW2)])


class TheLaneNumberNamesTheCarsTaskTest(Base):
    """task/3746 D1: a car whose chain and lane record name no task takes
    the OPEN task its lane's trailing -<N> names, and the merge subject says
    it instead of "no task" (task/3693 landed as train482 and stayed open)."""

    def test_an_open_task_named_by_the_lane_number_is_the_car_s(self):
        from helm import trainblame
        tid = tasks.add("the numbered ask", "builder-seat", tid="3693",
                        force_new=True)[0]["id"]
        n = tid.split("/")[1]
        with mock.patch.object(trainblame, "lane_task",
                               lambda rid: (None, None, None)):
            facts = autoland.Ops().car_facts(
                self.root, _car(ROW1, "fix-the-thing-%s" % n, TIP1))
            self.assertEqual((facts["task"], facts["title"]),
                             (tid, "the numbered ask"))
            detail = autoland._merge_detail(dict(
                _car(ROW1, "fix-the-thing-%s" % n, TIP1), task=tid,
                title=facts["title"], doors=[], author="a", reader="b"))
            self.assertTrue(detail.startswith(tid), detail)
            tasks.close(tid, "closed before its lane landed")
            facts = autoland.Ops().car_facts(
                self.root, _car(ROW1, "fix-the-thing-%s" % n, TIP1))
        self.assertIsNone(facts["task"])


if __name__ == "__main__":
    unittest.main()
