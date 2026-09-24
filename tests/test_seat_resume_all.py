#!/usr/bin/env python3
"""`helm seat resume --all` — the post-reboot sweep, one arm per classification.

Hermetic: HELM_HOME / HELM_CHAT_DIR are tmp trees, the metaharness is a fake
that records every send/spawn/stop in order, /proc is emptied for the seat
routes by patching glob, the claude census and live-session map are patched
to "nothing", and boot time is a fixed number. No real pane, register, or
process is touched.

Each arm names the mutation that would redden it in its docstring; the
DEAD-PANE arm's (delete the disarm send in seat._resume) was actually run.
"""
import contextlib
import glob as _glob
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from tests import _tmphome  # noqa: F401 — plants the tmp config roots first

from helm import (harness, orcaadopt, seat, seat_exit_owner,
                  seat_resume_all, seats, sessions)
from helm.seat_launch_owner import TERMINAL_DISARM

SID = "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"
PANE_KEY = "tab-1:leaf-1"
BOOT = 1_700_000_000
BEFORE_BOOT = "2020-01-01T00:00:00Z"      # any stamp older than BOOT
AFTER_BOOT = "2030-01-01T00:00:00Z"
PROC_PATTERN = "/proc/[0-9]*/environ"


def _plant_resolve(info):
    """A patcher answering `info` from `orcaadopt.resolve`, behind the REAL
    signature. A hand-typed `lambda name, adapter=None` refused the `census=`
    keyword the liveness reader passes; that reader catches any exception and
    answers None, so the planted seat vanished from one of its two readers
    and no arm noticed. autospec cannot fall behind the function it copies."""
    return mock.patch.object(orcaadopt, "resolve", autospec=True,
                             return_value=info)

# A pane whose composer is EMPTY — the fake's read() answers it so `submit`
# (the wake-path re-arm inside _resume) can prove delivery by reading back.
ADVANCED_PANE = "\n".join(("─" * 40, "❯", "─" * 40,
                           "  opus-5 | ~/dev/example/repo",
                           "  ⏵⏵ bypass permissions on"))


class FakeOrca(harness._CLIAdapter):
    """Records every seam op in ORDER: the DEAD-PANE arm asserts disarm
    precedes launch, and the LIVE arm asserts the log is empty."""
    name, path = "orca", "/bin/orca"

    def __init__(self, rows=(), resolved=None, resolve_error=None):
        self.rows = list(rows)
        self.resolved, self.resolve_error = resolved or {}, resolve_error
        self.sent, self.spawned, self.stopped, self.order = [], [], [], []
        self.typed = {}
        self.renamed, self.rename_error = [], None

    def resolve_pane(self, pane_key):
        self.order.append(("resolve", pane_key))
        if self.resolve_error:
            raise harness.HarnessError(self.resolve_error)
        return dict(self.resolved)

    def list(self):
        return list(self.rows)

    def spawn(self, command, title=None, cwd=None):
        self.spawned.append((command, title, cwd))
        self.order.append(("spawn", command))
        return "pane-new"

    def read(self, handle, limit=3000, timeout=60):
        text = self.typed.get(handle)
        if text is not None:
            return ADVANCED_PANE.replace("\n❯\n", "\n❯\xa0%s\n" % text)
        return ADVANCED_PANE

    def send(self, handle, text, enter=True):
        if enter:
            self.typed.pop(handle, None)
        else:
            self.typed[handle] = text
        self.sent.append((handle, text, enter))
        self.order.append(("send", handle, text))

    def stop(self, handle):
        self.stopped.append(handle)
        self.order.append(("stop", handle))

    def rename(self, handle, title=None):
        self.renamed.append((handle, title))
        self.order.append(("rename", handle, title))
        if self.rename_error:
            raise harness.HarnessError(self.rename_error)
        return title


# `title` and `worktree` are the two fields the TITLE re-stamp reads, and they
# carry orca's own shape: an auto-generated title decorated with an activity
# glyph, and the seat's home worktree path.
LIVE_ROW = {"handle": "shell-1", "pty_id": "pty-1", "status": "connected",
            "writable": True, "orphaned": False, "worktree_id": "w1",
            "title": "\u2733 Claude Code",
            "worktree": "/home/x/dev/proj-wt/seats/codex"}
#: what helm asserts on that pane: the seat name as the board renders it, and
#: nothing else — the tab strip is meant to be matched against the board by eye
LIVE_TITLE = "codex"


