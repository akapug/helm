#!/usr/bin/env python3
"""What a RESTART is obliged to restore (rows #153 + #155, one seam).

#153 — the WAKE PATH. A seat's inbox beacon is a PER-SESSION Monitor process:
it survives a compaction (which compacts the conversation, not the process)
but not a resume/respawn, and a resumed session gets no TURN to act on the
SessionStart arm directive. Measured 2026-08-03: six of seven restored seats
came back alive and DEAF, and a fleet-wide brief reached nobody for hours. So
`helm seat resume` must deliver the re-arm turn itself — and when it cannot,
it must REFUSE TO REPORT THE RESTART COMPLETE rather than hand back a deaf
seat with a clean rc 0.

#155 — the CWD. Resume respawned at whatever cwd the newest transcript
sniffed, with no override; both measured workarounds (editing spawn.json's
worktree, rehoming the transcript slug) do not take because neither is what
resume reads. Not cosmetic: cwd decides which BINARY and which TREE the seat
acts on (PATH helm symlinks to the shared checkout's bin/helm). So: --cwd
overrides, and a recorded cwd that no longer exists or sits in a REMOVED
worktree refuses loudly instead of spawning somewhere stale.

Fixtures are synthetic (SeatResumeTest's shape): HELM_HOME is a tmp tree, the
adapter is a fake that records sends, no real pane or metaharness is touched.
`seats.TEMP_ROOTS` is patched wherever a fixture path under /tmp must not be
classed throwaway (tests/test_resume_cwd.py's law).
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import harness, orcaadopt, seat, seats


# A pane whose composer is EMPTY — one that took its turn. Synthetic, but a
# real frame's shape: `submit` proves delivery by READING THE COMPOSER BACK,
# so a double returning "" models an UNREADABLE pane (UNKNOWN), not a
# working one.
ADVANCED_PANE = "\n".join(("─" * 40, "❯", "─" * 40,
                           "  opus-5 | ~/dev/example/repo",
                           "  ⏵⏵ bypass permissions on"))


class FakeAdapter(harness._CLIAdapter):
    name, path = "fake", "/bin/fake"

    def __init__(self, rows=()):
        self.rows, self.spawned, self.sent, self.stopped = \
            list(rows), [], [], []
        self.typed = {}

    def spawn(self, command, title=None, cwd=None):
        self.spawned.append((command, title, cwd))
        return "pane-1"

    def list(self):
        return list(self.rows)

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

    def stop(self, handle):
        self.typed.pop(handle, None)
        self.stopped.append(handle)


class DeafAdapter(FakeAdapter):
    """The pane spawns fine; the re-arm keystrokes cannot be delivered."""

    def send(self, handle, text, enter=True):
        raise harness.HarnessError("send seam down")


class ResumeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-wakepath-")
        self._env = {k: os.environ.get(k)
                     for k in ("HELM_HOME", "MELD_HOME",
                               "HELM_SPAWN_SEND_DELAY",
                               "HELM_SUBMIT_SETTLE_S")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ.pop("MELD_HOME", None)
        os.environ["HELM_SPAWN_SEND_DELAY"] = "0"
        os.environ["HELM_SUBMIT_SETTLE_S"] = "0"
        self.timer = mock.patch.object(seat, "_ensure_autocompact_timer")
        self.timer.start()
        self.addCleanup(self.timer.stop)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mint(self, seat_name="codex", family="codex"):
        d = seat._instance_dir(family, seat_name)
        os.makedirs(d, exist_ok=True)
        launch = os.path.join(d, "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env FAKE=1 claude \"$@\"\n")
        os.chmod(launch, 0o700)
        return d, launch

    def _plant_session(self, d, cwd,
                       sid="0199aaaa-bbbb-cccc-dddd-eeeeffff0000"):
        proj = os.path.join(d, "claude", "projects", "-spot")
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, sid + ".jsonl"), "w") as f:
            f.write(json.dumps({"cwd": cwd, "type": "user"}) + "\n")
        return sid

    def _resume(self, args, adapter):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_write_launch_assets") as wla, \
                mock.patch.object(harness, "detect", return_value=adapter), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["resume"] + list(args))
        return rc, out.getvalue(), err.getvalue(), wla


# ---------------------------------------------------------------------------
# #153 — a restart re-arms the wake path or refuses to call itself complete
# ---------------------------------------------------------------------------

class ResumeRearmsTheWakePathTest(ResumeFixture):

    def test_resume_sends_the_rearm_turn_into_the_pane(self):
        """The gap in one sentence: SessionStart directs the arm, but a
        resume gives the seat no TURN to act on it — so resume itself must
        inject that turn, with the exact incantation."""
        self._mint()
        fake = FakeAdapter()
        rc, out, err, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        # THE SPLIT: the prompt is typed with NO Enter, then a BARE ENTER is
        # its own act. `enter=True` on one call was the OLD proof and it was
        # exactly the insufficient one — it says the metaharness took the
        # bytes, which was true of every pane the owner found holding its next
        # instruction unsent. Delivery is now proven by READING THE COMPOSER
        # BACK (the double returns an advanced pane), and rc 0 above is that
        # proof arriving.
        self.assertEqual(len(fake.sent), 2)
        handle, text, enter = fake.sent[0]
        self.assertEqual(handle, "pane-1")
        self.assertFalse(enter, "the text leg must NOT carry Enter")
        self.assertEqual(fake.sent[1], ("pane-1", "", True),
                         "the submit is its own act with its own receipt")
        self.assertEqual(
            text, "Run `helm seat boot-brief --rearm` and follow it.")
        self.assertIn("wake-path re-arm prompt sent", out)

    def test_a_resume_that_cannot_rearm_refuses_to_report_complete(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1, ALIVE AND DEAF, the incantation in err; the absences (pane not stopped, no success line) ARE the contract, and mutation 2 (swallow the failure) reddens the rc arm
        """A deaf seat reads exactly like an idle one from outside; rc 0 here
        is the lie that let a restored fleet sit unreachable for hours."""
        self._mint()
        deaf = DeafAdapter()
        rc, out, err, _ = self._resume(["codex"], deaf)
        self.assertEqual(rc, 1)
        self.assertIn("ALIVE AND DEAF", err)
        self.assertIn("helm chat wait --seat codex --follow", err)
        self.assertNotIn("resumed codex via fake", out)
        # the pane itself is up and mid-session: refusing the REPORT must not
        # escalate to killing the seat a second time
        self.assertEqual(deaf.stopped, [])

    def test_the_rearm_prompt_is_one_pane_safe_line_keyed_to_the_seat(self):
        text = seat.rearm_prompt("codex-2")
        self.assertNotIn("\n", text)
        self.assertIn("helm chat wait --seat codex-2 --follow", text)
        self.assertIn("timeout_ms: 1800000", text)   # the harness's deadline
        self.assertIn("expires every 30 minutes", text)
        self.assertIn("ToolSearch", text)   # deferred-tool reality
        self.assertIn("background", text)   # the non-beacon substitute, named

    def test_boot_brief_rearm_expands_to_the_full_monitor_directive(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ,
                             {"HELM_CHAT_NAME": "seat-b",
                              "HELM_SEAT_ROLE": "lead"}, clear=False), \
                contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["boot-brief", "--rearm"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("helm chat wait --seat seat-b --follow", text)
        self.assertIn("Monitor", text)
        self.assertIn("RELAUNCHED", text)
        self.assertIn("role lead", text)
        self.assertIn("helm task list", text)
        self.assertIn("subagents/workflows", text)
        self.assertIn("pre-authorized to use Agent subagents", text)
        self.assertIn("never ask the owner for permission to delegate", text)
        self.assertIn("positive live/recent delegation evidence", text)
        self.assertIn("UNKNOWN still blocks", text)
        self.assertIn("Do not park while eligible work queues", text)

    def test_the_rearm_rides_the_boot_grace_knob(self):
        """The keystrokes must wait out claude's boot the same way spawn's
        onboarding does — the knob is read, not a hardcoded zero."""
        self._mint()
        os.environ["HELM_SPAWN_SEND_DELAY"] = "0.01"
        with mock.patch.object(seat.time, "sleep") as zzz:
            rc, _, err, _ = self._resume(["codex"], FakeAdapter())
        self.assertEqual(rc, 0, err)
        # `seat.time` is Python's shared time module: patching it also observes
        # bounded sleeps in imported delivery ledgers. This contract is the
        # configured boot grace, not exclusivity over every process sleep.
        self.assertIn(mock.call(0.01), zzz.call_args_list)


class AdoptedResumeRefusesCompleteOnFailedKickTest(unittest.TestCase):
    """The orca-adopted twin: the kick IS that path's re-arm turn (its brief
    carries the beacon directive), so a failed kick is an incomplete restart,
    not an rc-0 footnote."""

    ROW = {"i": "sid-1234abcd", "h": "claude", "cwd": "/w", "mt": 1}

    def _resume(self, kicked):
        with mock.patch.object(orcaadopt, "seat_liveness",
                               return_value=(orcaadopt.DEAD, "dead")), \
                mock.patch.object(orcaadopt, "newest_session_row",
                                  return_value=(dict(self.ROW), None)), \
                mock.patch("helm.sessions.resume_warnings", return_value=[]), \
                mock.patch("helm.sessions.credhome_for", return_value=None), \
                mock.patch("helm.sessions.kick_resumed",
                           return_value=kicked), \
                mock.patch("helm.sessions.spawn_resume",
                           return_value=("/p.sh", "h9", "orca")), \
                mock.patch.object(harness, "detect", return_value=object()):
            return orcaadopt.resume("console-design")

    def test_failed_kick_is_rc_1_and_says_deaf(self):
        rc, lines = self._resume(kicked=False)
        self.assertEqual(rc, 1)
        self.assertIn("DEAF", " ".join(lines))

    def test_delivered_kick_stays_rc_0(self):  # noqa: VACUOUS_ASSERTION — the non-regression direction of the sibling failed-kick test; mutation 5 (rc forced 0) reddens the sibling, this arm pins that a delivered kick was not broken in the fix
        rc, _ = self._resume(kicked=True)
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# #155 — --cwd overrides; a stale recorded cwd refuses loudly
# ---------------------------------------------------------------------------

class ResumeCwdOverrideTest(ResumeFixture):

    def test_cwd_override_wins_over_the_sniffed_cwd(self):  # noqa: VACUOUS_ASSERTION — positive controls on the spawned cwd and the spawn.json worktree; mutation 4 (override ignored) reddens both
        d, _ = self._mint()
        sniffed = os.path.join(self.tmp, "sniffed")
        chosen = os.path.join(self.tmp, "chosen")
        os.makedirs(sniffed)
        os.makedirs(chosen)
        self._plant_session(d, sniffed)
        fake = FakeAdapter()
        # TEMP_ROOTS + safe_cwd are BOTH pinned to the fixture, and not
        # because the green path needs them: under the regression this test
        # exists to catch (override ignored), an unpinned run classes the
        # /tmp fixture as throwaway and _resume_cwd PROVISIONS A WORKTREE IN
        # THE REAL CHECKOUT resolved from the invoker's cwd — measured doing
        # exactly that during this lane's own mutation pass (it refreshed a
        # live seat's home). A test must fail RED, never reach live state.
        with mock.patch.object(seats, "TEMP_ROOTS", ("/dev/shm",)), \
                mock.patch.object(seats, "safe_cwd", return_value=self.tmp):
            rc, out, err, _ = self._resume(["codex", "--cwd", chosen], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.spawned[0][2], chosen)
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["worktree"], chosen)

    def test_a_recorded_cwd_that_no_longer_exists_refuses_loudly(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1, REFUSING, the reason, the --cwd rescue; the absences (no spawn, no reap, no remint) ARE the blocker, and mutation 3 (gate disabled) reddens the rc arm
        d, _ = self._mint()
        self._plant_session(d, os.path.join(self.tmp, "was-here", "gone"))
        # NOT under a TEMP_ROOTS patch: the path must simply not exist
        shutil.rmtree(os.path.join(self.tmp, "was-here"), ignore_errors=True)
        fake = FakeAdapter()
        with mock.patch.object(seats, "TEMP_ROOTS", ("/dev/shm",)):
            rc, out, err, wla = self._resume(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("REFUSING", err)
        self.assertIn("no longer exists", err)
        self.assertIn("--cwd", err)          # the rescue is named, pasteable
        self.assertEqual(fake.spawned, [])
        self.assertEqual(fake.stopped, [])   # refusal precedes the reap: the
        wla.assert_not_called()              # seat keeps the pane it still has

    def test_a_recorded_cwd_in_a_removed_worktree_refuses_loudly(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1 + the REMOVED-worktree reason; absence of a spawn IS the blocker; mutation 3 reddens it
        d, _ = self._mint()
        lane = os.path.join(self.tmp, "proj-wt", "lane-x")
        os.makedirs(lane)
        with open(os.path.join(lane, ".git"), "w") as f:
            f.write("gitdir: %s\n"
                    % os.path.join(self.tmp, "proj", ".git", "worktrees",
                                   "lane-x"))
        self._plant_session(d, lane)
        fake = FakeAdapter()
        with mock.patch.object(seats, "TEMP_ROOTS", ("/dev/shm",)):
            rc, _, err, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("REMOVED worktree", err)
        self.assertEqual(fake.spawned, [])

    def test_a_cwd_override_that_does_not_exist_refuses_too(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 1 + REFUSING; absence of a spawn IS the blocker; mutation 3 reddens it
        d, _ = self._mint()
        real = os.path.join(self.tmp, "real")
        os.makedirs(real)
        self._plant_session(d, real)
        fake = FakeAdapter()
        rc, _, err, _ = self._resume(
            ["codex", "--cwd", os.path.join(self.tmp, "nope")], fake)
        self.assertEqual(rc, 1)
        self.assertIn("REFUSING", err)
        self.assertEqual(fake.spawned, [])

    def test_cwd_without_a_value_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["resume", "codex", "--cwd"])
        self.assertEqual(rc, 2)
        self.assertIn("wants a value", err.getvalue())

    def test_an_adopted_seat_refuses_cwd_honestly_not_silently(self):  # noqa: VACUOUS_ASSERTION — positive controls: rc 2 + 'not supported'; the absence (orcaadopt.resume never called) IS the honesty being pinned
        err = io.StringIO()
        with mock.patch.object(orcaadopt, "resolve", return_value=object()), \
                mock.patch.object(orcaadopt, "resume") as res, \
                mock.patch.object(seat, "_seat_family",
                                  return_value=(None, "unknown seat")), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["resume", "ghost", "--cwd", self.tmp])
        self.assertEqual(rc, 2)
        self.assertIn("not supported", err.getvalue())
        res.assert_not_called()


class StaleResumeCwdPredicateTest(unittest.TestCase):
    """The predicate alone — file reads only, fail-open off every unprovable
    branch (the `_resume_cwd` law: refusing on a guess blocks real resumes)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-stalecwd-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_nothing_and_a_plain_dir_are_usable(self):  # noqa: VACUOUS_ASSERTION — absence of a verdict IS the fail-open law; the positive arms are this class's stale-reason siblings
        self.assertIsNone(seat._stale_resume_cwd(None))
        self.assertIsNone(seat._stale_resume_cwd(""))
        self.assertIsNone(seat._stale_resume_cwd(self.tmp))

    def test_a_missing_path_is_stale(self):
        self.assertEqual(
            seat._stale_resume_cwd(os.path.join(self.tmp, "gone")),
            "it no longer exists")

    def test_a_checkout_root_with_a_real_git_dir_is_usable(self):  # noqa: VACUOUS_ASSERTION — absence of a verdict IS the fail-open law; positive arms are the stale-reason siblings
        os.makedirs(os.path.join(self.tmp, "repo", ".git"))
        self.assertIsNone(
            seat._stale_resume_cwd(os.path.join(self.tmp, "repo")))

    def test_a_linked_worktree_whose_gitdir_survives_is_usable(self):  # noqa: VACUOUS_ASSERTION — absence of a verdict IS the fail-open law; the dangling-gitdir sibling is the positive arm on the same .git file shape
        admin = os.path.join(self.tmp, "repo", ".git", "worktrees", "lane")
        os.makedirs(admin)
        lane = os.path.join(self.tmp, "repo-wt", "lane")
        os.makedirs(lane)
        with open(os.path.join(lane, ".git"), "w") as f:
            f.write("gitdir: %s\n" % admin)
        self.assertIsNone(seat._stale_resume_cwd(lane))

    def test_a_dangling_gitdir_is_a_removed_worktree(self):
        lane = os.path.join(self.tmp, "repo-wt", "lane")
        os.makedirs(lane)
        with open(os.path.join(lane, ".git"), "w") as f:
            f.write("gitdir: %s\n" % os.path.join(self.tmp, "repo", ".git",
                                                  "worktrees", "lane"))
        why = seat._stale_resume_cwd(lane)
        self.assertIsNotNone(why)
        self.assertIn("REMOVED worktree", why)

    def test_a_subdir_of_a_removed_worktree_is_caught_by_the_walk_up(self):
        lane = os.path.join(self.tmp, "repo-wt", "lane")
        deep = os.path.join(lane, "src", "inner")
        os.makedirs(deep)
        with open(os.path.join(lane, ".git"), "w") as f:
            f.write("gitdir: /nowhere/at/all\n")
        why = seat._stale_resume_cwd(deep)
        self.assertIsNotNone(why)
        self.assertIn("REMOVED worktree", why)

    def test_an_unreadable_or_empty_git_file_is_not_evidence(self):  # noqa: VACUOUS_ASSERTION — absence of a verdict IS the fail-open law; a malformed .git file must never convict, and the dangling sibling proves the detector fires
        lane = os.path.join(self.tmp, "repo-wt", "lane")
        os.makedirs(lane)
        with open(os.path.join(lane, ".git"), "w") as f:
            f.write("not a gitdir line\n")
        self.assertIsNone(seat._stale_resume_cwd(lane))


if __name__ == "__main__":
    unittest.main()
