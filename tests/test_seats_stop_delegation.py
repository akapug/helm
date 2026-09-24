#!/usr/bin/env python3
"""Does work a seat DELEGATED hold that seat's stop, and on what proof?

Block (b)'s delegation exemption, asked apart from the rest of the stop guard
because it is the one carve-out that turns on evidence about OTHER PROCESSES.
A saturated delegation may release the stop only on positive proof — real
children, read through a real `/proc` — and every UNKNOWN must block exactly
like the un-exempted rung, because an exemption that fires on ignorance is a
seat walking away from live work. The ancestry half asks the same question one
hop up: which holder does a delegated row actually belong to.

MOVED WHOLE OUT OF `tests/test_seats.py`, which stood 56,594 bytes under the
1 MiB never-track ceiling with arms still landing in it. No body was rewritten
on the way; each class is the byte-identical text it had there.

THE FIXTURE IS REUSED BY REFERENCE, NEVER COPIED. `SeatsBase` is imported from
the module these arms came from, so one fixture serves both files and the two
cannot drift — which matters more here than anywhere, because these arms
repoint `HELM_PROC` off that fixture's empty default on purpose.
"""
import json
import os
import subprocess
import threading
import time
from unittest import mock

from tests.test_seats import SeatsBase

from helm import chat, seats, seats_stop_claims

# THE FIXTURE THAT PROTECTS THESE ARMS LIVES IN ANOTHER FILE, and two
# source-driven audits read THIS one. `SeatsBase.setUp` snapshots every key in
# `ENV_KEYS`, sets `HELM_SCRATCH_GC=0` so the stop hook's silent-mechanical
# lane cannot reap real `/tmp/claude-*` host scratch, and restores the lot in
# tearDown. tests/test_env_hygiene.py and tests/test_scratch.py each parse a
# module ON ITS OWN, so neither can follow an imported base class -- and moving
# these arms out of tests/test_seats.py moved them out of the only file where
# those two audits could see the guarantee. Both went red on the whole-suite
# gate for exactly that reason; neither was wrong to.
#
# SO IT IS RESTATED HERE AS SOMETHING THAT RUNS, not as a comment and not as an
# allowlist entry. This module snapshots the keys its own arms write, puts them
# back when the module is done, and disables the scratch reaper ITSELF rather
# than trusting that it inherited the setting. If SeatsBase ever stops doing
# either, this module is still safe instead of quietly deleting host scratch.
_ENV_PRIOR = {}


def setUpModule():
    _ENV_PRIOR["HELM_SCRATCH_GC"] = os.environ.get("HELM_SCRATCH_GC")
    _ENV_PRIOR["HELM_CHAT_NAME"] = os.environ.get("HELM_CHAT_NAME")
    _ENV_PRIOR["HELM_PROC"] = os.environ.get("HELM_PROC")
    _ENV_PRIOR["HELM_STOP_GUARD_BEACON"] = os.environ.get("HELM_STOP_GUARD_BEACON")
    _ENV_PRIOR["HELM_STOP_GUARD_WIRING"] = os.environ.get("HELM_STOP_GUARD_WIRING")
    os.environ["HELM_SCRATCH_GC"] = "0"


def tearDownModule():
    for key, was in _ENV_PRIOR.items():
        if was is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = was