class SweepFixture(unittest.TestCase):
    ENV = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
           "HELM_SPAWN_SEND_DELAY", "HELM_SUBMIT_SETTLE_S", "HELM_CHAT_NAME")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-sweep-")
        self._env = {k: os.environ.get(k) for k in self.ENV}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_SPAWN_SEND_DELAY"] = "0"
        os.environ["HELM_SUBMIT_SETTLE_S"] = "0"
        for k in ("MELD_HOME", "MELD_CHAT_DIR", "HELM_CHAT_NAME"):
            os.environ.pop(k, None)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        real_glob = _glob.glob

        def no_proc(pattern, *a, **kw):
            return [] if pattern == PROC_PATTERN else real_glob(pattern, *a, **kw)

        for patch in (
                mock.patch.object(seat, "_ensure_autocompact_timer"),
                mock.patch.object(seat, "_write_launch_assets"),
                mock.patch.object(seat.glob, "glob", side_effect=no_proc),
                mock.patch.object(orcaadopt, "claude_processes",
                                  return_value=([], [])),
                mock.patch.object(sessions, "live_sids", return_value={}),
                mock.patch.object(seat_resume_all, "boot_epoch",
                                  return_value=BOOT),
                mock.patch.object(seats, "TEMP_ROOTS", ("/dev/shm",))):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixture builders ---------------------------------------------------

    def _mint(self, seat_name="codex", family="codex"):
        d = seat._instance_dir(family, seat_name)
        os.makedirs(d, exist_ok=True)
        launch = os.path.join(d, "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env FAKE=1 claude \"$@\"\n")
        os.chmod(launch, 0o700)
        return d, launch

    def _record(self, d, ts=BEFORE_BOOT, **extra):
        rec = {"v": 1, "seat": "codex", "harness": "orca", "handle": "old",
               "session": SID, "pane_key": PANE_KEY, "worktree": self.work,
               "room": "main", "ts": ts}
        rec.update(extra)
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump(rec, f)
        return rec

    def _register(self, d):
        with open(os.path.join(d, "spawn.json")) as f:
            return json.load(f)

    def _plant_session(self, d, sid=SID):
        proj = os.path.join(d, "claude", "projects", "-spot")
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, sid + ".jsonl"), "w") as f:
            # one REAL turn: the by-id lookup the act pins through refuses a
            # stub with no assistant message (a reboot crash stub's shape)
            f.write(json.dumps({"cwd": self.work, "type": "user",
                                "message": {"role": "user", "content": "hi"}})
                    + "\n")
            f.write(json.dumps({"cwd": self.work, "type": "assistant",
                                "message": {"role": "assistant",
                                            "content": "hello"}}) + "\n")
        return sid

    def _dead_pane_seat(self):
        """The incident's shape: register predates boot, no process anywhere,
        pane key resolves to a live bare shell, transcript present."""
        d, _ = self._mint()
        self._record(d)
        self._plant_session(d)
        fake = FakeOrca(rows=[LIVE_ROW],
                        resolved={"handle": "shell-1", "pty_id": "pty-1"})
        return d, fake

    def _sweep(self, args, adapter):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(harness, "detect", return_value=adapter), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["resume"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    @staticmethod
    def _row(out, name):
        lines = [l for l in out.splitlines() if l.startswith(name + " ")]
        assert len(lines) == 1, (name, out)
        return lines[0]


# ---------------------------------------------------------------------------
# LIVE — the must-miss control
# ---------------------------------------------------------------------------

class LiveSeatTest(SweepFixture):

    def test_live_seat_is_restamped_and_never_disarmed_or_resumed(self):  # noqa: VACUOUS_ASSERTION — the MUST-MISS control: positive controls are rc 0, the LIVE row text, the re-stamped handle and session bytes in spawn.json, and the 1 LIVE summary; the empty sent log and the uncalled _resume double ARE the contract, and the DEAD-PANE arm proves the same fixture shape does send
        """MUTATION THAT REDDENS THIS: make the sweep act on LIVE rows (or
        drop the `if not err` branch) — the sent log stops being empty and
        the _resume double records a call."""
        d, _ = self._mint()
        self._record(d, handle="stale-boot-handle", session="stale-sid")
        fake = FakeOrca(rows=[LIVE_ROW],
                        resolved={"handle": "shell-1", "pty_id": "pty-1"})
        current = {"pid": 42, "proc_start": "1", "pane_key": PANE_KEY,
                   "worktree_id": "w1", "session": SID}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(None, "stale session is gone")), \
                mock.patch.object(seat, "_live_seat_orca_identity",
                                  return_value=(current, None)), \
                mock.patch.object(seat, "_resume") as resume:
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, err)
        row = self._row(out, "codex")
        self.assertIn("LIVE", row)
        self.assertIn("re-stamped register", row)
        self.assertEqual(fake.sent, [], "a LIVE seat must receive NO disarm")
        self.assertEqual(fake.spawned, [])
        self.assertEqual(fake.stopped, [])
        resume.assert_not_called()
        # rebind's own effect, reused not duplicated: the register now names
        # the live pane and the live session
        rec = self._register(d)
        self.assertEqual(rec["handle"], "shell-1")
        self.assertEqual(rec["session"], SID)
        self.assertIn("1 LIVE", out)

    def test_a_healthy_fleet_never_pays_for_a_proc_walk(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 0 and the 'already current' row; the never-called census doubles are the cost contract, and the DEAD-PANE arms drive both doubles on the same fixture shape
        """The timer runs this every interval; on a fleet that is all LIVE the
        census and live-session map must not be consulted at all.
        MUTATION: compute them eagerly in cmd_resume_all."""
        d, _ = self._mint()
        self._record(d, handle="shell-1", pty_id="pty-1", worktree_id="w1")
        fake = FakeOrca(rows=[LIVE_ROW],
                        resolved={"handle": "shell-1", "pty_id": "pty-1"})
        current = {"pid": 42, "proc_start": "1", "pane_key": PANE_KEY,
                   "worktree_id": "w1"}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(current, None)):
            rc, out, err = self._sweep(["--all"], fake)
        self.assertEqual(rc, 0, err)
        self.assertIn("already current", self._row(out, "codex"))
        orcaadopt.claude_processes.assert_not_called()
        sessions.live_sids.assert_not_called()


# ---------------------------------------------------------------------------
# DEAD-PANE — disarm, then launch, into the SAME pane
# ---------------------------------------------------------------------------

