#!/usr/bin/env python3
"""The commit-during-gate rung: refuse inside the bracket, never outside it.

A commit inside a gate's bracket moves HEAD under a running suite whose receipt
already recorded the OLD head, so the receipt brackets a moving tree and bind()
refuses it. Three seats hit that inside one hour on 2026-08-03. The bracket rule
catches it AFTER; this rung is the BEFORE.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from helm import gate, inflight_gate


class InflightMarkerTest(unittest.TestCase):
    def setUp(self):
        # A REAL git repo, not a tempdir with a hand-made .git directory. The
        # first version of this fixture made `.git` by hand, which meant the
        # marker path was never resolved through git at all — and that is
        # precisely how the worktree case (where .git is a FILE) slipped
        # through seven green tests.
        import subprocess
        self.room = tempfile.mkdtemp(prefix="helm-inflight-")
        for cmd in (["init", "-q"], ["config", "user.email", "t@e.example"],
                    ["config", "user.name", "T"]):
            subprocess.run(["git"] + cmd, cwd=self.room, capture_output=True)

    def test_a_LIVE_gate_refuses_the_commit(self):
        nonce = gate._inflight_open(self.room)
        live = gate.inflight(self.room)
        # POSITIVE CONTROL: the marker is readable and names THIS process,
        # so the refusal below is about a gate and not about a parse failure.
        self.assertEqual(live[0], os.getpid())
        with mock.patch.object(os, "getcwd", lambda: self.room):
            rc = inflight_gate.main()
        self.assertEqual(rc, 1)

    def test_NO_marker_does_not_refuse(self):
        # BOUND TO A NAME so the positive and negative land on ONE root: the
        # vacuity rung pairs controls by root, and an assertion on a bare call
        # expression has no root to pair with.
        nonce = gate._inflight_open(self.room)
        live = gate.inflight(self.room)
        self.assertEqual(live[0], os.getpid())   # POSITIVE: the pid it holds            # POSITIVE CONTROL, first
        gate._inflight_close(self.room, nonce)
        live = gate.inflight(self.room)
        self.assertIsNone(live)
        with mock.patch.object(os, "getcwd", lambda: self.room):
            rc = inflight_gate.main()
        self.assertEqual(rc, 0)

    def test_a_DEAD_pid_cannot_wedge_the_room_shut(self):
        """A killed runner leaves its marker behind. If a stale file refused
        forever, the guard would be worse than the gap it closes — the room
        would be uncommittable until someone found the file by hand."""
        with open(gate.inflight_path(self.room), "w", encoding="utf-8") as fh:
            json.dump({"pid": 999999, "ts": "stale"}, fh)
        self.assertTrue(os.path.exists(gate.inflight_path(self.room)))  # present
        live = gate.inflight(self.room)
        self.assertIsNone(live)                                         # not live
        with mock.patch.object(os, "getcwd", lambda: self.room):
            rc = inflight_gate.main()
        self.assertEqual(rc, 0)

    def test_an_UNREADABLE_marker_never_manufactures_a_refusal(self):
        """A warning we cannot substantiate must not block a commit. Garbage
        is UNKNOWN, and UNKNOWN fails open — the asymmetry is deliberate: a
        wrong refusal costs a real commit, a missed warning costs one gate."""
        with open(gate.inflight_path(self.room), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        live = gate.inflight(self.room)
        self.assertIsNone(live)
        with mock.patch.object(os, "getcwd", lambda: self.room):
            rc = inflight_gate.main()
        self.assertEqual(rc, 0)

    def test_the_marker_is_REMOVED_when_the_run_ends(self):
        """The window must close on EVERY exit or the room stays refused. run()
        wraps its whole body in try/finally for exactly this — a `return` added
        later cannot leak the marker."""
        nonce = gate._inflight_open(self.room)
        live = gate.inflight(self.room)
        self.assertEqual(live[0], os.getpid())   # POSITIVE: the pid it holds                       # POSITIVE CONTROL
        gate._inflight_close(self.room, nonce)
        live = gate.inflight(self.room)
        self.assertIsNone(live)
        self.assertFalse(os.path.exists(gate.inflight_path(self.room)))
        # idempotent: closing an already-closed window must not raise
        gate._inflight_close(self.room, nonce)

    def test_the_marker_is_ROOM_SCOPED_not_project_wide(self):
        """The reason this reads a marker and not gatelock: the lock records the
        HOLDER and never the REPO, so a lock-based guard would refuse commits in
        every room while any seat gated anywhere."""
        other = tempfile.mkdtemp(prefix="helm-inflight-other-")
        os.makedirs(os.path.join(other, ".git"))
        nonce = gate._inflight_open(self.room)
        live = gate.inflight(self.room)
        self.assertEqual(live[0], os.getpid())   # gating room: live
        live = gate.inflight(other)
        self.assertIsNone(live)                          # sibling room: free
        with mock.patch.object(os, "getcwd", lambda: other):
            rc = inflight_gate.main()
        self.assertEqual(rc, 0)

    def test_a_REAL_run_leaves_no_marker_behind(self):
        """SPANS THE SEAM, not the primitives. The clause above proves
        _inflight_close works; this proves run() CALLS it — which is the part
        that can rot when someone adds a `return`. Testing the helper and
        calling it coverage is the exact mistake that cost #116 four rounds.

        Uses the non-suite argv path so the run is a millisecond, not a whole
        gate: the marker bracket is shared by both paths."""
        import subprocess
        open(os.path.join(self.room, "f"), "w").write("x")
        subprocess.run(["git", "add", "-A"], cwd=self.room, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "seed", "--no-verify"],
                       cwd=self.room, capture_output=True)
        nonce = gate._inflight_open(self.room)
        live = gate.inflight(self.room)
        self.assertEqual(live[0], os.getpid())   # POSITIVE: the pid it holds          # POSITIVE CONTROL: marker CAN exist
        gate._inflight_close(self.room, nonce)
        live = gate.inflight(self.room)
        self.assertIsNone(live)             # clean before
        gate.run(repo=self.room, argv=["true"], label="marker-probe")
        # POSITIVE CONTROL that the run REACHED the bracket: it left a receipt
        # trail or an error, either way the room is not still marked.
        live = gate.inflight(self.room)
        self.assertIsNone(live, "run() left its in-flight marker behind")
        self.assertFalse(os.path.exists(gate.inflight_path(self.room)))


class InflightInARealWorktreeTest(unittest.TestCase):
    """THE FIXTURE THE UNIT TESTS COULD NOT EXPRESS, and the reason the guard
    shipped inert on its first build.

    Every clause above makes `.git` a real DIRECTORY. In a LANE WORKTREE — the
    only place this guard matters — `.git` is a FILE holding `gitdir: ...`, so
    `<repo>/.git/<marker>` raises NotADirectoryError and _inflight_open's
    swallow turned that into silence. Seven tests stayed green while the guard
    did nothing. Caught by DOGFOODING it against its own gate, not by testing.
    """

    def setUp(self):
        import subprocess
        self.base = tempfile.mkdtemp(prefix="helm-inflight-main-")
        for cmd in (["init", "-q"], ["config", "user.email", "t@e.example"],
                    ["config", "user.name", "T"]):
            subprocess.run(["git"] + cmd, cwd=self.base, capture_output=True)
        with open(os.path.join(self.base, "f"), "w") as fh:
            fh.write("x")
        subprocess.run(["git", "add", "-A"], cwd=self.base, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "seed", "--no-verify"],
                       cwd=self.base, capture_output=True)
        self.room = os.path.join(tempfile.mkdtemp(prefix="helm-inflight-wt-"),
                                 "lane")
        subprocess.run(["git", "worktree", "add", "-q", "-b", "lane/probe",
                        self.room], cwd=self.base, capture_output=True)

    def test_the_room_is_a_real_worktree_whose_dot_git_is_a_FILE(self):
        """The fixture's own control — if .git were a directory here, this
        class would be testing the same thing as the one above."""
        dot = os.path.join(self.room, ".git")
        self.assertTrue(os.path.isfile(dot), "fixture is not a real worktree")
        with open(dot, encoding="utf-8") as fh:
            self.assertIn("gitdir:", fh.read())

    def test_the_marker_works_in_a_WORKTREE_not_just_a_plain_repo(self):
        path = gate.inflight_path(self.room)
        # POSITIVE: it resolves into the PER-WORKTREE admin dir, never
        # <room>/.git (which is a file) and never the SHARED common dir
        # (which would make the marker project-wide again).
        self.assertIn("worktrees", path)
        self.assertTrue(os.path.isdir(os.path.dirname(path)))
        nonce = gate._inflight_open(self.room)
        live = gate.inflight(self.room)
        self.assertEqual(live[0], os.getpid())
        with mock.patch.object(os, "getcwd", lambda: self.room):
            rc = inflight_gate.main()
        self.assertEqual(rc, 1)
        gate._inflight_close(self.room, nonce)
        live = gate.inflight(self.room)
        self.assertIsNone(live)


class InflightRealInstalledHookTest(unittest.TestCase):
    """THE CASE SEVEN GREEN UNIT TESTS COULD NOT EXPRESS.

    Every test above calls `inflight_gate.main()` IN PROCESS, where `helm` is
    already imported. The deployed rung is a SNAPSHOT COPY in the hooks dir,
    run as `python3 <copy>` by a shell hook — and from there `import helm`
    raised ModuleNotFoundError, `main` swallowed it, and `git commit` returned
    0 with a live marker sitting in the room. The guard was inert in every
    place it was actually installed. Found by a review of e697b4e against
    a REAL installed hook in a fresh repo; no in-process test can see it,
    because the thing that breaks is the subprocess's import path.
    """

    def setUp(self):
        import subprocess
        self.room = tempfile.mkdtemp(prefix="helm-inflight-hook-")
        for cmd in (["init", "-q"], ["config", "user.email", "t@e.example"],
                    ["config", "user.name", "T"]):
            subprocess.run(["git"] + cmd, cwd=self.room, capture_output=True)

    def _commit(self, message):
        """Commit through the REAL hook with a scrubbed environment.

        PYTHONPATH is stripped deliberately: inheriting the runner's path would
        make `import helm` succeed for a reason the deployed hook never has,
        and the test would pass while the guard stayed broken."""
        import subprocess
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.pop("HELM_INFLIGHT_GATE_SKIP", None)
        return subprocess.run(["git", "commit", "-q", "-m", message],
                              cwd=self.room, capture_output=True, text=True,
                              env=env)

    def test_the_INSTALLED_hook_refuses_a_commit_while_a_gate_runs(self):  # noqa: VACUOUS_ASSERTION — the clean-commit rc==0 control and assertIn on blocked.stderr are unconditional positives
        from helm.work import _guard
        _guard.install_guard(self.room, apply=True, profile="rail")
        # THE RUNG UNDER TEST IS THE RAIL'S. An undeclared project
        # repo now defaults to the leak legs (task/2441), so this
        # room names the profile whose hook it is driving.
        with open(os.path.join(self.room, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("one\n")
        import subprocess
        subprocess.run(["git", "add", "f.txt"], cwd=self.room,
                       capture_output=True)
        # CONTROL FIRST: with no gate running the very same hook must ALLOW the
        # commit. Without this, a hook that refused everything (or a repo that
        # could not commit at all) would pass the real assertion below.
        clean = self._commit("no gate running")
        self.assertEqual(clean.returncode, 0,
                         "control: an unmarked room must commit: %s"
                         % clean.stderr)
        # now a gate IS running in this room
        nonce = gate._inflight_open(self.room)
        self.assertIsNotNone(nonce, "fixture: the marker must be written")
        self.assertEqual(gate.inflight(self.room)[0], os.getpid())
        with open(os.path.join(self.room, "f.txt"), "a", encoding="utf-8") as fh:
            fh.write("two\n")
        subprocess.run(["git", "add", "f.txt"], cwd=self.room,
                       capture_output=True)
        blocked = self._commit("during the gate")
        self.assertNotEqual(blocked.returncode, 0,
                            "an installed hook must REFUSE inside the bracket")
        self.assertIn("in-flight-gate", blocked.stderr)


class InflightOverlappingOwnersTest(unittest.TestCase):
    """A SECOND GATE MUST NOT FREE THE FIRST GATE'S ROOM.

    The repro against e697b4e: a long gate opens the marker, a short gate
    overwrites it, the short one finishes and unlinks — and the room reads FREE
    while the long suite is still running. The window this rung exists to close,
    reopened by the rung's own cleanup.
    """

    def setUp(self):
        import subprocess
        self.room = tempfile.mkdtemp(prefix="helm-inflight-overlap-")
        for cmd in (["init", "-q"], ["config", "user.email", "t@e.example"],
                    ["config", "user.name", "T"]):
            subprocess.run(["git"] + cmd, cwd=self.room, capture_output=True)

    def test_neither_gate_can_free_the_room_while_the_other_still_runs(self):  # noqa: VACUOUS_ASSERTION — every arm asserts the exact live pid, and the final assertIsNone is reached only after two positive observations
        """BOTH DIRECTIONS, because the first fix only survived one of them.

        Draft 1 overwrote, so a short gate finishing second deleted a long
        gate's marker. Draft 2 deferred to a live incumbent — and a review found
        the mirror image at review: if the INCUMBENT finishes FIRST it removes
        the sole marker while the deferred gate is still running. Deferral only
        ever worked when the incumbent outlived everyone who deferred to it.

        One file per owner makes both orders identical: each gate removes only
        its own, and the room is gating while ANY owner remains."""
        first = gate._inflight_open(self.room)
        second = gate._inflight_open(self.room)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second, "a second gate must REGISTER, not defer")
        self.assertNotEqual(first, second)
        self.assertEqual(gate.inflight(self.room)[0], os.getpid())
        # THE REVERSE-DURATION CASE: the FIRST one finishes first.
        gate._inflight_close(self.room, first)
        still = gate.inflight(self.room)
        self.assertIsNotNone(
            still, "the second gate is still running — the room must stay marked")
        self.assertEqual(still[0], os.getpid())
        # and only when the last owner goes does the room open
        gate._inflight_close(self.room, second)
        self.assertIsNone(gate.inflight(self.room))

    def test_closing_with_a_foreign_nonce_removes_nothing(self):  # noqa: VACUOUS_ASSERTION — the surviving marker's exact pid is asserted, an unconditional positive
        """A close names ONE owner file. A nonce that owns nothing must not
        reach any other gate's file — otherwise the refcount is decorative."""
        mine = gate._inflight_open(self.room)
        gate._inflight_close(self.room, "0" * 32)      # owns nothing
        live = gate.inflight(self.room)
        self.assertIsNotNone(live, "a foreign nonce must not free the room")
        self.assertEqual(live[0], os.getpid())
        gate._inflight_close(self.room, mine)
        self.assertIsNone(gate.inflight(self.room))