class StopGuardDelegationTest(SeatsBase):
    """Block (b)'s delegation exemption (board row stop-guard-delegation-
    exemption) — the verifiable-saturated-state carve-out. REAL processes in
    a REAL scratch repo's lane room, read through the REAL /proc (HELM_PROC
    repointed off SeatsBase's empty fixture on purpose): the exemption may
    fire on positive proof ONLY, and every UNKNOWN must block exactly like
    the un-exempted rung. Session ids are minted random so the live-host
    needle scan can never collide with an unrelated process."""

    def setUp(self):
        super().setUp()
        # a scratch project: the SAME pure functions `helm work` mints leases
        # with (resource/lane_path) resolve the planted lease back to this
        # room — the guard anchors the project from its cwd, so chdir in
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        r = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root,
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        subprocess.run(["git", "-C", self.root, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "tip"], check=True, capture_output=True, timeout=30)
        self.wt = os.path.join(self.root + "-wt", "lane-x")
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-x", self.wt], check=True,
                       capture_output=True, timeout=30)
        self.res = "worktree:proj:lane-x"
        self.sid = "s-deleg-" + os.urandom(6).hex()
        self._cwd = os.getcwd()
        os.chdir(self.root)
        os.environ["HELM_PROC"] = "/proc"   # the REAL table — proof must be real
        self.kids = []

    def tearDown(self):
        os.chdir(self._cwd)
        for p in self.kids:
            p.kill()
            p.wait(timeout=10)
        super().tearDown()

    def guard(self, payload=None, args=()):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        return self.cmd("stop-guard", ["--hook-json", *args], stdin=stdin or b"{}")

    def spawn(self, cwd, mark=None, argv=("sleep", "300")):
        """A REAL delegate: env carries the session id exactly the way a
        Bash-tool child's CLAUDE_CODE_SESSION_ID does."""
        env = {"PATH": "/usr/bin:/bin"}
        if mark:
            env["CLAUDE_CODE_SESSION_ID"] = mark
        p = subprocess.Popen(list(argv), cwd=cwd, env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.kids.append(p)
        return p

    def settle(self, pid, cwd, mark=None):
        """Wait out the fork→exec window: pre-exec, /proc shows the PARENT's
        environ — the child is proof only once its own cwd link AND (when
        marked) its own environ read back."""
        want = os.path.realpath(cwd)
        for _ in range(200):
            try:
                ok = os.path.realpath("/proc/%d/cwd" % pid) == want
                if ok and mark:
                    with open("/proc/%d/environ" % pid, "rb") as f:
                        ok = mark.encode() in f.read()
                if ok:
                    return
            except OSError:
                pass
            time.sleep(0.025)
        self.fail("child %d never settled into %s" % (pid, cwd))

    def test_live_delegated_child_exempts_stating_its_proof(self):
        ok, _m, _l = seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.assertTrue(ok)
        p = self.spawn(self.wt, mark=self.sid)
        self.settle(p.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("live delegated build", err)   # SAYS what it verified
        self.assertIn("pid %d" % p.pid, err)
        self.assertIn(self.wt, err)
        self.assertIn(self.res, err)
        self.assertIn("lease retained", err)

    def test_child_in_a_different_directory_never_exempts(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        p = self.spawn(elsewhere, mark=self.sid)     # OUR child, WRONG room
        self.settle(p.pid, elsewhere, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)                      # the poach-shield holds
        self.assertIn(self.res, err)
        self.assertIn(self.res, err)          # the held lane is NAMED
        self.assertIn("act per line, then stop:", err)
        self.assertNotIn("delegated", err)

    def test_foreign_process_in_the_room_never_exempts(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        p = self.spawn(self.wt)                      # right room, NOT our tree
        self.settle(p.pid, self.wt)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn(self.res, err)          # the held lane is NAMED
        self.assertIn("act per line, then stop:", err)

    def test_dead_child_re_blocks_the_very_next_stop(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        p = self.spawn(self.wt, mark=self.sid)
        self.settle(p.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        p.kill()
        p.wait(timeout=10)                           # the SA finished/died
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)                      # per-stop proof, no cache
        self.assertIn(self.res, err)          # the held lane is NAMED
        self.assertIn("act per line, then stop:", err)

    def test_unreadable_proc_table_blocks(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        p = self.spawn(self.wt, mark=self.sid)       # a live delegate EXISTS —
        self.settle(p.pid, self.wt, mark=self.sid)
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "no-such-proc")
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)                      # — but unprovable blocks
        self.assertIn(self.res, err)          # the held lane is NAMED
        self.assertIn("act per line, then stop:", err)

    def test_ppid_chain_reaches_the_marked_ancestor(self):
        # the codex-SA shape: the delegate's own env is scrubbed (env -i),
        # proof arrives via the ppid walk to its live MARKED parent shell
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        pidfile = os.path.join(self.tmp, "kid.pid")
        self.spawn(self.root, mark=self.sid, argv=(
            "bash", "-c",
            "cd '%s' && env -i sleep 300 & echo $! > '%s'; wait"
            % (self.wt, pidfile)))
        for _ in range(200):
            try:
                with open(pidfile) as f:
                    kid = int(f.read().strip())
                break
            except (OSError, ValueError):
                time.sleep(0.025)
        else:
            self.fail("delegate never wrote its pidfile")
        self.addCleanup(self._reap, kid)
        self.settle(kid, self.wt)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("live delegated build", err)
        self.assertIn("pid %d" % kid, err)

    def _reap(self, pid):
        try:
            os.kill(pid, 9)
        except (OSError, ProcessLookupError):
            pass

    def test_interval_sampling_allows_subagent_between_tool_calls(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        sub = self.spawn(self.root, mark=self.sid, argv=(
            "python3", "-c",
            "import os, time; os.chdir('%s'); open('%s/ready', 'w').close(); time.sleep(0.5); os.chdir('%s'); time.sleep(300)"
            % (self.wt, self.tmp, self.tmp)))
        self.settle(sub.pid, self.wt, mark=self.sid)
        # First stop-guard call records process activity in self.wt
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("live delegated build", err)

        # Wait for sub's cwd to move away from self.wt (simulating between-tool-calls state)
        for _ in range(100):
            try:
                if os.path.realpath("/proc/%d/cwd" % sub.pid) != os.path.realpath(self.wt):
                    break
            except OSError:
                pass
            time.sleep(0.025)

        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("recent activity", err)

    def test_posttool_subagent_activity_between_stops_exempts(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))

        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2, err)

        payload = json.dumps({
            "hook_event_name": "PostToolUse",
            "session_id": self.sid,
            "cwd": self.wt,
            "tool_name": "Read",
            "agent_id": "agent-regression",
            "agent_type": "Explore",
        }).encode()
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id, create=True):
            rc, _out = self.cmd_fd("deliver", ["--hook-json"], stdin=payload)
            self.assertEqual(rc, 0)
            rc, _o, err = self.guard({"session_id": self.sid})

        self.assertEqual(rc, 0, err)
        self.assertIn("recent subagent activity", err)
        self.assertIn("agent-regression", err)
        self.assertIn(self.res, err)
        self.assertIn("lease retained", err)

    def test_subagent_stop_removes_interval_exemption(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        event = {
            "session_id": self.sid, "cwd": self.wt,
            "agent_id": "agent-complete", "agent_type": "Explore",
        }
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            body = dict(event, hook_event_name="PostToolUse", tool_name="Read")
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(body).encode())
            rc, _o, err = self.guard({"session_id": self.sid})
            self.assertEqual(rc, 0, err)
            body = dict(event, hook_event_name="SubagentStop")
            rc, _out = self.cmd_fd("delegation-stop", ["--hook-json"],
                                   stdin=json.dumps(body).encode())
            self.assertEqual(rc, 0)
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2, err)
        self.assertIn(self.res, err)          # the held lane is NAMED
        self.assertIn("act per line, then stop:", err)

    def test_subagent_stop_tombstone_survives_claim_lock_contention(self):
        import fcntl
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        event = {"session_id": self.sid, "cwd": self.wt,
                 "agent_id": "agent-contended"}
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            body = dict(event, hook_event_name="PostToolUse", tool_name="Read")
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(body).encode())
        with open(seats.claims_path() + ".lock", "a") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            body = dict(event, hook_event_name="SubagentStop")
            rc, _out = self.cmd_fd("delegation-stop", ["--hook-json"],
                                   stdin=json.dumps(body).encode())
            self.assertEqual(rc, 0)
        self.assertTrue(seats._agent_stopped(self.sid, "agent-contended"))
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2, err)

    def test_posttool_requires_agent_and_exact_claimed_lane(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        nested = os.path.join(self.wt, "nested")
        os.makedirs(nested)
        base = {
            "hook_event_name": "PostToolUse",
            "session_id": self.sid,
            "cwd": self.wt,
            "tool_name": "Read",
            "agent_id": "agent-regression",
            "agent_type": "Explore",
        }
        cases = {
            "main-thread": {k: v for k, v in base.items()
                            if k not in ("agent_id", "agent_type")},
            "blank-agent": dict(base, agent_id=""),
            "agent-type-only": {k: v for k, v in base.items() if k != "agent_id"},
            "wrong-event": dict(base, hook_event_name="PreToolUse"),
            "project-root": dict(base, cwd=self.root),
            "nested-lane": dict(base, cwd=nested),
            "wrong-session": dict(base, session_id="s-other"),
        }
        for name, body in cases.items():
            with self.subTest(name=name), \
                    mock.patch.object(seats, "_enclosing_claude_holder",
                                      return_value=holder_id):
                rc, _out = self.cmd_fd(
                    "deliver", ["--hook-json"],
                    stdin=json.dumps(body).encode())
                self.assertEqual(rc, 0)
                self.assertFalse(os.path.exists(
                    seats._delegation_activity_path(self.sid, self.res)))

        os.environ["HELM_CHAT_NAME"] = "bob"
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(base).encode())
        self.assertFalse(os.path.exists(
            seats._delegation_activity_path(self.sid, self.res)))

        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_DELEGATION"] = "0"
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(base).encode())
        self.assertFalse(os.path.exists(
            seats._delegation_activity_path(self.sid, self.res)))
        os.environ.pop("HELM_STOP_GUARD_DELEGATION")

        class NoLock:
            f = None

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id), \
                mock.patch.object(seats, "_flocked", return_value=NoLock()):
            self.assertFalse(seats._record_posttool_delegation(base))
        self.assertFalse(os.path.exists(
            seats._delegation_activity_path(self.sid, self.res)))

    def test_activity_lock_contention_never_holds_hook_paths(self):  # noqa: VACUOUS_ASSERTION — exact started/completed dictionaries prove both workers reached the barrier and returned while the lock stayed held; forcing blocking acquisition makes completed false
        import fcntl
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        # This test times the CLAIMS-lock boundary, not every independent Stop
        # rung. A lane adding one module legitimately activates wiring's AST
        # census and made the 200ms assertion fail while neither worker held
        # the claims lock — a false verdict about the path this test names.
        os.environ["HELM_STOP_GUARD_WIRING"] = "0"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        event = {"hook_event_name": "PostToolUse", "session_id": self.sid,
                 "cwd": self.wt, "tool_name": "Read",
                 "agent_id": "agent-lock"}
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.assertTrue(seats._record_posttool_delegation(event))
            results, failures = {}, {}
            start = threading.Event()
            ready = {name: threading.Event() for name in ("produce", "consume")}
            done = {name: threading.Event() for name in ready}

            def run(name, fn):
                ready[name].set()
                start.wait()
                try:
                    results[name] = fn()
                except Exception as e:
                    failures[name] = e
                finally:
                    done[name].set()

            workers = [
                threading.Thread(
                    target=run, args=("produce", lambda:
                        seats._record_posttool_delegation(
                            dict(event, agent_id="agent-contender")))),
                threading.Thread(
                    target=run, args=("consume", lambda:
                        seats.stop_guard(session=self.sid, seat="alice"))),
            ]
            with open(seats.claims_path() + ".lock", "a") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX)
                for worker in workers:
                    worker.start()
                started = {name: event.wait(5) for name, event in ready.items()}
                start.set()
                completed = {name: event.wait(5) for name, event in done.items()}
            for worker in workers:
                worker.join(2)
        self.assertEqual(started, {"produce": True, "consume": True},
                         "a worker never reached the start barrier")
        self.assertEqual(completed, {"produce": True, "consume": True},
                         "a hook path waited on the held claims lock")
        self.assertEqual(failures, {})
        self.assertFalse(results["produce"])
        self.assertTrue(results["consume"][0])

    def test_same_basename_foreign_clone_activity_never_exempts(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        other = os.path.join(self.tmp, "other", "proj")
        os.makedirs(other)
        subprocess.run(["git", "init", "-q", "-b", "main", other],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "-C", other, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "tip"], check=True, capture_output=True, timeout=30)
        other_wt = other + "-wt/lane-x"
        subprocess.run(["git", "-C", other, "worktree", "add", "-q",
                        "-b", "lane-x", other_wt], check=True,
                       capture_output=True, timeout=30)
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        base = {"hook_event_name": "PostToolUse", "session_id": self.sid,
                "tool_name": "Read"}
        foreign = dict(base, cwd=other_wt, agent_id="agent-foreign")
        current = dict(base, cwd=self.wt, agent_id="agent-current")
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(foreign).encode())
            rc, _o, err = self.guard({"session_id": self.sid})
            self.assertEqual(rc, 2, err)

            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(current).encode())
            rc, _o, err = self.guard({"session_id": self.sid})
            self.assertEqual(rc, 0, err)
            self.assertIn("agent-current", err)

            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(foreign).encode())
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("agent-current", err)

    def test_posttool_requires_same_holder_incarnation_and_window(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        os.environ["HELM_STOP_GUARD_BEACON"] = "0"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        payload = json.dumps({
            "hook_event_name": "PostToolUse", "session_id": self.sid,
            "cwd": self.wt, "tool_name": "Read",
            "agent_id": "agent-window",
        }).encode()
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"], stdin=payload)
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=(holder_id[0], holder_id[1] + 1)):
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2, err)

        # The block above LATCHED this held set (the 2026-08-04 same-state
        # latch), and the next probe examines CLASSIFICATION — an expired
        # window must not exempt — not emission. Reset the emission memory so
        # the probe can read the classification directly: with the latch
        # cleared, a wrongly-exempting guard exits 0 saying 'live delegated
        # build' and a correctly-blocking one exits 2.
        import glob as _glob
        for p in _glob.glob(os.path.join(
                chat.chat_dir(), "*.%s.*" % seats.LEASE_LATCH)):
            os.unlink(p)

        path = seats._delegation_activity_path(self.sid, self.res)
        data = seats.pk.read_json(path, {})
        key = seats._activity_record_key("post_tool_use", "agent-window")
        data["records"][key]["mono"] = seats._now_mono() - 301
        seats.pk.write_json(path, data)
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2, err)

    def test_a_failed_migration_write_still_returns_the_proof(self):  # noqa: VACUOUS_ASSERTION — the unconditional assertTrue on _get_delegation_activity before any manipulation is the positive control on the SAME observable; the rung cannot credit it because each call mints a fresh producer identity
        """Migration is opportunistic; a write failure must not erase a fact.

        The write sits inside the function's outer `except OSError: return
        None`, so a disk error during an OPTIONAL convenience discarded
        records that were already read, already validated and already the
        answer."""
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        child = self.spawn(self.wt, mark=self.sid)
        self.settle(child.pid, self.wt, mark=self.sid)
        self.assertEqual(self.guard({"session_id": self.sid})[0], 0)

        path = seats._delegation_activity_path(self.sid, self.res)
        data = seats.pk.read_json(path, {})
        self.assertTrue(data.get("records"), "fixture produced no records")
        # old-only, so the read WILL attempt a migration write
        # UNCONDITIONAL positive control on the SAME observable, before any
        # manipulation: the fixture really does yield a readable result, so a
        # later assertion about it is about the property and not about the
        # setup having silently produced nothing.
        self.assertTrue(seats._get_delegation_activity(self.sid, self.res))
        legacy = os.path.join(chat.chat_dir(), os.path.basename(path))
        seats.pk.write_json(legacy, data)
        os.unlink(path)

        with mock.patch.object(seats.pk, "atomic_write",
                               side_effect=OSError("disk full")):
            records = seats._get_delegation_activity(self.sid, self.res)
        self.assertTrue(records, "a failed migration erased proven records")
        # MUST-MISS: with the write WORKING the same read still returns them,
        # so the assertion above is about the failure path and not about the
        # records being unconditionally present.
        seats.pk.write_json(legacy, data)
        self.assertTrue(seats._get_delegation_activity(self.sid, self.res))

    def test_a_dead_new_record_does_not_bury_a_live_legacy_one(self):  # noqa: VACUOUS_ASSERTION — assertIn on the LIVE pid is a positive membership assertion, not an absence; the unconditional pre-manipulation read is the control on the same observable
        """The whole point of preserving both: the proof is downstream.

        A NEWER record whose pid is dead must not evict an OLDER one that is
        live, because the proof checks the pid and would have kept the live
        one. Any merge-time pick by recency gets this exactly backwards."""
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        child = self.spawn(self.wt, mark=self.sid)
        self.settle(child.pid, self.wt, mark=self.sid)
        self.assertEqual(self.guard({"session_id": self.sid})[0], 0)

        path = seats._delegation_activity_path(self.sid, self.res)
        live = seats.pk.read_json(path, {})
        # THE FIXTURE ONLY YIELDS live_scan, whose validity keys on ITS PID
        # BEING ALIVE — mono cannot move it. Drive a real PostToolUse through
        # the deliver hook to mint the mono-governed record this arm needs,
        # the same way test_posttool_and_live_scan_sources_do_not_clobber does.
        _holder = self.spawn(self.root)
        _hid = (_holder.pid, seats._get_pid_starttime(_holder.pid))
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=_hid):
            self.cmd_fd("deliver", ["--hook-json"], stdin=json.dumps({
                "hook_event_name": "PostToolUse", "session_id": self.sid,
                "cwd": self.wt, "tool_name": "Read",
                "agent_id": "agent-mono"}).encode())
        # RE-READ AFTER THE MINT, INTO THE NAME THE SELECTOR BELOW READS.
        # The snapshot taken above predates the PostToolUse, so the record
        # the deliver hook just wrote is not in it and every downstream use
        # -- the selector AND the legacy write -- was reading a file state
        # that no longer exists. Four arms failed on exactly this, and the
        # first repair fixed only ONE of them: it re-read into `live` at
        # every site, but two of the three name their snapshot `data`, so
        # the fresh value went into a variable nobody read while the
        # selector kept the stale one. Bind the re-read to the SAME name.
        live = seats.pk.read_json(path, {})
        # PICK BY THE PROPERTY, NEVER BY POSITION. records[0] is the
        # live_scan entry, whose validity keys on ITS PID BEING ALIVE
        # rather than on mono — so ageing cannot invalidate it and an
        # assertion written against it fails for a reason unrelated to
        # what the arm is about.
        key = next((k for k, v in (live.get("records") or {}).items()
                    if v.get("source") == "post_tool_use"), None)
        self.assertIsNotNone(key, "no mono-governed record in fixture")
        # UNCONDITIONAL positive control on the SAME observable, before any
        # manipulation: the fixture really does yield a readable result, so a
        # later assertion about it is about the property and not about the
        # setup having silently produced nothing.
        self.assertTrue(seats._get_delegation_activity(self.sid, self.res))
        legacy = os.path.join(chat.chat_dir(), os.path.basename(path))
        seats.pk.write_json(legacy, live)          # legacy: the LIVE record

        dead = seats.pk.read_json(path, {})        # new: same key, DEAD pid,
        dead["records"][key]["pid"] = 2 ** 22      # newer mono
        dead["records"][key]["mono"] = seats._now_mono() + 1
        seats.pk.write_json(path, dead)

        records = seats._get_delegation_activity(self.sid, self.res)
        pids = {r.get("pid") for r in records.values()}
        self.assertIn(live["records"][key]["pid"], pids,
                      "the LIVE candidate was evicted by a newer dead one")

    def test_a_collision_writes_nothing_back(self):  # noqa: VACUOUS_ASSERTION — assertEqual against a captured BEFORE snapshot is an exact-equality control, and the must-miss immediately after requires a migration to DO happen
        """No faithful single-key form exists, so migration REFUSES.

        Writing one side back would persist exactly the arbitrary pick the
        merge stopped making."""
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        child = self.spawn(self.wt, mark=self.sid)
        self.settle(child.pid, self.wt, mark=self.sid)
        self.assertEqual(self.guard({"session_id": self.sid})[0], 0)

        path = seats._delegation_activity_path(self.sid, self.res)
        data = seats.pk.read_json(path, {})
        # THE FIXTURE ONLY YIELDS live_scan, whose validity keys on ITS PID
        # BEING ALIVE — mono cannot move it. Drive a real PostToolUse through
        # the deliver hook to mint the mono-governed record this arm needs,
        # the same way test_posttool_and_live_scan_sources_do_not_clobber does.
        _holder = self.spawn(self.root)
        _hid = (_holder.pid, seats._get_pid_starttime(_holder.pid))
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=_hid):
            self.cmd_fd("deliver", ["--hook-json"], stdin=json.dumps({
                "hook_event_name": "PostToolUse", "session_id": self.sid,
                "cwd": self.wt, "tool_name": "Read",
                "agent_id": "agent-mono"}).encode())
        # RE-READ AFTER THE MINT, INTO THE NAME THE SELECTOR BELOW READS.
        # The snapshot taken above predates the PostToolUse, so the record
        # the deliver hook just wrote is not in it and every downstream use
        # -- the selector AND the legacy write -- was reading a file state
        # that no longer exists. Four arms failed on exactly this, and the
        # first repair fixed only ONE of them: it re-read into `live` at
        # every site, but two of the three name their snapshot `data`, so
        # the fresh value went into a variable nobody read while the
        # selector kept the stale one. Bind the re-read to the SAME name.
        data = seats.pk.read_json(path, {})
        # PICK BY THE PROPERTY, NEVER BY POSITION. records[0] is the
        # live_scan entry, whose validity keys on ITS PID BEING ALIVE
        # rather than on mono — so ageing cannot invalidate it and an
        # assertion written against it fails for a reason unrelated to
        # what the arm is about.
        key = next((k for k, v in (data.get("records") or {}).items()
                    if v.get("source") == "post_tool_use"), None)
        self.assertIsNotNone(key, "no mono-governed record in fixture")
        legacy = os.path.join(chat.chat_dir(), os.path.basename(path))
        other = seats.pk.read_json(path, {})
        # UNCONDITIONAL positive control on the SAME observable, before any
        # manipulation: the fixture really does yield a readable result, so a
        # later assertion about it is about the property and not about the
        # setup having silently produced nothing.
        self.assertTrue(seats._get_delegation_activity(self.sid, self.res))
        other["records"][key]["mono"] = other["records"][key]["mono"] - 7
        seats.pk.write_json(legacy, other)         # same key, DIFFERENT body

        before = seats.pk.read_json(path, {})
        seats._get_delegation_activity(self.sid, self.res)
        self.assertEqual(seats.pk.read_json(path, {}), before,
                         "a genuine collision was migrated anyway")
        # MUST-MISS: identical bodies are NOT a collision, and that read DOES
        # write forward — so the equality above is about collisions, not about
        # migration never happening.
        seats.pk.write_json(legacy, data)
        os.unlink(path)
        seats._get_delegation_activity(self.sid, self.res)
        self.assertTrue(os.path.exists(path))

    def test_both_candidates_reach_the_proof_instead_of_one_winner(self):  # noqa: VACUOUS_ASSERTION — assertIsNotNone(key) is an unconditional positive control proving the fixture yielded a mono-governed record, and assertEqual(len(monos), 2) is a positive count on the same observable, not an absence
        """No tiebreak here: the proof downstream is TWO-dimensional.

        It checks mono inside the window AND the pid still alive, so any
        single-axis pick at merge time discards a candidate the proof would
        have kept — a NEWER record whose pid is dead or recycled beats an
        OLDER live one, and max(mono) chooses exactly wrong. Both survive to
        the consumer, which ignores the key and evaluates each record."""
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        child = self.spawn(self.wt, mark=self.sid)
        self.settle(child.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        path = seats._delegation_activity_path(self.sid, self.res)
        data = seats.pk.read_json(path, {})
        # THE FIXTURE ONLY YIELDS live_scan, whose validity keys on ITS PID
        # BEING ALIVE — mono cannot move it. Drive a real PostToolUse through
        # the deliver hook to mint the mono-governed record this arm needs,
        # the same way test_posttool_and_live_scan_sources_do_not_clobber does.
        _holder = self.spawn(self.root)
        _hid = (_holder.pid, seats._get_pid_starttime(_holder.pid))
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=_hid):
            self.cmd_fd("deliver", ["--hook-json"], stdin=json.dumps({
                "hook_event_name": "PostToolUse", "session_id": self.sid,
                "cwd": self.wt, "tool_name": "Read",
                "agent_id": "agent-mono"}).encode())
        # RE-READ AFTER THE MINT, INTO THE NAME THE SELECTOR BELOW READS.
        # The snapshot taken above predates the PostToolUse, so the record
        # the deliver hook just wrote is not in it and every downstream use
        # -- the selector AND the legacy write -- was reading a file state
        # that no longer exists. Four arms failed on exactly this, and the
        # first repair fixed only ONE of them: it re-read into `live` at
        # every site, but two of the three name their snapshot `data`, so
        # the fresh value went into a variable nobody read while the
        # selector kept the stale one. Bind the re-read to the SAME name.
        data = seats.pk.read_json(path, {})
        # PICK BY THE PROPERTY, NEVER BY POSITION. records[0] is the
        # live_scan entry, whose validity keys on ITS PID BEING ALIVE
        # rather than on mono — so ageing cannot invalidate it and an
        # assertion written against it fails for a reason unrelated to
        # what the arm is about.
        key = next((k for k, v in (data.get("records") or {}).items()
                    if v.get("source") == "post_tool_use"), None)
        self.assertIsNotNone(key, "no mono-governed record in fixture")

        # same key in both roots, DIFFERENT content: legacy older, new newer
        legacy = os.path.join(chat.chat_dir(), os.path.basename(path))
        older = seats.pk.read_json(path, {})
        older["records"][key]["mono"] = \
            older["records"][key]["mono"] - 5
        seats.pk.write_json(legacy, older)

        records = seats._get_delegation_activity(self.sid, self.res)
        self.assertTrue(records)
        monos = sorted(r.get("mono") for r in records.values()
                       if r.get("agent") == data["records"][key]["agent"])
        self.assertEqual(len(monos), 2,
                         "one candidate was collapsed away before the proof")

        # MUST-MISS: identical content in both roots is NOT a collision and
        # must collapse to one, or this arm would pass on a merge that simply
        # never deduplicates anything.
        seats.pk.write_json(legacy, data)
        again = seats._get_delegation_activity(self.sid, self.res)
        self.assertEqual(len(again), len(data["records"]))

    def test_a_truthy_non_dict_envelope_does_not_crash_the_read(self):  # noqa: VACUOUS_ASSERTION — the unconditional assertTrue on a well-formed legacy root, immediately before the loop, is a positive control on the SAME observable (_get_delegation_activity's return); the rung cannot credit it because the in-loop assertions call the same producer again and each call mints a fresh producer identity
        """A corrupt file can be truthy AND non-dict — a JSON list or bare
        scalar — and `raw.get("v")` on that RAISES rather than reading as
        invalid. isinstance comes first."""
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        child = self.spawn(self.wt, mark=self.sid)
        self.settle(child.pid, self.wt, mark=self.sid)
        self.assertEqual(self.guard({"session_id": self.sid})[0], 0)

        path = seats._delegation_activity_path(self.sid, self.res)
        good = seats.pk.read_json(path, {})
        self.assertTrue(good.get("records"), "fixture produced no records")
        legacy = os.path.join(chat.chat_dir(), os.path.basename(path))

        # UNCONDITIONAL positive control on the same observable, BEFORE any
        # corruption: the read answers when both roots are well-formed. The
        # assertions below sit inside a loop the rung cannot prove runs, and
        # without this one a read that always returned truthy would pass.
        seats.pk.write_json(legacy, good)
        self.assertTrue(seats._get_delegation_activity(self.sid, self.res))

        for body in ("[1, 2, 3]", '"a string"', "42"):
            with open(legacy, "w", encoding="utf-8") as fh:
                fh.write(body)
            # the GOOD root still answers: a corrupt sibling is skipped, not
            # fatal, and not a reason to lose the records that are fine
            self.assertTrue(seats._get_delegation_activity(self.sid, self.res),
                            body)

    def test_an_old_only_read_migrates_its_file_forward(self):
        """The round-one commit PROMISED each read migrates itself forward.

        The first write-back diffed against the MERGED set, so a read whose
        records all came from the legacy root compared clean and never wrote
        the new file — the promise was in the message and not in the code."""
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        child = self.spawn(self.wt, mark=self.sid)
        self.settle(child.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        path = seats._delegation_activity_path(self.sid, self.res)
        data = seats.pk.read_json(path, {})
        self.assertTrue(data.get("records"), "fixture produced no records")

        # OLD-ONLY: the records exist ONLY in the legacy root
        legacy = os.path.join(chat.chat_dir(), os.path.basename(path))
        seats.pk.write_json(legacy, data)
        os.unlink(path)
        self.assertFalse(os.path.exists(path))

        records = seats._get_delegation_activity(self.sid, self.res)
        self.assertTrue(records, "old-only read returned nothing")
        self.assertTrue(os.path.exists(path),
                        "old-only read did not migrate its file forward")
        self.assertEqual(set(seats.pk.read_json(path, {}).get("records", {})),
                         set(records))

    def test_a_valid_legacy_record_survives_an_invalid_new_one(self):  # noqa: VACUOUS_ASSERTION — the unconditional assertIn on _get_delegation_activity immediately before the both-roots-broken write is the positive control on the SAME observable the assertNotIn reads; the rung cannot credit it because it asserts on the call expression rather than a bound name
        """Merge by VALIDITY, never by which root the record sat in.

        Both roots can hold the same record key while relaunched and
        un-relaunched writers coexist. A plain new-wins update discards a
        VALID legacy record whenever the new root holds an invalid one under
        that key — evidence loss dressed as a merge, and invisible to any
        arm that only checks the merged COUNT."""
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        child = self.spawn(self.wt, mark=self.sid)
        self.settle(child.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        # The fixture alone yields only live_scan, whose validity keys on ITS
        # PID BEING ALIVE — mono cannot move it, so ageing proves nothing about
        # it. Mint the mono-governed record this arm's lever actually reaches.
        _holder = self.spawn(self.root)
        _hid = (_holder.pid, seats._get_pid_starttime(_holder.pid))
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=_hid):
            self.cmd_fd("deliver", ["--hook-json"], stdin=json.dumps({
                "hook_event_name": "PostToolUse", "session_id": self.sid,
                "cwd": self.wt, "tool_name": "Read",
                "agent_id": "agent-mono"}).encode())

        path = seats._delegation_activity_path(self.sid, self.res)
        data = seats.pk.read_json(path, {})
        self.assertTrue(data.get("records"), "fixture produced no records")
        mono_key = next((k for k, v in data["records"].items()
                         if v.get("source") == "post_tool_use"), None)
        self.assertIsNotNone(mono_key, "no mono-governed record in fixture")

        # LEGACY root: the same records, still valid, where a pre-namespace
        # writer would have left them
        legacy = os.path.join(chat.chat_dir(), os.path.basename(path))
        seats.pk.write_json(legacy, data)

        # NEW root: the same KEYS, made STRUCTURALLY invalid — the only axis
        # this merge has. _activity_record_valid is structural ONLY; the mono
        # window AND the pid-alive proof both run at the CONSUMER, on purpose,
        # so that a candidate the proof would have kept is never discarded
        # here. Ageing `mono` therefore does NOT make a record invalid to the
        # merge: the earlier form of this arm aged it and then asserted the
        # merge had dropped it, which asserted a lever this function does not
        # hold. A non-numeric `mono` is what it actually rejects.
        stale = seats.pk.read_json(path, {})
        for key in stale["records"]:
            stale["records"][key]["mono"] = "not-a-number"
        seats.pk.write_json(path, stale)

        records = seats._get_delegation_activity(self.sid, self.res)
        self.assertTrue(records, "valid legacy records were discarded by "
                                 "invalid new-root records under the same key")
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, before the manipulation:
        # the colliding key IS present while the legacy root is still valid.
        # Without this the assertNotIn below passes just as happily on an
        # empty fixture, which would make the must-miss prove nothing.
        self.assertIn(mono_key,
                      seats._get_delegation_activity(self.sid, self.res) or {},
                      "fixture never carried the colliding key forward")
        # MUST-MISS: BOTH roots structurally invalid. Writing only the legacy
        # root left the FIRST call's migrated-forward records live in the new
        # root, so this control was green for a reason that had nothing to do
        # with the property — write the broken envelope to BOTH.
        seats.pk.write_json(legacy, stale)
        seats.pk.write_json(path, stale)
        # ASSERT ON THE COLLIDING KEY, NOT THE WHOLE RESULT: a survivor under
        # a DIFFERENT key is not evidence against this property.
        left = seats._get_delegation_activity(self.sid, self.res) or {}
        self.assertNotIn(mono_key, left,
                         "the structurally-invalid colliding key survived in "
                         "both roots")

    def test_posttool_and_live_scan_sources_do_not_clobber(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        child = self.spawn(self.wt, mark=self.sid)
        self.settle(child.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        event = {
            "hook_event_name": "PostToolUse", "session_id": self.sid,
            "cwd": self.wt, "tool_name": "Read",
        }
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            for agent in ("agent-a", "agent-b"):
                self.cmd_fd("deliver", ["--hook-json"],
                            stdin=json.dumps(dict(event, agent_id=agent)).encode())
        records = seats._get_delegation_activity(self.sid, self.res)
        self.assertEqual(
            sorted(r["source"] for r in records.values()),
            ["live_scan", "post_tool_use", "post_tool_use"])

        stop = {"hook_event_name": "SubagentStop", "session_id": self.sid,
                "cwd": self.wt, "agent_id": "agent-b"}
        self.cmd_fd("delegation-stop", ["--hook-json"],
                    stdin=json.dumps(stop).encode())
        records = seats._get_delegation_activity(self.sid, self.res)
        self.assertEqual({r.get("agent") for r in records.values()},
                         {None, "agent-a"})

    def test_rebind_and_rollback_purge_posttool_activity(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        os.environ["HELM_CHAT_NAME"] = "alice"
        holder = self.spawn(self.root)
        holder_id = (holder.pid, seats._get_pid_starttime(holder.pid))
        path_a = seats._delegation_activity_path(self.sid, self.res)
        payload = {
            "hook_event_name": "PostToolUse", "session_id": self.sid,
            "cwd": self.wt, "tool_name": "Read",
            "agent_id": "agent-rebind",
        }
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(payload).encode())
        self.assertTrue(os.path.exists(path_a))

        sid_b = self.sid + "-new"
        self.assertEqual(seats.rebind_claim_sessions("alice", self.sid, sid_b),
                         [self.res])
        self.assertFalse(os.path.exists(path_a))
        payload["session_id"] = sid_b
        path_b = seats._delegation_activity_path(sid_b, self.res)
        with mock.patch.object(seats, "_enclosing_claude_holder",
                               return_value=holder_id):
            self.cmd_fd("deliver", ["--hook-json"],
                        stdin=json.dumps(payload).encode())
        self.assertTrue(os.path.exists(path_b))
        self.assertEqual(seats.rollback_claim_sessions(
            "alice", self.sid, sid_b, [self.res]), 1)
        self.assertFalse(os.path.exists(path_b))

    def test_interval_sampling_refuses_recycled_or_expired_pid(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        sub = self.spawn(self.root, mark=self.sid, argv=(
            "python3", "-c",
            "import os, time; os.chdir('%s'); open('%s/ready', 'w').close(); time.sleep(0.5)"
            % (self.wt, self.tmp)))
        self.settle(sub.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        # Wait for sub process to exit completely
        sub.wait()
        for _ in range(50):
            if not seats._is_pid_alive(sub.pid):
                break
            time.sleep(0.02)

        # Exited PID no longer passes delegation check, fail-closed BLOCK
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn(self.res, err)          # the held lane is NAMED
        self.assertIn("act per line, then stop:", err)

    def test_interval_sampling_refuses_missing_or_mismatched_starttime(self):
        seats.claim(self.res, "alice", ttl=120, session=self.sid)
        sub = self.spawn(self.wt, mark=self.sid)
        self.settle(sub.pid, self.wt, mark=self.sid)

        real_st = seats._get_pid_starttime(sub.pid)
        self.assertIsInstance(real_st, int)

        # 1. Missing starttime with require_starttime=True -> False
        self.assertFalse(seats._is_pid_alive(sub.pid, starttime=None, require_starttime=True))

        # 2. Non-integer starttime -> False
        self.assertFalse(seats._is_pid_alive(sub.pid, starttime="invalid", require_starttime=True))

        # 3. Mismatched recycled starttime -> False
        self.assertFalse(seats._is_pid_alive(sub.pid, starttime=real_st + 9999, require_starttime=True))

        # 4. Correct starttime -> True
        self.assertTrue(seats._is_pid_alive(sub.pid, starttime=real_st, require_starttime=True))

    def test_release_and_lease_nonce_clean_up_delegation_activity(self):
        ok, _msg, lease_a = seats.claim(self.res, "alice", ttl=120, session=self.sid)
        self.assertTrue(ok)
        self.assertIsNotNone(lease_a)

        sub = self.spawn(self.wt, mark=self.sid)
        self.settle(sub.pid, self.wt, mark=self.sid)

        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        act_file = seats._delegation_activity_path(self.sid, self.res)
        self.assertTrue(os.path.exists(act_file))

        act = seats._get_delegation_activity(self.sid, self.res)
        self.assertIsNotNone(act)
        self.assertEqual(act["live_scan"].get("lease"), lease_a)

        # Release lease -> unlinks delegation activity record physically
        own = seats.own_leases("alice")
        token = own.get(self.res)
        ok_rel, _m = seats.release(self.res, "alice", lease=token, session=self.sid)
        self.assertTrue(ok_rel)

        self.assertFalse(os.path.exists(act_file))
        act_after = seats._get_delegation_activity(self.sid, self.res)
        self.assertIsNone(act_after)

    def test_reacquire_lease_nonce_mismatch_unlinks_stale_activity(self):
        ok, _m, lease_a = seats.claim(self.res, "alice", ttl=120, session=self.sid)
        self.assertTrue(ok)

        sub = self.spawn(self.wt, mark=self.sid)
        self.settle(sub.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)

        act_file = seats._delegation_activity_path(self.sid, self.res)
        self.assertTrue(os.path.exists(act_file))

        # Stale file on disk carries lease_a
        act_raw = seats.pk.read_json(act_file, {})
        self.assertEqual(act_raw["records"]["live_scan"].get("lease"), lease_a)

        # Simulate claim lease being re-granted / rotated to lease_b in claims.json (ABA scenario)
        lease_b = os.urandom(8).hex()
        c = seats.pk.read_json(seats.claims_path(), {})
        c[self.res]["lease"] = lease_b
        seats.pk.write_json(seats.claims_path(), c)

        # _get_delegation_activity detects lease_a != lease_b mismatch, returns None, and unlinks file
        act = seats._get_delegation_activity(self.sid, self.res)
        self.assertIsNone(act)
        self.assertFalse(os.path.exists(act_file))

    def test_corrupt_json_or_corrupt_lease_fails_closed_and_unlinks(self):
        ok, _m, lease_a = seats.claim(self.res, "alice", ttl=120, session=self.sid)
        self.assertTrue(ok)

        act_file = seats._delegation_activity_path(self.sid, self.res)
        with open(act_file, "w") as f:
            f.write("{corrupt json[")

        self.assertTrue(os.path.exists(act_file))
        act = seats._get_delegation_activity(self.sid, self.res)
        self.assertIsNone(act)
        self.assertFalse(os.path.exists(act_file))

        # Corrupt lease "None" string in claim
        c = seats.pk.read_json(seats.claims_path(), {})
        c[self.res]["lease"] = "None"
        seats.pk.write_json(seats.claims_path(), c)

        self.assertIsNone(seats._get_claim_lease(self.res))

    def test_resource_sha256_hash_prevents_slug_collisions(self):
        seats.chat._ensure_dir()
        res_dot = "worktree:p:lane.a"
        res_dash = "worktree:p:lane-a"

        path_dot = seats._delegation_activity_path(self.sid, res_dot)
        path_dash = seats._delegation_activity_path(self.sid, res_dash)

        self.assertNotEqual(path_dot, path_dash)

        with open(path_dot, "w") as f:
            f.write("{}")
        with open(path_dash, "w") as f:
            f.write("{}")

        seats._unlink_delegation_activity(res_dot)
        self.assertFalse(os.path.exists(path_dot))
        self.assertTrue(os.path.exists(path_dash))

        seats._unlink_delegation_activity(res_dash)
        self.assertFalse(os.path.exists(path_dash))

    def test_kill_switch_restores_the_strict_block(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        p = self.spawn(self.wt, mark=self.sid)
        self.settle(p.pid, self.wt, mark=self.sid)
        os.environ["HELM_STOP_GUARD_DELEGATION"] = "0"
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn(self.res, err)          # the held lane is NAMED
        self.assertIn("act per line, then stop:", err)
        self.assertNotIn("delegated", err)

    def test_non_lane_leases_never_consult_proc_and_block(self):
        seats.claim("worktree-main", "alice", ttl=60, session=self.sid)
        seats.claim("worktree:other:lane-x", "alice", ttl=60, session=self.sid)
        p = self.spawn(self.wt, mark=self.sid)       # live delegate, but no
        self.settle(p.pid, self.wt, mark=self.sid)   # lease NAMES its room
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn("worktree-main", err)          # not lane-shaped
        self.assertIn("worktree:other:lane-x", err)  # foreign project

    def test_mixed_leases_block_on_the_bare_one_and_account_for_the_exempt(self):
        """BOTH OWNER OBSERVATIONS HOLD AT ONCE, and this arm is where they meet.

        Claude Code renders every exit-2 stop hook as a red "Stop hook error:",
        so reassurance printed beside the one real demand glows red as an error.
        That is why the exempt lease's ADVISORY rides the ALLOW exit — and while
        the advisory was its ONLY line, the lane had nothing at all on a stop
        that blocks.

        THAT ABSENCE IS THE DEFECT task/2388 MEASURED: a seat holding three
        leases, the sermon naming two, and the third — nine dirty files — gone.
        The sermon header promises exactly two shapes per lane (a measurable
        lane names its command, an unprovable one says why and offers none);
        absence is a third, and it reads as ALREADY RELEASED, the one reading
        that makes a seat walk away from uncommitted work.

        So accounting and reassurance separate. The red block gets ONE scannable
        line per exempt lane — its measured state word, NO ACTION OWED, no
        command — and the reassurance SENTENCE stays on the allow exit.
        `..._keeps_advisories_OFF_the_exit2_emission` pins that second half and
        stays green against this."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        seats.claim("db-migration", "alice", ttl=60, session=self.sid)
        p = self.spawn(self.wt, mark=self.sid)
        self.settle(p.pid, self.wt, mark=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn("db-migration", err)           # the bare lease blocks
        # 2388: the exempt lane is ACCOUNTED FOR, so its absence can no longer
        # be read as a release.
        self.assertIn(self.res + " ", err)
        self.assertIn("NO ACTION OWED", err)
        self.assertIn("LIVE DELEGATE", err)          # the measured state word
        # ...and the no-red-reassurance ruling still holds: no prose, no command
        self.assertNotIn("live delegated build", err)
        self.assertNotIn("lease retained", err)
        self.assertNotIn("release " + self.res, err)

    # --- ONE TABLE READ PER STOP, NOT ONE PER HELD CLAIM ------------------
    #
    # The exemption asks the process table one question per lane -- who is
    # standing in THIS room -- and everything before that comparison is
    # identical across lanes. Run inside the claims loop it is O(leases x
    # processes). The two arms below are the two halves the cost change owes:
    # the shared scan must answer every lane exactly as its own walk did, and
    # the walk count must stop tracking the lease count.

    def extra_lane(self, name):
        """A second, third, ... real lane room off the same scratch repo."""
        path = os.path.join(self.root + "-wt", name)
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane/" + name, path], check=True,
                       capture_output=True, timeout=30)
        return path, "worktree:proj:" + name

    def test_a_SHARED_scan_answers_every_lane_as_its_OWN_walk_did(self):  # noqa: VACUOUS_ASSERTION — the two loops are proven non-empty before either runs by the unconditional `assertEqual(len(rooms), 4)` and `assertEqual(len(live), 2)`, and the mixed set means neither loop's body can be satisfied by a uniformly blind reader
        """EQUIVALENCE, lane for lane, over a MIXED set -- and the mix is the
        arm. Four rooms: one empty, one holding a process of ours that is not
        marked, and two holding real marked delegates. A shared map that
        answered None everywhere would agree with nothing; one that answered
        the same pid everywhere would agree with nothing either. Only a map
        that reproduces the per-lane split can pass.

        THE PROOF MUST COME FROM THE LIVE SCAN, asserted by its own shape.
        `_delegated_build` returns an int pid for the table read and a
        SENTENCE for the interval record, and the first call of a pair writes
        an interval record the second could then ride -- so comparing the two
        spellings without pinning which path produced them would let the cache
        the control wrote paper over a divergence in the thing being tested.

        AND THE MAP ITSELF IS READ, not only the answer composed from it: the
        shared scan's own candidate list for a proven room must be exactly the
        delegate's pid, which is a direct assertion on the new producer rather
        than on a value four steps downstream."""
        rooms = [(self.wt, self.res)]
        for name in ("scan-b", "scan-c", "scan-d"):
            rooms.append(self.extra_lane(name))
        for _path, res in rooms:
            ok, msg, _l = seats.claim(res, "alice", ttl=120, session=self.sid)
            self.assertTrue(ok, msg)
        # room 0: empty. room 1: ours but UNMARKED. rooms 2 and 3: delegates.
        unmarked = self.spawn(rooms[1][0])
        self.settle(unmarked.pid, rooms[1][0])
        live = {}
        for path, res in rooms[2:]:
            child = self.spawn(path, mark=self.sid)
            self.settle(child.pid, path, mark=self.sid)
            live[res] = child.pid

        # THE FIXTURE ITSELF, ASSERTED BEFORE ANY LOOP READS IT. Both loops
        # below iterate slices of `rooms` and index `live`; if either were
        # short, their bodies would pass by never running. These two lines are
        # what make every assertion inside them a measurement.
        self.assertEqual(len(rooms), 4)
        self.assertEqual(len(live), 2)
        shared = seats.proc_scan()
        self.assertTrue(shared.ok, "the fixture's own table was unreadable")

        own, threaded = {}, {}
        for path, res in rooms:
            own[res] = seats._delegated_build(res, self.sid)
            threaded[res] = seats._delegated_build(res, self.sid, scan=shared)
        self.assertEqual(own, threaded,
                         "the shared scan changed an answer: %r vs %r"
                         % (own, threaded))

        # THE MIX IS REAL, asserted unconditionally on both sides so the
        # equality above cannot be satisfied by a uniformly blind reader.
        for _path, res in rooms[:2]:
            self.assertIsNone(own[res], res)
            self.assertIsNone(threaded[res], res)
        for path, res in rooms[2:]:
            self.assertIsNotNone(own[res], res)
            self.assertEqual(own[res][0], live[res],
                             "the per-lane walk named the wrong pid for %s"
                             % res)
            self.assertIsInstance(
                threaded[res][0], int,
                "the threaded answer for %s came from the INTERVAL record, "
                "not the table read this arm is about: %r"
                % (res, threaded[res]))
            self.assertEqual(threaded[res][0], live[res], res)
            # the map itself, read directly rather than four steps downstream
            want = os.path.realpath(path).rstrip(os.sep)
            self.assertEqual(shared.candidates(want), [live[res]],
                             "the shared map's own candidate list for %s is "
                             "wrong: %r" % (res, shared.candidates(want)))
        # AND THE UNMARKED ROOM IS IN THE MAP but proves nothing -- the split
        # between "the table saw it" and "it is proof" is the poach-shield,
        # and hoisting the scan must not move it.
        idle_want = os.path.realpath(rooms[1][0]).rstrip(os.sep)
        self.assertIn(unmarked.pid, shared.candidates(idle_want),
                      "the scan dropped a process it must still see")

    def test_a_pid_that_DIES_after_the_scan_is_dropped_at_the_moment_of_use(self):
        """THE HALF A SNAPSHOT COULD GET WRONG, and the one my own mutation
        run proved I had asserted without testing.

        Hoisting the walk turns N samples of the world into one, and the
        dangerous direction is a pid that was in the room when the scan ran
        and is gone when the lane is judged. Trusted from the snapshot it
        would be laundered into a live-delegate exemption for a delegate that
        has finished -- an exemption granted on a process that no longer
        exists, which is the fleet-wide un-guarding this predicate exists to
        refuse.

        THE POACH-SHIELD IS NOT WHAT THIS TESTS, and the distinction is why
        the arm exists at all. The shield is the map's own KEY: a pid whose
        cwd was never the room is not in the bucket, and deleting the
        comparison inside `candidates` leaves every poach arm green. What
        `candidates` adds on top is FRESHNESS, and nothing was measuring it --
        found by mutating the line and watching 37 tests stay green.

        THE RAW BUCKET IS READ ALONGSIDE THE ANSWER. Asserting only that the
        candidate list is empty would also pass if the scan had never seen the
        child; the arm is the DISAGREEMENT between the two, in one call."""
        ok, msg, _l = seats.claim(self.res, "alice", ttl=120, session=self.sid)
        self.assertTrue(ok, msg)
        child = self.spawn(self.wt, mark=self.sid)
        self.settle(child.pid, self.wt, mark=self.sid)
        want = os.path.realpath(self.wt).rstrip(os.sep)

        shared = seats.proc_scan()
        self.assertTrue(shared.ok)
        # CONTROL, unconditional and on the SAME observable: while the child
        # lives the scan sees it AND `candidates` returns it, so the empty
        # list below is a measured drop and not a reader that never speaks.
        self.assertEqual(shared.candidates(want), [child.pid])
        self.assertIsNotNone(seats._delegated_build(self.res, self.sid,
                                                    scan=shared))

        child.kill()
        child.wait(timeout=10)
        # THE SNAPSHOT STILL CARRIES THE DEAD PID -- that is the whole hazard,
        # asserted rather than assumed, so the drop below has something to
        # drop.
        self.assertIn(child.pid, shared._by_cwd.get(want, []),
                      "the fixture did not reproduce a stale snapshot")
        self.assertEqual(shared.candidates(want), [],
                         "a dead pid survived into the candidate list")
        # AND NO EXEMPTION IS COMPOSED FROM IT. The interval record written by
        # the control call above is keyed on a pid that must now read dead, so
        # this also pins that the live-scan drop is not quietly rescued by the
        # record the control itself wrote.
        self.assertIsNone(seats._delegated_build(self.res, self.sid,
                                                 scan=shared),
                          "a finished delegate still exempted its lane")

    def test_the_TABLE_IS_WALKED_ONCE_however_many_leases_are_held(self):  # noqa: VACUOUS_ASSERTION — the delta assertion's positive pole is unconditional and on the SAME counter: `assertGreaterEqual(control, len(rooms))` proves the instrumented listdir fired once per lease in the control drive before the shipped drive is read at all
        """COST, counted at the mutation site rather than timed.

        A wall-clock figure on a shared box is an anecdote; the invariant is
        that the number of table walks stops being a function of the lease
        count. Twelve lane leases held in one stop, and the proc root is
        listed ONCE.

        THE COUNT IS TAKEN AT `os.listdir` OF THE PROC ROOT, which every walk
        performs exactly once. Counting calls to the producer by name would
        miss the path that matters: the claims rung binds `proc_scan` at
        import, so a spy on the producer's own module would watch a name the
        rung never reads.

        THE COUNTER IS NOT EXCLUSIVE TO THIS RUNG, and the arm is written
        around that rather than against it. A stop lists the proc root once
        more for the claims census, MEASURED at 13 walks for 12 leases where
        the exemption alone predicts 12. So the assertion is on the DELTA
        between the two shapes, which cancels every constant term whatever it
        turns out to be and is exactly the invariant being claimed: the walk
        count stops tracking the lease count.

        THE CONTROL IS THE SAME COUNTER ON THE SAME STOP with the scan not
        usable -- one walk per lease -- so the collapse is measured against a
        drive that really did pay per lease, not against a rung that never
        ran."""
        rooms = [(self.wt, self.res)]
        for i in range(11):
            rooms.append(self.extra_lane("scan-n%d" % i))
        for _path, res in rooms:
            ok, msg, _l = seats.claim(res, "alice", ttl=120, session=self.sid)
            self.assertTrue(ok, msg)
        real_listdir = os.listdir

        def counting(path, *a, **k):
            if str(path) == "/proc":
                walks.append(1)
            return real_listdir(path, *a, **k)

        # THE CONTROL FIRST: the pre-hoist shape, driven through the same rung
        # by making the threaded scan unusable to every lane. A scan whose
        # root is not the resolved one is rebuilt per lane BY CONTRACT, which
        # is exactly the O(leases) behaviour being retired.
        walks = []
        other_root = os.path.join(self.tmp, "not-proc")
        with mock.patch.object(os, "listdir", side_effect=counting), \
                mock.patch.object(seats_stop_claims, "proc_scan",
                                  side_effect=lambda *a, **k:
                                  seats.ProcScan(True, other_root)):
            blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        control = len(walks)
        self.assertGreaterEqual(
            control, len(rooms),
            "control: the per-lane shape must walk at least once per lease "
            "(%d leases, %d walks)" % (len(rooms), control))
        # POSITIVE POLE: the rung really did speak about these leases.
        self.assertIn(self.res, " ".join(blocks))

        # THE SHIPPED SHAPE, same stop, same counter.
        for name in os.listdir(chat.chat_dir()):
            if seats.LEASE_LATCH in name:
                os.unlink(os.path.join(chat.chat_dir(), name))
        walks = []
        with mock.patch.object(os, "listdir", side_effect=counting):
            blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        self.assertIn(self.res, " ".join(blocks))
        shipped = len(walks)
        self.assertGreaterEqual(shipped, 1,
                                "the shipped shape walked the table ZERO "
                                "times -- the exemption never ran")
        self.assertEqual(
            control - shipped, len(rooms) - 1,
            "%d lane leases: control took %d walks, shipped took %d. The "
            "exemption must collapse from one walk per lease to one per stop, "
            "so the delta is exactly %d."
            % (len(rooms), control, shipped, len(rooms) - 1))


class DelegationHolderAncestryTest(SeatsBase):
    def setUp(self):
        super().setUp()
        self.proc = os.path.join(self.tmp, "holder-proc")
        os.makedirs(self.proc)

    def plant(self, pid, ppid, starttime, comm, stat_comm=None):
        pdir = os.path.join(self.proc, str(pid))
        os.makedirs(pdir)
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(starttime), "0"]
        with open(os.path.join(pdir, "stat"), "w") as f:
            f.write("%d (%s) %s" %
                    (pid, stat_comm or comm, " ".join(fields)))
        with open(os.path.join(pdir, "comm"), "w") as f:
            f.write(comm + "\n")

    def test_walks_wrappers_and_parses_process_controlled_comm(self):
        self.plant(100, 90, 1000, "python3", stat_comm="odd ) worker")
        self.plant(90, 80, 900, "timeout")
        self.plant(80, 70, 800, "sh")
        self.plant(70, 1, 700, "claude")  # noqa: SEAT_NAME — the /proc comm the holder walk is ABOUT; moved whole from tests/test_seats.py
        self.assertEqual(seats._proc_stat_link(100, self.proc), (1000, 90))
        self.assertEqual(seats._enclosing_claude_holder(
            start_pid=100, proc_dir=self.proc), (70, 700))

    def test_nonblocking_evidence_lock_returns_unknown_immediately(self):  # noqa: VACUOUS_ASSERTION — the pole IS present and unconditional: the first `with seats._flocked(...)` block asserts f is NOT None on a FREE lock, same helper, same observable, before the held-lock refusal below. The rung appears to require the control inside the same block as the absence, so it cannot see a two-state discrimination expressed as two sequential context managers
        import fcntl
        path = os.path.join(self.tmp, "claims.lock")
        # UNCONDITIONAL POSITIVE CONTROL, on the same observable and the same
        # helper: with NOBODY holding the lock, _flocked hands back a real
        # handle. Without this the arm below asserts an absence against a
        # helper that could return None for any reason at all — a _flocked
        # that never acquired anything would satisfy it perfectly.
        with seats._flocked(path, blocking=False) as free:
            self.assertIsNotNone(free.f, "the non-blocking acquire returns "
                                         "None even when the lock is FREE, so "
                                         "the refusal below proves nothing")
        with open(path, "a") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            started = time.monotonic()
            with seats._flocked(path, blocking=False) as got:
                self.assertIsNone(got.f)
            # A HANG BACKSTOP, NOT THE PROOF. The lock is held for this entire
            # block, so a BLOCKING acquire could not have returned at all —
            # the assertIsNone above proves non-blocking on its own, at any
            # speed. The 0.1 was hang-detection wearing a correctness
            # assertion's clothes, and a scheduler hiccup on a loaded box can
            # spend 100ms without anything being wrong. The clock stays only
            # so a genuine hang FAILS instead of wedging the suite.
            self.assertLess(time.monotonic() - started, 5)

    def test_recheck_rejects_reused_intermediate_generation(self):
        self.plant(100, 90, 1000, "python3")
        self.plant(90, 70, 900, "timeout")
        self.plant(70, 1, 700, "claude")  # noqa: SEAT_NAME — the /proc comm the holder walk is ABOUT; moved whole from tests/test_seats.py
        real = seats._proc_stat_link
        seen = {}

        def changed(pid, proc_dir="/proc"):
            link = real(pid, proc_dir=proc_dir)
            seen[pid] = seen.get(pid, 0) + 1
            if pid == 90 and seen[pid] > 1:
                return link[0] + 1, link[1]
            return link

        with mock.patch.object(seats, "_proc_stat_link", side_effect=changed):
            self.assertIsNone(seats._enclosing_claude_holder(
                start_pid=100, proc_dir=self.proc))