class DeadPaneTest(SweepFixture):

    def test_disarm_precedes_the_launch_line_into_the_same_pane(self):
        """MUTATION RUN 2026-08-22: deleting the `ad.send(into_pane,
        seat_resume_all.disarm_line(), ...)` line in seat._resume reddens
        this arm at the first assertion below (sent[0] is the launch line)
        and the exact-bytes assertion; restored afterwards."""
        d, fake = self._dead_pane_seat()
        rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, out + err)
        row = self._row(out, "codex")
        self.assertIn("DEAD-PANE", row)
        self.assertIn("resumed", out)
        self.assertEqual(fake.spawned, [], "a DEAD-PANE reuses its pane")
        self.assertEqual(fake.stopped, [], "nothing to reap, nothing stopped")
        # the first two sends, in order: disarm, then the launch line
        self.assertGreaterEqual(len(fake.sent), 2, fake.sent)
        handle, disarm, enter = fake.sent[0]
        self.assertEqual((handle, enter), ("shell-1", True))
        self.assertEqual(disarm, seat_resume_all.disarm_line())
        # EXACT TERMINAL_DISARM bytes, in printf's \033 encoding — and never
        # a raw ESC, which readline would eat as a key-sequence prefix
        self.assertIn(TERMINAL_DISARM.decode("ascii").replace("\x1b", "\\033"),
                      disarm)
        self.assertNotIn("\x1b", disarm)
        self.assertTrue(disarm.startswith("\x15printf '"), disarm)
        handle, launch, enter = fake.sent[1]
        self.assertEqual((handle, enter), ("shell-1", True))
        self.assertTrue(launch.startswith("cd %s && " % self.work), launch)
        self.assertIn(os.path.join(d, "launch.sh"), launch)
        self.assertIn("--resume %s" % SID, launch)
        self.assertNotIn("--continue", launch)
        # the register now names the reused pane and the resumed session
        rec = self._register(d)
        self.assertEqual(rec["handle"], "shell-1")
        self.assertEqual(rec["session"], SID)
        self.assertGreater(rec["ts"], BEFORE_BOOT)

    def test_dry_run_plans_the_disarm_but_sends_nothing(self):
        """MUTATION: act regardless of --apply."""
        d, fake = self._dead_pane_seat()
        rc, out, err = self._sweep(["--all"], fake)
        self.assertEqual(rc, 0, err)
        row = self._row(out, "codex")
        self.assertIn("DEAD-PANE", row)
        self.assertIn("would disarm + resume into pane", row)
        self.assertEqual(fake.sent, [])
        self.assertEqual(fake.spawned, [])
        self.assertEqual(self._register(d)["handle"], "old")
        self.assertIn("DRY RUN", out)

    def test_a_register_written_after_boot_is_reported_not_acted(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 0, the DEAD-PANE row, the 'register postdates boot' action text; the empty sent/spawned logs are the contract and the sibling disarm arm proves the identical fixture minus the stamp does send
        """Mid-day death is not the reboot case. MUTATION: drop the boot
        binding — the seat gets relaunched and the sends appear."""
        d, fake = self._dead_pane_seat()
        self._record(d, ts=AFTER_BOOT)
        rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, err)
        row = self._row(out, "codex")
        self.assertIn("DEAD-PANE", row)
        self.assertIn("register postdates boot", row)
        self.assertEqual(fake.sent, [])
        self.assertEqual(fake.spawned, [])

    def test_the_dead_matcher_binds_to_rebinds_own_refusal(self):
        """The real _prove_orca_rebind, driven dead (no pid record, empty
        /proc), must produce exactly the sentence pair the matcher accepts;
        a count of two, or no session at all, must not. MUTATION: edit either
        producer sentence without its constant."""
        d, _ = self._mint()
        rec = self._record(d)
        fake = FakeOrca(rows=[LIVE_ROW],
                        resolved={"handle": "shell-1", "pty_id": "pty-1"})
        _pane, fields, err = seat._prove_orca_rebind(d, rec, fake, [LIVE_ROW])
        self.assertIsNone(fields)
        self.assertTrue(seat.rebind_refusal_means_dead(err, rec), err)
        two = err.replace("has 0 exact", "has 2 exact")
        self.assertFalse(seat.rebind_refusal_means_dead(two, rec))
        nameless = dict(rec, session=None)
        _pane, _fields, err2 = seat._prove_orca_rebind(d, nameless, fake,
                                                       [LIVE_ROW])
        self.assertFalse(seat.rebind_refusal_means_dead(err2, nameless), err2)


# ---------------------------------------------------------------------------
# PANE-GONE — a fresh pane through the metaharness
# ---------------------------------------------------------------------------

class PaneGoneTest(SweepFixture):

    def test_pane_gone_spawns_a_new_pane_without_a_disarm(self):
        """orca's POSITIVE terminal_not_found is the only route here.
        MUTATION: treat every resolve error as gone — the UNKNOWN arm below
        catches it; or keep the reap for proven-dead seats — the spawn
        never happens and rc is 1."""
        d, _ = self._mint()
        self._record(d)
        self._plant_session(d)
        fake = FakeOrca(rows=[], resolve_error=(
            "orca runtime rpc terminal.resolvePane: terminal_not_found"))
        rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, out + err)
        row = self._row(out, "codex")
        self.assertIn("PANE-GONE", row)
        self.assertIn("not found", row)
        self.assertIn("resumed", out)
        self.assertEqual(len(fake.spawned), 1, fake.spawned)
        command, title, cwd = fake.spawned[0]
        self.assertEqual((title, cwd), ("codex", self.work))
        self.assertIn("--resume %s" % SID, command)
        disarms = [t for _h, t, _e in fake.sent if "printf" in t]
        self.assertEqual(disarms, [], "no pane to disarm")
        self.assertEqual(self._register(d)["handle"], "pane-new")


# ---------------------------------------------------------------------------
# UNKNOWN — rendered with its reason, counted in the exit code, never acted
# ---------------------------------------------------------------------------

