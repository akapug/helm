#!/usr/bin/env python3
"""rearm tests — the land-to-live compression verb. Hermetic: /proc is a tmp
fixture tree (rearm.PROC patched), HEAD time / systemctl / chat.post / os.kill
are all mocked. No real process is ever signaled, no real unit restarted, no
real room written."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import rearm

BTIME = 1_000_000_000          # fixture boot epoch (/proc/stat btime)
HEAD_TIME = BTIME + 500        # main HEAD committed 500s after boot
HEAD_SHA = "abc1234"


class RearmBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-rearm-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        with open(os.path.join(self.proc, "stat"), "w") as f:
            f.write("cpu  1 2 3\nbtime %d\nprocesses 42\n" % BTIME)
        self._patch(rearm, "PROC", self.proc)
        # default: HEAD resolves; no web unit; nothing gets restarted/posted/killed
        self._patch(rearm, "_head_commit", lambda: (HEAD_SHA, HEAD_TIME))
        self._patch(rearm, "_systemctl_show", lambda unit: None)
        self.restart = mock.Mock(return_value=(True, "restarted"))
        self._patch(rearm, "_systemctl_restart", self.restart)
        self.post = mock.Mock()
        self._patch(rearm.chat, "post", self.post)
        self._patch(rearm.pk, "event", mock.Mock())   # no real ~/.helm write
        self.killed = []
        self._patch(rearm.os, "kill", lambda pid, sig: self.killed.append((pid, sig)))

    def _patch(self, obj, attr, value):
        p = mock.patch.object(obj, attr, value)
        p.start()
        self.addCleanup(p.stop)

    def plant(self, pid, argv, start_after_boot=None, comm="python3", ppid=1):
        """A fake /proc/<pid> with cmdline + (optionally) stat. start_after_boot
        is seconds after BTIME; None => no stat file (unreadable start)."""
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        if start_after_boot is not None:
            ticks = int(start_after_boot * rearm._clk_tck())
            with open(os.path.join(d, "stat"), "w") as f:
                f.write("%d (%s) S %d %s %d 0" % (pid, comm, ppid, "0 " * 17, ticks))
        return d

    def waiter(self, seat):
        return ["python3", "/home/u/.local/bin/helm", "chat", "wait",
                "--seat", seat, "--follow"]

    def bash_wrapper(self, seat):
        return ["/bin/bash", "-c",
                "source /x/snap.sh 2>/dev/null || true && eval 'helm chat wait "
                "--seat %s --follow' < /dev/null && pwd -P >| /tmp/claude-x-cwd"
                % seat]

    def web_show(self, active=True, since_after_boot=100, mainpid=9001):
        return lambda unit: {
            "LoadState": "loaded",
            "ActiveState": "active" if active else "inactive",
            "MainPID": str(mainpid),
            "ActiveEnterTimestamp": "@%d" % (BTIME + since_after_boot)}


# ---------------------------------------------------------------------------
# low-level seams
# ---------------------------------------------------------------------------

class ShapeTest(RearmBase):
    def test_helm_subargv_forms(self):
        self.assertEqual(
            rearm._helm_subargv(["python3", "/x/bin/helm", "chat", "wait"]),
            ["chat", "wait"])
        self.assertEqual(
            rearm._helm_subargv(["python3", "-m", "helm", "chat", "wait"]),
            ["chat", "wait"])
        self.assertEqual(rearm._helm_subargv(["helm", "web"]), ["web"])

    def test_bash_wrapper_is_not_a_helm_invocation(self):
        # the eval'd 'helm chat wait' sits INSIDE one -c argument: no standalone
        # helm token, so it is never mistaken for a waiter.
        self.assertIsNone(rearm._helm_subargv(self.bash_wrapper("codex-2")))

    def test_flag_value(self):
        sub = ["chat", "wait", "--seat", "kimi", "--follow"]
        self.assertEqual(rearm._flag(sub, "--seat"), "kimi")
        self.assertIsNone(rearm._flag(["chat", "wait", "--follow"], "--seat"))

    def test_proc_start_epoch_conversion(self):
        self.plant(101, self.waiter("a"), start_after_boot=100)
        self.assertAlmostEqual(rearm.proc_start_epoch(101), BTIME + 100, places=3)
        self.plant(102, self.waiter("b"))  # no stat -> unreadable
        self.assertIsNone(rearm.proc_start_epoch(102))


# ---------------------------------------------------------------------------
# stale classification
# ---------------------------------------------------------------------------

class StaleTest(RearmBase):
    def test_stale_and_current_classification(self):
        self.plant(201, self.waiter("codex-3"), start_after_boot=100)   # < HEAD
        self.plant(202, self.waiter("kimi"), start_after_boot=900)      # > HEAD
        w = {x["pid"]: x for x in rearm.scan()["waiters"]}
        self.assertEqual(w[201]["status"], "STALE")
        self.assertTrue(w[201]["signalable"])
        self.assertEqual(w[201]["seat"], "codex-3")
        self.assertEqual(w[202]["status"], "current")
        self.assertFalse(w[202]["signalable"])

    def test_unresolvable_head_never_classes_stale(self):
        self._patch(rearm, "_head_commit", lambda: (None, None))
        self.plant(210, self.waiter("codex-3"), start_after_boot=100)
        w = rearm.scan()["waiters"][0]
        self.assertEqual(w["status"], "UNKNOWN")
        self.assertFalse(w["signalable"])   # git down => the fail-safe: no signal

    def test_fresh_waiter_within_skew_band_is_not_stale(self):
        # whole-second flooring of btime/%ct can push a genuinely-fresh post-land
        # start just under HEAD; the SKEW_S band keeps it classed current so a
        # fresh waiter (already on new code) is never mis-killed.
        self.plant(220, self.waiter("codex-3"),
                   start_after_boot=500 - 1)   # HEAD is BTIME+500; 1s under HEAD
        w = rearm.scan()["waiters"][0]
        self.assertEqual(w["status"], "current")
        self.assertFalse(w["signalable"])


# ---------------------------------------------------------------------------
# exact-shape match + skip-on-uncertain
# ---------------------------------------------------------------------------

class ShapeMatchTest(RearmBase):
    def test_only_helm_chat_wait_shape_is_a_waiter(self):
        self.plant(301, self.waiter("codex-3"), start_after_boot=100)   # waiter
        self.plant(302, self.bash_wrapper("codex-3"), start_after_boot=100)  # shell
        self.plant(303, ["vim", "/etc/hosts"], start_after_boot=100)    # not helm
        self.plant(304, ["python3", "/x/helm", "web", "--port", "7433"],
                   start_after_boot=100)                                # web verb
        plan = rearm.scan()
        self.assertEqual([w["pid"] for w in plan["waiters"]], [301])
        # the bash wrapper is neither a waiter NOR advisory (no helm token)
        self.assertNotIn(302, [a["pid"] for a in plan["advisory"]])

    def test_uncertain_waiter_is_reported_not_signalable(self):
        # STALE by start, but no --seat token: attribution uncertain -> SKIP.
        self.plant(311, ["python3", "/x/helm", "chat", "wait", "--follow"],
                   start_after_boot=100)
        w = rearm.scan()["waiters"][0]
        self.assertEqual(w["status"], "STALE")
        self.assertIsNone(w["seat"])
        self.assertFalse(w["signalable"])

    def test_unreadable_start_waiter_is_unknown_not_signalable(self):
        self.plant(312, self.waiter("codex-3"))   # no stat -> start None
        w = rearm.scan()["waiters"][0]
        self.assertEqual(w["status"], "UNKNOWN")
        self.assertFalse(w["signalable"])


# ---------------------------------------------------------------------------
# advisory
# ---------------------------------------------------------------------------

class AdvisoryTest(RearmBase):
    def test_pre_head_helm_procs_are_advisory_never_signaled(self):
        self.plant(401, ["python3", "/x/helm", "router", "up"],
                   start_after_boot=100)                    # pre-HEAD -> advisory
        self.plant(402, ["python3", "/x/helm", "seat", "up", "codex"],
                   start_after_boot=900)                    # post-HEAD -> not
        plan = rearm.scan()
        self.assertEqual([a["pid"] for a in plan["advisory"]], [401])
        self.assertEqual(plan["advisory"][0]["verb"], "router")
        # advisory pids are never in the signal set even on --apply
        actions = rearm.apply(plan)
        self.assertEqual(actions["signaled"], [])
        self.assertEqual(self.killed, [])

    def test_web_mainpid_excluded_from_advisory(self):
        self._patch(rearm, "_systemctl_show",
                    self.web_show(active=True, since_after_boot=100, mainpid=9001))
        self.plant(9001, ["python3", "/x/helm", "web", "--port", "7433"],
                   start_after_boot=100)
        plan = rearm.scan()
        self.assertNotIn(9001, [a["pid"] for a in plan["advisory"]])


# ---------------------------------------------------------------------------
# web unit staleness
# ---------------------------------------------------------------------------

class WebTest(RearmBase):
    def test_web_stale_when_active_and_predates_head(self):
        self._patch(rearm, "_systemctl_show",
                    self.web_show(active=True, since_after_boot=100))
        web = rearm.scan()["web"]
        self.assertTrue(web["active"] and web["stale"])

    def test_web_current_when_started_after_head(self):
        self._patch(rearm, "_systemctl_show",
                    self.web_show(active=True, since_after_boot=900))
        web = rearm.scan()["web"]
        self.assertTrue(web["active"])
        self.assertFalse(web["stale"])

    def test_web_inactive_never_stale(self):
        self._patch(rearm, "_systemctl_show",
                    self.web_show(active=False, since_after_boot=100))
        web = rearm.scan()["web"]
        self.assertFalse(web["active"])
        self.assertFalse(web["stale"])


# ---------------------------------------------------------------------------
# apply plumbing (mocks) — the OWNED mutation
# ---------------------------------------------------------------------------

class ApplyTest(RearmBase):
    def test_dry_run_mutates_nothing(self):
        self.plant(501, self.waiter("codex-3"), start_after_boot=100)  # stale
        self._patch(rearm, "_systemctl_show",
                    self.web_show(active=True, since_after_boot=100))   # web stale
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rearm.cmd_rearm([])
        self.assertEqual(rc, 0)
        self.post.assert_not_called()
        self.restart.assert_not_called()
        self.assertEqual(self.killed, [])
        self.assertIn("1 waiter stale, web stale", out.getvalue())

    def test_apply_announces_first_then_signals_only_stale_seated(self):
        order = []
        self.post.side_effect = lambda *a, **k: order.append("post")
        self._patch(rearm.os, "kill",
                    lambda pid, sig: order.append(("kill", pid)) or
                    self.killed.append((pid, sig)))
        self.plant(601, self.waiter("codex-3"), start_after_boot=100)  # stale+seat
        self.plant(602, self.waiter("kimi"), start_after_boot=900)     # current
        self.plant(603, ["python3", "/x/helm", "chat", "wait", "--follow"],
                   start_after_boot=100)                               # stale,no seat
        self.plant(604, self.bash_wrapper("codex-3"), start_after_boot=100)  # shell
        plan = rearm.scan()
        actions = rearm.apply(plan)
        # announce is one ambient row, posted BEFORE any kill
        self.assertTrue(actions["announced"])
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.post.call_args.kwargs.get("ambient"), True)
        self.assertEqual(self.post.call_args.kwargs.get("room"), "main")
        self.assertEqual(order[0], "post")
        self.assertTrue(all(o[0] == "kill" for o in order[1:]))
        # ONLY the stale, seated waiter (601) is signaled — with SIGTERM
        import signal
        self.assertEqual(self.killed, [(601, signal.SIGTERM)])
        self.assertEqual(actions["signaled"], [601])
        # the announce row carries the exact re-arm incantation + the seat
        txt = self.post.call_args.args[0]
        self.assertIn("Monitor(command:", txt)
        self.assertIn("--follow", txt)
        self.assertIn("codex-3", txt)

    def test_apply_restarts_web_iff_active_and_stale(self):
        self._patch(rearm, "_systemctl_show",
                    self.web_show(active=True, since_after_boot=100))  # stale
        actions = rearm.apply(rearm.scan())
        self.restart.assert_called_once_with(rearm.WEB_UNIT)
        self.assertTrue(actions["web_restarted"])

    def test_apply_does_not_restart_current_web(self):
        self._patch(rearm, "_systemctl_show",
                    self.web_show(active=True, since_after_boot=900))  # current
        rearm.apply(rearm.scan())
        self.restart.assert_not_called()

    def test_apply_noop_when_nothing_stale_posts_no_announce(self):
        self.plant(701, self.waiter("kimi"), start_after_boot=900)     # current only
        actions = rearm.apply(rearm.scan())
        self.assertFalse(actions["announced"])
        self.post.assert_not_called()
        self.assertEqual(self.killed, [])

    def test_apply_fails_closed_when_announce_raises(self):
        # a chat-node hiccup: the announce raises. Fail-closed — no waiter is
        # SIGTERMed and no web unit restarted (the disruption never lands
        # unexplained), and the error is recorded loudly, not swallowed.
        self.post.side_effect = RuntimeError("room node unreachable")
        self.plant(621, self.waiter("codex-3"), start_after_boot=100)  # stale+seat
        self._patch(rearm, "_systemctl_show",
                    self.web_show(active=True, since_after_boot=100))   # web stale
        actions = rearm.apply(rearm.scan())
        self.assertFalse(actions["announced"])
        self.assertEqual(actions["announce_error"], "room node unreachable")
        self.assertEqual(self.killed, [])
        self.assertEqual(actions["signaled"], [])
        self.restart.assert_not_called()
        self.assertFalse(actions["web_restarted"])

    def test_recycled_pid_is_revalidated_and_skipped(self):
        # a classified stale waiter exits and its pid is recycled to an unrelated
        # process during the announce round-trip: the starttime changes, so the
        # kill-time re-check spares the innocent recycled pid.
        self.plant(631, self.waiter("codex-3"), start_after_boot=100)  # stale+seat
        plan = rearm.scan()
        self.assertEqual([w["pid"] for w in plan["waiters"] if w["signalable"]],
                         [631])
        with open(os.path.join(self.proc, "631", "stat"), "w") as f:   # new proc
            f.write("631 (python3) S 1 %s %d 0"
                    % ("0 " * 17, 777 * rearm._clk_tck()))
        actions = rearm.apply(plan)
        self.assertEqual(self.killed, [])
        self.assertEqual(actions["signaled"], [])
        self.assertEqual(actions["skipped"], [631])

    def test_reshaped_pid_is_revalidated_and_skipped(self):
        # same pid + starttime (an exec preserves both) but the cmdline no longer
        # reads as `chat wait`: the shape half of the re-check skips it.
        self.plant(632, self.waiter("kimi"), start_after_boot=100)     # stale+seat
        plan = rearm.scan()
        with open(os.path.join(self.proc, "632", "cmdline"), "wb") as f:
            f.write(b"\0".join([b"vim", b"/etc/hosts"]) + b"\0")
        actions = rearm.apply(plan)
        self.assertEqual(self.killed, [])
        self.assertEqual(actions["skipped"], [632])

    def test_second_apply_is_idempotent_noop(self):
        d = self.plant(801, self.waiter("codex-3"), start_after_boot=100)  # stale
        first = rearm.apply(rearm.scan())
        self.assertEqual(first["signaled"], [801])
        # the waiter died and its owner re-armed on new code: the proc is gone
        shutil.rmtree(d)
        self.post.reset_mock()
        second = rearm.apply(rearm.scan())
        self.assertFalse(second["announced"])
        self.assertEqual(second["signaled"], [])
        self.post.assert_not_called()


# ---------------------------------------------------------------------------
# report / summary line
# ---------------------------------------------------------------------------

class ReportTest(RearmBase):
    def test_summary_line_and_advisory_recipe(self):
        self.plant(901, self.waiter("codex-3"), start_after_boot=100)   # stale
        self.plant(902, ["python3", "/x/helm", "router", "up"],
                   start_after_boot=100)                                # advisory
        self._patch(rearm, "_systemctl_show",
                    self.web_show(active=True, since_after_boot=100))    # web stale
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rearm.cmd_rearm([])
        text = out.getvalue()
        self.assertIn("helm rearm: 1 waiter stale, web stale, 1 advisory", text)
        self.assertIn("helm seat down <seat> && helm seat up <seat>", text)
        self.assertIn("[--apply restarts]", text)

    def test_json_output_is_wellformed(self):
        import json
        self.plant(911, self.waiter("codex-3"), start_after_boot=100)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rearm.cmd_rearm(["--json"])
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["head_sha"], HEAD_SHA)
        self.assertEqual(doc["waiters"][0]["seat"], "codex-3")

    def test_bad_flag_rejected(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = rearm.cmd_rearm(["--nuke"])
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm rearm", err.getvalue())


if __name__ == "__main__":
    unittest.main()