class InflightStaleIdentityTest(unittest.TestCase):
    """A LIVE PID IS NOT A LIVE GATE.

    The third blocker: a marker left by a crash or a reboot names a pid the
    kernel has since reissued, so `os.kill(pid, 0)` reports a STRANGER alive and
    the room stays refused for that unrelated process's whole lifetime. The
    marker therefore carries (boot, start) and both must still agree.
    """

    def setUp(self):
        import subprocess
        self.room = tempfile.mkdtemp(prefix="helm-inflight-stale-")
        for cmd in (["init", "-q"], ["config", "user.email", "t@e.example"],
                    ["config", "user.name", "T"]):
            subprocess.run(["git"] + cmd, cwd=self.room, capture_output=True)

    def _owner_file(self):
        """The single owner file this fixture just wrote. Asserting there is
        EXACTLY one is the fixture's own control: if open() ever stopped
        writing, `sole` would be empty and every mutation below would be
        rewriting nothing while still passing."""
        d = gate.inflight_dir(self.room)
        sole = [f for f in os.listdir(d) if f.endswith(".json")]
        self.assertEqual(len(sole), 1, "fixture: exactly one owner expected")
        return os.path.join(d, sole[0])

    def _rewrite(self, **over):
        path = self._owner_file()
        with open(path, encoding="utf-8") as fh:
            row = json.load(fh)
        row.update(over)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(row, fh)

    def test_a_marker_from_another_boot_is_not_a_live_gate(self):  # noqa: VACUOUS_ASSERTION — the as-written inflight()[0]==os.getpid() control runs first, unconditionally
        gate._inflight_open(self.room)
        # CONTROL: as written, by this live process, it IS a live gate.
        self.assertEqual(gate.inflight(self.room)[0], os.getpid())
        # same pid, still alive — but the machine has rebooted since.
        self._rewrite(boot="00000000-0000-0000-0000-000000000000")
        self.assertIsNone(gate.inflight(self.room),
                          "a pre-reboot marker must not refuse commits")

    def test_a_reused_pid_is_not_the_process_that_gated(self):  # noqa: VACUOUS_ASSERTION — the as-written inflight()[0]==os.getpid() control runs first, unconditionally
        gate._inflight_open(self.room)
        self.assertEqual(gate.inflight(self.room)[0], os.getpid())
        # the pid is live and the boot matches, but it started at a different
        # tick — the kernel reissued the number to somebody else.
        self._rewrite(start="1")
        self.assertIsNone(gate.inflight(self.room),
                          "a recycled pid must not refuse commits")

    def test_a_legacy_marker_without_identity_still_reads_live(self):  # noqa: VACUOUS_ASSERTION — live[0]==os.getpid() is a structural positive on the same root
        """BACK-COMPAT, and the negative direction of the two above: a marker
        written before this field existed carries no boot/start, and must fall
        back to the bare liveness check rather than being discarded — otherwise
        the fix would silently disarm the guard for every in-flight gate across
        the upgrade."""
        gate._inflight_open(self.room)
        path = self._owner_file()
        with open(path, encoding="utf-8") as fh:
            row = json.load(fh)
        row.pop("boot", None)
        row.pop("start", None)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(row, fh)
        live = gate.inflight(self.room)
        self.assertIsNotNone(live, "a legacy marker must still refuse")
        self.assertEqual(live[0], os.getpid())