class UnknownTest(SweepFixture):

    def test_a_non_dead_rebind_refusal_is_unknown_and_apply_exits_nonzero(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1 under --apply, rc 0 dry-run, the UNKNOWN row carrying the refusal text; the uncalled _resume double and empty sent log are the never-relaunch contract
        """Two live processes is ambiguity, not death. MUTATION: match the
        dead sentence by substring — '2 exact live' contains 'exact live'
        and the seat would be relaunched over a live process."""
        d, _ = self._mint()
        self._record(d)
        self._plant_session(d)
        fake = FakeOrca(rows=[LIVE_ROW],
                        resolved={"handle": "shell-1", "pty_id": "pty-1"})
        ambiguous = seat.NO_EXACT_LIVE_SESSION % (SID, 2)
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(None, ambiguous)), \
                mock.patch.object(seat, "_resume") as resume:
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 1, "UNKNOWN under --apply must exit non-zero")
        row = self._row(out, "codex")
        self.assertIn("UNKNOWN", row)
        self.assertIn("rebind refused", row)
        self.assertIn("2 exact live Claude processes", row)
        self.assertEqual(fake.sent, [])
        resume.assert_not_called()
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(None, ambiguous)):
            rc, out, _ = self._sweep(["--all"], fake)
        self.assertEqual(rc, 0, "dry-run reports, it does not fail")
        self.assertIn("UNKNOWN", self._row(out, "codex"))

    def test_a_stub_transcript_is_unknown_not_resumed(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1 under --apply and the UNKNOWN row naming 'stub'; the uncalled _resume path (empty sent log) is the contract, proven non-vacuous by the disarm arm whose transcript carries an assistant turn
        """The table must select by the act's own bar: a newest transcript
        with no assistant turn is a reboot crash stub, which the by-id pin
        refuses. MUTATION: drop the by-id check in classify_spawned — the
        row reads RESUMING and the act fails rc 1 under it."""
        d, fake = self._dead_pane_seat()
        path = os.path.join(d, "claude", "projects", "-spot", SID + ".jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user",
                                                            "content": "hi"}})
                    + "\n")
        rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 1, out + err)
        row = self._row(out, "codex")
        self.assertIn("UNKNOWN", row)
        self.assertIn("stub", row)
        self.assertEqual(fake.sent, [])

    def test_no_transcript_is_unknown_not_a_fresh_session(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1 and the UNKNOWN row naming 'no session transcript'; the uncalled _resume double is the no-minted-session contract
        """MUTATION: fall through to _resume — it would launch --continue in
        an empty config dir, i.e. mint a session."""
        d, _ = self._mint()
        self._record(d)
        fake = FakeOrca(rows=[LIVE_ROW],
                        resolved={"handle": "shell-1", "pty_id": "pty-1"})
        with mock.patch.object(seat, "_resume") as resume:
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 1)
        row = self._row(out, "codex")
        self.assertIn("UNKNOWN", row)
        self.assertIn("no session transcript", row)
        resume.assert_not_called()
        self.assertEqual(fake.sent, [])

    def test_a_resolve_failure_that_is_not_not_found_is_unknown(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1 and the UNKNOWN row naming the resolution failure; the uncalled _resume double is the contract, and the PANE-GONE arm proves the not-found sibling DOES relaunch
        """Daemon down is not 'no pane'. MUTATION: map every exception to
        PANE-GONE."""
        d, _ = self._mint()
        self._record(d)
        self._plant_session(d)
        fake = FakeOrca(rows=[], resolve_error=(
            "orca runtime metadata unavailable: [Errno 2] No such file"))
        with mock.patch.object(seat, "_resume") as resume:
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 1)
        row = self._row(out, "codex")
        self.assertIn("UNKNOWN", row)
        self.assertIn("pane key resolution failed", row)
        resume.assert_not_called()


# ---------------------------------------------------------------------------
# FLEET HOLD — classified, reported, never acted
# ---------------------------------------------------------------------------

class HandResumeTest(SweepFixture):
    """`helm seat resume <seat>` BY HAND on a reboot-dead seat — the manual
    process the owner asked for, which aborted on every proxy seat the
    morning of 2026-08-22 because the reap resolver refuses a register whose
    pane the boot destroyed."""

    def test_hand_resume_takes_the_into_pane_leg_when_the_reap_refuses(self):
        """MUTATION: abort on the reap's refusal without consulting
        prove_reboot_dead (the pre-sweep behaviour) — rc 1, nothing sent."""
        d, fake = self._dead_pane_seat()
        real_proof = seat_resume_all.prove_reboot_dead
        proof_entry = []

        def prove(*args, **kwargs):
            proof_entry.append((
                os.path.exists(os.path.join(d, "spawn.json")),
                seat_exit_owner.archive_paths(d)))
            return real_proof(*args, **kwargs)

        with mock.patch.object(seat_resume_all, "prove_reboot_dead",
                               side_effect=prove):
            rc, out, err = self._sweep(["codex"], fake)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(proof_entry, [(True, []), (True, [])],
                         "both in-lock proofs precede terminal archival")
        self.assertIn("reap refused", out)
        self.assertIn("proven DEAD-PANE", out)
        self.assertEqual(fake.spawned, [], "the bare pane is reused, not replaced")
        self.assertEqual(fake.stopped, [])
        self.assertGreaterEqual(len(fake.sent), 2, fake.sent)
        self.assertEqual(fake.sent[0][1], seat_resume_all.disarm_line())
        self.assertIn("--resume %s" % SID, fake.sent[1][1])
        self.assertEqual(self._register(d)["handle"], "shell-1")
        archives = seat_exit_owner.archive_paths(d)
        self.assertEqual(len(archives), 1)
        with open(archives[0]) as f:
            terminal = json.load(f)["terminal"]
        self.assertIn("DEAD-PANE under the seat lock", terminal["reason"])

    def test_disarm_failure_restores_script_before_launch_was_attempted(self):  # noqa: VACUOUS_ASSERTION — original bytes and explicit error are positive controls
        d, fake = self._dead_pane_seat()
        launch = os.path.join(d, "launch.sh")
        with open(launch, "rb") as f:
            before = f.read()

        def remint(*_a, **_k):
            with open(launch, "w") as f:
                f.write("#!/bin/sh\nexec env NEW=1 claude \"$@\"\n")

        def fail_disarm(*_a, **_k):
            raise harness.HarnessError("disarm transport failed")

        seat._write_launch_assets.side_effect = remint
        fake.send = fail_disarm
        rc, _out, err = self._sweep(["codex"], fake)
        self.assertEqual(rc, 1)
        with open(launch, "rb") as f:
            self.assertEqual(f.read(), before,
                             "a failed disarm was treated as a relaunched pane")
        self.assertIn("disarm transport failed", err)

    def test_hand_resume_is_unbound_from_boot_and_hold(self):
        """MUTATION: bind_boot honoured (or the hold consulted) inside
        prove_reboot_dead — the register below postdates boot and the marker
        exists, and the sweep arms prove BOTH stop the timer; a named seat
        must still resume."""
        d, fake = self._dead_pane_seat()
        self._record(d, ts=AFTER_BOOT)
        marker = seat_resume_all.hold_marker_path()
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        open(marker, "w").close()
        rc, out, err = self._sweep(["codex"], fake)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(fake.sent[0][1], seat_resume_all.disarm_line())
        self.assertEqual(self._register(d)["handle"], "shell-1")

    def test_hand_resume_still_aborts_when_the_proof_is_not_death(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1, both refusals on stderr naming the reap and the UNKNOWN reason; the empty sent log is the contract and the sibling arm proves the same fixture minus the squatter does send
        """MUTATION: treat any reap refusal as proven death."""
        d, fake = self._dead_pane_seat()
        squatter = {"pid": 4242, "start": "1", "seat": "other",
                    "pane_key": PANE_KEY, "worktree_id": "w1",
                    "resume_sid": None}
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([squatter], [])):
            rc, out, err = self._sweep(["codex"], fake)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("reboot-dead proof under the seat lock: UNKNOWN", err)
        self.assertIn("pid 4242", err)
        self.assertEqual(fake.sent, [])
        self.assertEqual(fake.spawned, [])
        self.assertEqual(seat_exit_owner.archive_paths(d), [])
        self.assertEqual(self._register(d)["handle"], "old")


class ProofUnderTheLockTest(SweepFixture):
    """The FIX on 7840d597e: the sweep proved death under
    .spawn.lock, RELEASED it, then `_resume` re-took the lock and acted on
    the handed-in proof — a hand resume in the gap was injected over. The
    proof now runs inside `_resume`'s own lock, and the table is a report."""

    def test_a_seat_that_came_alive_after_the_table_is_refused_not_injected(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1 under --apply, the DEAD-PANE row (the table's honest report), the 'resume FAILED' action, the in-lock UNKNOWN reason naming pid 4242, the untouched register; the empty sent log is the never-inject contract, proven non-vacuous by the disarm arm on the identical fixture with a quiet census
        """MUTATION: hand the table's handle into _resume and skip the in-lock
        proof (the 7840d597e shape) — both sends fire into the squatter's
        composer and the register is overwritten."""
        d, fake = self._dead_pane_seat()
        squatter = {"pid": 4242, "start": "1", "seat": "other",
                    "pane_key": PANE_KEY, "worktree_id": "w1",
                    "resume_sid": None}
        # first census (the table): nobody; second census (the early proof
        # under the lock, inside _resume): another claude now holds the
        # pane key — refused before any asset is re-minted
        with mock.patch.object(orcaadopt, "claude_processes",
                               side_effect=[([], []), ([squatter], [])]), \
                mock.patch.object(seat, "_write_launch_assets") as wla:
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        wla.assert_not_called()
        self.assertEqual(rc, 1, out + err)
        row = self._row(out, "codex")
        self.assertIn("DEAD-PANE", row)
        self.assertIn("resume FAILED", out)
        self.assertIn("under the seat lock: UNKNOWN", out)
        self.assertIn("pid 4242", out)
        self.assertEqual(fake.sent, [], "nothing is typed into a live composer")
        self.assertEqual(fake.spawned, [])
        self.assertEqual(self._register(d)["handle"], "old")

    def test_the_pane_written_to_is_the_one_resolved_under_the_lock(self):
        """MUTATION: write to the table's handle instead of the in-lock one —
        the launch line lands in the pane orca no longer maps the key to."""
        d, fake = self._dead_pane_seat()
        moved = dict(LIVE_ROW, handle="shell-2")
        fake.rows = [LIVE_ROW, moved]
        # the table AND the early in-lock proof resolve shell-1; by the
        # LATE proof at the send, orca maps the key to shell-2 (a remint in
        # helm's own asset-minting window) — the send follows the late one
        answers = iter([{"handle": "shell-1", "pty_id": "pty-1"},
                        {"handle": "shell-1", "pty_id": "pty-1"},
                        {"handle": "shell-2", "pty_id": "pty-1"}])
        fake.resolve_pane = lambda key: next(answers)
        rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual({h for h, _, _ in fake.sent}, {"shell-2"}, fake.sent)
        self.assertEqual(self._register(d)["handle"], "shell-2")


class AdoptedSeatTest(SweepFixture):
    """An orca-ADOPTED seat (a roster row helm never spawned: no register,
    no pane key) is always PANE-GONE when dead and relaunches through
    orcaadopt.resume. A FIX on the first under-the-lock cut: the refactor that moved
    the proof under the lock made _resume REFUSE reboot_dead for adopted
    seats, so every adopted row failed rc 2 under --apply — and no arm had
    ever driven an adopted row through --apply."""

    ADOPTED = "hc-adopted"

    def _plant(self, seen):
        from helm import seats_roster
        seats.write_roster(self.ADOPTED, session=SID, cwd=self.work)
        os.utime(seats_roster.seen_path(self.ADOPTED), (seen, seen))
        info = {"state": orcaadopt.DEAD, "sessions": [SID],
                "evidence": "no live process claims it"}
        calls = []

        def resume(name, force=False, session=None):
            calls.append((name, force, session))
            return 0, ["resumed %s" % name]

        for patch in (_plant_resolve(info),
                      mock.patch.object(orcaadopt, "resume", resume)):
            patch.start()
            self.addCleanup(patch.stop)
        return calls

    def test_a_dead_adopted_seat_is_resumed_under_apply(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 0, the PANE-GONE adopted row, the resumed action, the orcaadopt.resume double called exactly once with the seat; the empty sent log is the no-pane contract, proven non-vacuous by the DEAD-PANE arms on a registered seat
        """MUTATION: refuse reboot_dead on the adopted branch of _resume
        (the first under-the-lock cut's shape) — the row reads resume FAILED rc=2."""
        calls = self._plant(BOOT - 100)
        fake = FakeOrca()
        rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, out + err)
        row = self._row(out, self.ADOPTED)
        self.assertIn("PANE-GONE", row)
        self.assertIn("adopted/orca", row)
        self.assertIn("resumed", out)
        # the SESSION is pinned on the adopted branch too (a FIX on
        # the managed-seat pin: this branch had dropped it and
        # orcaadopt.resume reselected newest)
        self.assertEqual(calls, [(self.ADOPTED, False, SID)])
        self.assertEqual(fake.sent, [], "an adopted seat has no pane to type into")

    def test_the_adopted_pin_reaches_newest_session_row(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1 and the refusal naming the pinned session; the uncalled spawn double is the never-another-session contract, proven non-vacuous by the sibling arm whose pinned session has a row
        """The real orcaadopt.resume with a pinned session whose transcript
        is gone must refuse, not fall back to the newest recorded one.
        MUTATION: ignore `session` in resume — newest_session_row picks the
        other transcript and the spawn fires."""
        from helm import sessions
        other = {"i": "0199ffff-0000-0000-0000-000000000009", "h": "claude",
                 "mt": 1, "cwd": self.work}
        spawned = []
        with mock.patch.object(orcaadopt, "seat_liveness",
                               return_value=(orcaadopt.DEAD, "planted")), \
                mock.patch.object(orcaadopt, "roster_sessions",
                                  return_value=([SID, other["i"]], False)), \
                mock.patch.object(sessions, "rows_for",
                                  return_value=[other]), \
                mock.patch.object(sessions, "spawn_resume",
                                  side_effect=lambda *a, **k: spawned.append(a)):
            rc, lines = orcaadopt.resume(self.ADOPTED, adapter=FakeOrca(),
                                         session=SID)
        self.assertEqual(rc, 1, lines)
        self.assertTrue(any("pinned session" in l for l in lines), lines)
        self.assertEqual(spawned, [])

    def test_an_adopted_seat_seen_after_boot_is_reported_not_acted(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 0, the PANE-GONE row, the 'roster last_seen postdates boot' action; the uncalled orcaadopt.resume double is the contract, proven non-vacuous by the sibling arm on the identical fixture with an older last_seen
        calls = self._plant(BOOT + 100)
        rc, out, err = self._sweep(["--all", "--apply"], FakeOrca())
        self.assertEqual(rc, 0, out + err)
        row = self._row(out, self.ADOPTED)
        self.assertIn("PANE-GONE", row)
        self.assertIn("postdates boot", row)
        self.assertEqual(calls, [])


class SidPinnedTest(SweepFixture):

    def test_the_session_the_table_classified_is_the_one_resumed(self):
        """The FIX on the SID-pin cut: the table classified SID A, a
        newer transcript B also exists — the launch line must carry A.
        MUTATION: let _resume reselect newest — the line carries B."""
        d, fake = self._dead_pane_seat()
        newer = "0199ffff-0000-0000-0000-000000000002"
        self._plant_session(d, newer)
        path = os.path.join(d, "claude", "projects", "-spot", newer + ".jsonl")
        os.utime(path, (time.time() + 60, time.time() + 60))
        rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, out + err)
        launch = fake.sent[1][1]
        self.assertIn("--resume %s" % SID, launch)
        self.assertNotIn(newer, launch)
        self.assertEqual(self._register(d)["session"], SID)

    def test_a_vanished_classified_session_refuses_rather_than_resuming_another(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1, the refusal naming the classified sid and 'never the newest other one', the untouched register; the empty sent log is the never-a-different-conversation contract, proven non-vacuous by the sibling arm with the transcript present
        """SID A vanishes between the table and _resume while B remains.
        MUTATION: fall back to newest when reboot_sid is absent."""
        d, fake = self._dead_pane_seat()
        other = "0199ffff-0000-0000-0000-000000000003"
        self._plant_session(d, other)
        a = os.path.join(d, "claude", "projects", "-spot", SID + ".jsonl")
        real = seat._seat_session_by_id
        looks = []

        def vanish(dd, sid):
            # the table's by-id check is the FIRST look and sees A; A is gone
            # by the time _resume's pin looks (the second)
            looks.append(sid)
            if sid == SID and len(looks) == 2 and os.path.exists(a):
                os.unlink(a)
            return real(dd, sid)

        with mock.patch.object(seat, "_seat_session_by_id", side_effect=vanish):
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("never the newest other one", out + err)
        self.assertEqual(fake.sent, [])
        self.assertEqual(fake.spawned, [])
        self.assertEqual(self._register(d)["handle"], "old")


class PositionalTest(SweepFixture):

    def test_a_seat_name_beside_all_refuses_instead_of_sweeping(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 2 and the refusal naming codex and the one-seat verb; the empty sent log and untouched register are the never-sweep-under-a-misread-argv contract, proven non-vacuous by the disarm arm on the identical fixture without the name
        """The second finding: `--all codex --apply` dropped `codex`
        and swept the fleet. MUTATION: filter positionals out silently."""
        d, fake = self._dead_pane_seat()
        rc, out, err = self._sweep(["--all", "codex", "--apply"], fake)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("takes no seat name", err)
        self.assertIn("helm seat resume codex", err)
        self.assertEqual(fake.sent, [])
        self.assertEqual(self._register(d)["handle"], "old")

    def test_a_seat_name_refuses_even_beside_help(self):
        """The residual: `--all codex --help` printed help rc 0 over
        the name. MUTATION: run guard_tail before the positional check."""
        d, fake = self._dead_pane_seat()
        rc, out, err = self._sweep(["--all", "codex", "--help"], fake)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("takes no seat name", err)


class SquatterTest(SweepFixture):

    def test_a_pane_key_held_by_another_claude_is_unknown_not_dead(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1 under --apply and the UNKNOWN row naming the squatter pid; the empty sent log is the never-type-into-a-composer contract, proven non-vacuous by the disarm arm on the identical fixture without the squatter
        """MUTATION: drop the squatter check in classify_spawned — the row
        reads DEAD-PANE and the launch line is typed into a live composer."""
        d, fake = self._dead_pane_seat()
        squatter = {"pid": 4242, "start": "1", "seat": "other",
                    "pane_key": PANE_KEY, "worktree_id": "w1",
                    "resume_sid": None}
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([squatter], [])):
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 1, out + err)
        row = self._row(out, "codex")
        self.assertIn("UNKNOWN", row)
        self.assertIn("pid 4242", row)
        self.assertIn("not bare", row)
        self.assertEqual(fake.sent, [])
        self.assertEqual(fake.spawned, [])
        self.assertEqual(self._register(d)["handle"], "old")


class FleetHoldTest(SweepFixture):

    def test_hold_marker_reports_held_and_acts_on_nothing(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 0, the HELD row, the footer naming the marker path, the untouched register handle; the empty sent log and uncalled _resume are the hold contract, proven non-vacuous by the disarm arm on the same fixture without the marker
        """MUTATION: ignore the marker — the disarm send appears and the
        footer disappears."""
        d, fake = self._dead_pane_seat()
        marker = seat_resume_all.hold_marker_path()
        self.assertTrue(marker.startswith(os.environ["HELM_HOME"]), marker)
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w"):
            pass
        with mock.patch.object(seat, "_resume") as resume:
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, err)
        row = self._row(out, "codex")
        self.assertIn("DEAD-PANE", row)
        self.assertIn("HELD", row)
        self.assertNotIn("SKIP", row)
        self.assertEqual(fake.sent, [], "hold forbids the disarm write")
        resume.assert_not_called()
        self.assertIn("FLEET HOLD in effect", out)
        self.assertIn(marker, out)
        self.assertEqual(self._register(d)["handle"], "old")


    def test_a_hold_written_before_this_boot_expired_with_it(self):  # noqa: VACUOUS_ASSERTION — positive controls on the same run: rc 0, the EXPIRED footer naming the marker, the disarm send and the re-stamped handle; the marker's absence is the removal contract, and the during-boot arm proves the identical fixture with a newer mtime keeps it
        """MUTATION: drop the mtime-vs-btime bound (any marker holds) — the
        row reads HELD, nothing is sent, the marker survives."""
        d, fake = self._dead_pane_seat()
        marker = seat_resume_all.hold_marker_path()
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        open(marker, "w").close()
        os.utime(marker, (BOOT - 10, BOOT - 10))
        rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("EXPIRED", out)
        self.assertIn("removed", out)
        self.assertFalse(os.path.exists(marker), "an expired hold is removed")
        self.assertNotIn("HELD", self._row(out, "codex"))
        self.assertEqual(fake.sent[0][1], seat_resume_all.disarm_line())
        self.assertEqual(self._register(d)["handle"], "shell-1")

    def test_a_hold_written_during_this_boot_holds(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 0, the HELD row, the footer naming the marker, the marker still on disk; the empty sent log is the hold contract and the expired-hold arm proves the identical fixture with an older mtime does send
        """MUTATION: compare with <= or read the wrong clock — a marker
        stamped after boot reads expired and the fleet relaunches under a
        live hold."""
        d, fake = self._dead_pane_seat()
        marker = seat_resume_all.hold_marker_path()
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        open(marker, "w").close()
        os.utime(marker, (BOOT + 10, BOOT + 10))
        rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("HELD", self._row(out, "codex"))
        self.assertIn("FLEET HOLD in effect", out)
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(fake.sent, [])
        self.assertEqual(self._register(d)["handle"], "old")


# ---------------------------------------------------------------------------
# the timer unit carries the sweep
# ---------------------------------------------------------------------------


class TimerUnitTest(unittest.TestCase):

    def test_the_rendered_unit_runs_the_sweep_not_bare_rebind(self):
        """MUTATION: revert ExecStart to `seat rebind --all --apply`."""
        spath, service, tpath, timer = seat.rebind_timer_units()
        exec_lines = [l for l in service.splitlines()
                      if l.startswith("ExecStart=")]
        self.assertEqual(len(exec_lines), 1, service)
        self.assertTrue(exec_lines[0].endswith(" seat resume --all --apply"),
                        exec_lines[0])
        self.assertNotIn("seat rebind --all --apply", service)
        self.assertTrue(spath.endswith("helm-seat-rebind.service"),
                        "the unit NAME stays so the installed timer keeps working")
        self.assertIn("OnUnitActiveSec=", timer)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# TITLE — the OTHER thing a reboot reverts
# ---------------------------------------------------------------------------
#
# A reboot invalidates the spawn register AND reverts every hand-set pane
# title to an orca-generated summary. The sweep that repairs the first is the
# honest home for the second: it runs after the event that causes both, it has
# already PROVEN which seat holds which pane, and the pane inventory is
# already in its hand — so the title costs it nothing.

class TitleRestampTest(SweepFixture):

    def _live_seat(self, row=None, fake=None):
        """The LIVE registered seat the sweep re-stamps, with the register
        already naming the live pane (the healthy-fleet shape)."""
        d, _ = self._mint()
        self._record(d, handle="shell-1", pty_id="pty-1", worktree_id="w1")
        fake = fake or FakeOrca(rows=[row or LIVE_ROW],
                                resolved={"handle": "shell-1",
                                          "pty_id": "pty-1"})
        current = {"pid": 42, "proc_start": "1", "pane_key": PANE_KEY,
                   "worktree_id": "w1"}
        return d, fake, mock.patch.object(seat, "_live_session_orca_identity",
                                          return_value=(current, None))

    def test_a_live_seat_pane_is_re_titled_with_its_seat_name(self):
        """MUTATION: drop the `orcatitle.restamp_one` call from `rebind_seat`
        — `fake.renamed` goes empty while the row still reports LIVE and
        already current. That one hook serves BOTH this sweep and `seat
        rebind`, which is the reboot verb the ruling names."""
        _d, fake, identity = self._live_seat()
        with identity:
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.renamed, [("shell-1", LIVE_TITLE)])
        row = self._row(out, "codex")
        self.assertIn("LIVE", row)
        self.assertIn(LIVE_TITLE, row)

    def test_a_dry_run_names_the_title_it_would_write_and_writes_none(self):  # noqa: VACUOUS_ASSERTION — rc 0 and the 'would title' row are the unconditional positive controls; the empty rename log IS the dry-run contract, and the arm above drives the same double to a write
        _d, fake, identity = self._live_seat()
        with identity:
            rc, out, err = self._sweep(["--all"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.renamed, [], "a dry run wrote a pane title")
        self.assertIn("would title", self._row(out, "codex"))

    def test_a_pane_already_reading_correctly_is_left_alone_and_SILENT(self):  # noqa: VACUOUS_ASSERTION — rc 0 and the 'already current' row are unconditional positive controls on the same sweep; the empty rename log and the absent note ARE the idempotent-and-quiet contract, proven non-vacuous by the sibling arm whose identical fixture differs only in the pane's title
        """Idempotent AND quiet: this rides a timer, and the owner reads the
        surface it writes to. MUTATION: rename unconditionally, or emit a
        note on the OK case."""
        _d, fake, identity = self._live_seat(
            row=dict(LIVE_ROW, title=LIVE_TITLE))
        with identity:
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.renamed, [])
        row = self._row(out, "codex")
        self.assertIn("already current", row)
        self.assertNotIn("titled", row)

    def test_a_refused_rename_never_fails_the_sweep(self):
        """A tab label may not break the verb that brings a fleet back.
        MUTATION: let `_write` propagate — rc goes non-zero and the table
        stops at this row."""
        _d, fake, identity = self._live_seat()
        fake.rename_error = "terminal_handle_stale"
        with identity:
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, err)
        row = self._row(out, "codex")
        self.assertIn("LIVE", row)
        self.assertIn("not titled", row)
        self.assertIn("terminal_handle_stale", row)
        self.assertIn("1 LIVE", out)

    def test_the_title_restamp_costs_a_healthy_fleet_no_proc_walk(self):  # noqa: VACUOUS_ASSERTION — the MUST-HIT rename assertion is the unconditional positive control — the title write demonstrably happened on this run, so the never-called census doubles are about HOW it was resolved
        """THE COST CONTRACT, on the APPLY path this time. The sweep is a
        timer payload; a title must not buy it a /proc walk per pass.
        MUTATION: resolve the title through `orcaadopt.pane_rows` (which takes
        its own census) instead of the inventory row already in hand."""
        _d, fake, identity = self._live_seat()
        with identity:
            rc, _out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, err)
        # MUST-HIT: the title write really happened on this run, so the
        # never-called census below is about HOW it was resolved.
        self.assertEqual(fake.renamed, [("shell-1", LIVE_TITLE)])
        orcaadopt.claude_processes.assert_not_called()
        sessions.live_sids.assert_not_called()


class HeldFleetTitleTest(SweepFixture):
    """A HOLD STOPS RELAUNCHES, NOT LABELS — a decision, pinned so it is a
    stated one. The marker's contract is "no resume and no disarm write": it
    stops helm typing into panes and minting processes, and it deliberately
    does NOT withhold the register re-stamp on a LIVE seat. A tab rename is
    the same class of act — it wakes nothing, types nothing and starts nothing
    — and a held fleet the owner is watching is exactly the fleet whose labels
    should be right. MUTATION: gate the title on `held` and this arm reddens
    while the sibling hold arms stay green."""

    def test_a_held_fleet_still_gets_its_LIVE_titles(self):
        d, _ = self._mint()
        self._record(d, handle="shell-1", pty_id="pty-1", worktree_id="w1")
        fake = FakeOrca(rows=[LIVE_ROW],
                        resolved={"handle": "shell-1", "pty_id": "pty-1"})
        marker = seat_resume_all.hold_marker_path()
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w"):
            pass
        current = {"pid": 42, "proc_start": "1", "pane_key": PANE_KEY,
                   "worktree_id": "w1"}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(current, None)):
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, err)
        self.assertIn("FLEET HOLD in effect", out)
        self.assertEqual(fake.sent, [], "the hold still forbids a pane write")
        self.assertEqual(fake.renamed, [("shell-1", LIVE_TITLE)])