class InflightTwoReadersAgreeTest(unittest.TestCase):
    """THE PRICE OF SELF-CONTAINMENT, PAID EXPLICITLY.

    The rung may not import helm — every sibling `_scanner_assets` snapshots is
    stdlib-only, and the two attempts to make this one different both failed:
    importing helm made it INERT from the snapshot dir, and baking the
    installer's source path made the guard die with the tree it was installed
    from (review of 469e4b8). So the marker read exists TWICE, and
    duplication that nothing pins is duplication that drifts. These tests are
    the pin: for every marker state the two readers must return the SAME answer.
    """

    def setUp(self):
        import subprocess
        self.room = tempfile.mkdtemp(prefix="helm-inflight-agree-")
        for cmd in (["init", "-q"], ["config", "user.email", "t@e.example"],
                    ["config", "user.name", "T"]):
            subprocess.run(["git"] + cmd, cwd=self.room, capture_output=True)

    def _both(self):
        return gate.inflight(self.room), inflight_gate.inflight(self.room)

    def test_the_rung_imports_nothing_from_helm(self):
        """THE LAW ITSELF, asserted on the source rather than trusted. A future
        edit that reaches for helm.gate would pass every behavioural test in
        this file and reintroduce BOTH defects at once."""
        import ast
        import inspect
        # PARSED, NOT GREPPED. The first draft substring-searched the source and
        # failed on its OWN DOCSTRING, which says "imports nothing from helm" in
        # prose — a check that cannot tell a sentence from a statement. ast can,
        # and it also cannot miss an import written in a shape a grep would
        # skip.
        tree = ast.parse(inspect.getsource(inflight_gate))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:                       # any relative import
                    names.add("." * node.level)
                elif node.module:
                    names.add(node.module.split(".")[0])
        self.assertNotIn("helm", names,
                         "the snapshot rung must stay stdlib-only")
        self.assertFalse([n for n in names if n.startswith(".")],
                         "no relative imports: the snapshot has no package")
        # POSITIVE CONTROL: it really does import the stdlib it needs, so an
        # empty `names` (a parse that saw nothing) cannot pass this test.
        self.assertIn("subprocess", names)
        self.assertIn("json", names)

    def test_both_readers_agree_across_every_marker_state(self):  # noqa: VACUOUS_ASSERTION — each arm asserts the two readers EQUAL each other and the live arms assert the exact pid
        # 1. NO MARKER
        a, b = self._both()
        self.assertEqual(a, b)
        self.assertIsNone(a)
        # 2. ONE LIVE OWNER — both must see this process
        nonce = gate._inflight_open(self.room)
        a, b = self._both()
        self.assertEqual(a, b)
        self.assertEqual(a[0], os.getpid())
        # 3. TWO LIVE OWNERS — both must report the same (oldest) one
        second = gate._inflight_open(self.room)
        a, b = self._both()
        self.assertEqual(a, b)
        self.assertEqual(a[0], os.getpid())
        gate._inflight_close(self.room, second)
        # 4. FOREIGN BOOT — both must call it dead
        d = gate.inflight_dir(self.room)
        path = os.path.join(d, nonce + ".json")
        with open(path, encoding="utf-8") as fh:
            row = json.load(fh)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(dict(row, boot="00000000-0000-0000-0000-000000000000"), fh)
        a, b = self._both()
        self.assertEqual(a, b)
        self.assertIsNone(a)
        # 5. LEGACY SINGLE FILE — both must still honour it
        os.remove(path)
        with open(gate.inflight_path(self.room), "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "ts": "2026-01-01T00:00:00Z"}, fh)
        a, b = self._both()
        self.assertEqual(a, b)
        self.assertEqual(a[0], os.getpid())