class AdoptedTitleTest(SweepFixture):
    """THE POPULATION `rebind` HAS NEVER REACHED. A hand-launched seat carries
    no spawn register, so every register-keyed repair skips it — and that is
    where the owner's own coordinating panes live. Their titles revert on a
    reboot exactly like a spawned seat's."""

    ADOPTED = "hc-adopted"

    def _live(self, handle="shell-1"):
        seats.write_roster(self.ADOPTED, session=SID, cwd=self.work)
        info = {"state": orcaadopt.LIVE, "sessions": [SID], "handle": handle,
                "evidence": "pid 42 declares HELM_CHAT_NAME"}
        return _plant_resolve(info)

    def test_a_live_adopted_seat_is_titled_though_it_has_no_register(self):
        """MUTATION: drop the `restamp_one` call from classify_adopted — the
        row still reads LIVE and `renamed` goes empty."""
        fake = FakeOrca(rows=[dict(LIVE_ROW,
                                   worktree="/home/x/dev/proj-wt/a-lane")])
        with self._live():
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, err)
        row = self._row(out, self.ADOPTED)
        self.assertIn("LIVE", row)
        self.assertEqual(fake.renamed, [("shell-1", "hc-adopted")])

    def test_an_adopted_pane_absent_from_the_inventory_is_not_titled(self):  # noqa: VACUOUS_ASSERTION — rc 0 and the LIVE adopted row are the unconditional positive controls; the empty rename log IS the never-guess contract, proven non-vacuous by the sibling arm whose identical fixture names a handle that IS in the inventory
        """The handle resolve() reported is not in `ad.list()` — there is no
        row to read a title or a worktree from, so nothing is written and
        nothing is guessed. MUTATION: title it from the seat name alone."""
        fake = FakeOrca(rows=[LIVE_ROW])
        with self._live(handle="shell-gone"):
            rc, out, err = self._sweep(["--all", "--apply"], fake)
        self.assertEqual(rc, 0, err)
        self.assertIn("LIVE", self._row(out, self.ADOPTED))
        self.assertEqual(fake.renamed, [])
