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

    def plant(self, pid, argv, start_after_boot=None, comm="python3", ppid=1,
              environ=None):
        """A fake /proc/<pid> with cmdline + (optionally) stat. start_after_boot
        is seconds after BTIME; None => no stat file (unreadable start).

        `environ` is a dict written as the NUL-separated block the kernel
        exposes, or None for NO environ FILE AT ALL — which is the unreadable
        case, distinct from an environ that simply declares no seat.
        """
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        if environ is not None:
            with open(os.path.join(d, "environ"), "wb") as f:
                f.write(b"".join(("%s=%s\0" % kv).encode()
                                 for kv in sorted(environ.items())))
        if start_after_boot is not None:
            ticks = int(start_after_boot * rearm._clk_tck())
            with open(os.path.join(d, "stat"), "w") as f:
                f.write("%d (%s) S %d %s %d 0" % (pid, comm, ppid, "0 " * 17, ticks))
        return d

    def waiter(self, seat):
        return ["python3", "/home/tester/.local/bin/helm", "chat", "wait",
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

    def test_a_declared_seat_is_read_for_the_REPORT_and_not_for_the_SIGNAL(self):
        """THE TWO SOURCES ARE NOT INTERCHANGEABLE AND THE SPLIT IS THE FIX.
        argv is what the operator WROTE; environ is what the process
        INHERITED. A wrong address here costs a SIGTERM in another seat's
        waiter, so ownership-for-signalling stays argv — while the operator
        still needs to know WHOSE waiter is holding pre-HEAD code, which is a
        reporting question and the one this row was filed for.

        Measured on the live host when this was written: 14 waiters, 13
        carrying --seat and one carrying none but declaring a seat in its
        environ, so the case is not hypothetical."""
        self.plant(321, ["python3", "/x/helm", "chat", "wait", "--follow"],
                   start_after_boot=100,
                   environ={"HELM_CHAT_NAME": "seat-b", "PATH": "/usr/bin"})
        w = rearm.scan()["waiters"][0]
        self.assertEqual(w["seat_declared"], "seat-b",
                         "the report cannot name a waiter that names itself")
        # THE HALF THAT MUST NOT MOVE. Naming it is not authorizing it.
        self.assertIsNone(w["seat"],
                          "the declared name reached the argv-only field that "
                          "decides who is signaled")
        self.assertFalse(w["signalable"],
                         "a waiter with no --seat became signalable because "
                         "its environ named a seat")

    def test_an_environ_that_cannot_be_read_is_not_an_absent_seat(self):
        """NEGATIVE AND UNREADABLE MUST NOT SHARE A VALUE. "declares no seat"
        is a fact about the process; "could not read the environ" is a fact
        about the probe, and collapsing the second into the first reports an
        absence nobody measured."""
        self.plant(322, ["python3", "/x/helm", "chat", "wait", "--follow"],
                   start_after_boot=100)                    # no environ file
        self.plant(323, ["python3", "/x/helm", "chat", "wait", "--follow"],
                   start_after_boot=100, environ={"PATH": "/usr/bin"})
        seen = {w["pid"]: w["seat_declared"] for w in rearm.scan()["waiters"]}
        self.assertEqual(seen[322], rearm.ENVIRON_UNREADABLE,
                         "an environ that could not be read is reported as a "
                         "process that declares no seat")
        self.assertIsNone(seen[323],
                          "a readable environ with no HELM_CHAT_NAME is "
                          "reported as a failed read")

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
        # codex-3 keeps a SECOND, current beacon so 601 is not its last wake
        # path — otherwise the spare-the-last rule holds 601 back and this arm
        # measures the sparing instead of the announce-then-signal ordering it
        # was written for. A seat that can afford to lose a waiter is the
        # precondition of every signalling mechanic below.
        self.plant(605, self.waiter("codex-3"), start_after_boot=900)  # current
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
        self.assertIn('helm chat wait --seat <your-seat> --follow"', txt)
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
        self.plant(639, self.waiter("codex-3"), start_after_boot=900)  # current sibling:
        self.plant(640, self.waiter("codex-3"), start_after_boot=100)  # POSITIVE CONTROL:
        # a stale sibling that IS signalled in this same pass, so "killed == []"
        # below measures the pid re-check and not a harness incapable of a kill.
        # 631 must not be codex-3's LAST wake path, or spare-the-last holds it
        # back and the pid-recycle re-check under test never runs at all.
        plan = rearm.scan()
        self.assertEqual([w["pid"] for w in plan["waiters"] if w["signalable"]],
                         [631, 640])   # 640 is the control; both are signalable
        with open(os.path.join(self.proc, "631", "stat"), "w") as f:   # new proc
            f.write("631 (python3) S 1 %s %d 0"
                    % ("0 " * 17, 777 * rearm._clk_tck()))
        actions = rearm.apply(plan)
        self.assertEqual([p for p, _s in self.killed], [640])   # control fired
        self.assertEqual(actions["signaled"], [640])            # 631 did NOT
        self.assertEqual(actions["skipped"], [631])
        self.assertEqual(actions["skipped"], [631])

    def test_reshaped_pid_is_revalidated_and_skipped(self):
        # same pid + starttime (an exec preserves both) but the cmdline no longer
        # reads as `chat wait`: the shape half of the re-check skips it.
        self.plant(632, self.waiter("kimi"), start_after_boot=100)     # stale+seat
        self.plant(638, self.waiter("kimi"), start_after_boot=900)     # current sibling
        self.plant(637, self.waiter("kimi"), start_after_boot=100)     # POSITIVE CONTROL:
        # stale sibling, signalled in the same pass (see the recycled arm above)
        # (as above: keep 632 signalable so the SHAPE re-check is what is measured)
        plan = rearm.scan()
        with open(os.path.join(self.proc, "632", "cmdline"), "wb") as f:
            f.write(b"\0".join([b"vim", b"/etc/hosts"]) + b"\0")
        actions = rearm.apply(plan)
        self.assertEqual([p for p, _s in self.killed], [637])   # control fired
        self.assertEqual(actions["skipped"], [632])             # reshaped, not killed

    def test_second_apply_is_idempotent_noop(self):
        d = self.plant(801, self.waiter("codex-3"), start_after_boot=100)  # stale
        self.plant(809, self.waiter("codex-3"), start_after_boot=900)      # current
        # sibling keeps 801 signalable; it is CURRENT, so the second pass still
        # finds nothing stale and the idempotent-noop claim is unchanged.
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
    def test_the_printed_row_names_the_declared_seat_and_still_refuses(self):
        """THE FIELD IS NOT THE FIX — THE ROW IS. A value computed correctly
        and discarded one frame before the operator sees it is the shape this
        row was filed against: rearm KNEW nothing about whose waiter it was,
        and after the field alone it would know and still not say.

        SCOPED TO THE ROW, NEVER THE SCREEN. `assertIn(name, text)` over the
        whole report is satisfied by any other line that happens to carry the
        name — here, literally, by the OTHER waiter planted beside it. So the
        assertions select the pid's own line first and read only that."""
        self.plant(931, ["python3", "/x/helm", "chat", "wait", "--follow"],
                   start_after_boot=100,
                   environ={"HELM_CHAT_NAME": "seat-b"})
        self.plant(932, self.waiter("seat-a"), start_after_boot=100,
                   environ={"HELM_CHAT_NAME": "seat-a"})
        # NO environ FILE AT ALL — the fixture's own unreadable case, and
        # distinct from an environ that simply declares no seat. No --seat in
        # argv either, or the first branch would claim the row before the
        # ordering under test is ever reached.
        self.plant(933, ["python3", "/x/helm", "chat", "wait", "--follow"],
                   start_after_boot=100)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rearm.cmd_rearm([])
        lines = out.getvalue().splitlines()
        declared = [ln for ln in lines if "pid 931" in ln]
        argv_row = [ln for ln in lines if "pid 932" in ln]
        unreadable_row = [ln for ln in lines if "pid 933" in ln]
        self.assertEqual(len(declared), 1, "the 931 row is not on the report "
                                           "exactly once, so the assertions "
                                           "below read the wrong thing")
        self.assertEqual(len(argv_row), 1, "the 932 control row is not on the "
                                           "report exactly once")
        self.assertEqual(len(unreadable_row), 1,
                         "the 933 unreadable row is not on the report exactly "
                         "once, so the ordering assertions below read the "
                         "wrong line")
        self.assertIn("seat-b (environ)", declared[0],
                      "the row prints '?' for a waiter that names itself, so "
                      "the operator cannot tell whose beacon is stale")
        self.assertIn("re-arm that seat by hand", declared[0],
                      "the row names the seat and not the remedy")
        # MUST-MISS ON THE NEIGHBOURING ROW: a waiter whose seat came from
        # argv is NOT annotated. Without this the arm would pass against a
        # build that tagged every row '(environ)'.
        self.assertIn("seat-a", argv_row[0])
        self.assertNotIn("(environ)", argv_row[0],
                         "an argv-seated waiter is labelled as though its "
                         "name came from the weaker source")
        # AND THE THIRD ROW, WHICH IS WHAT PINS THE BRANCH ORDER. The render
        # tests ENVIRON_UNREADABLE **before** truthiness, and that ordering is
        # the entire guard: the sentinel is the string "<unreadable>", which is
        # TRUTHY, so under the two arms swapped the truthy arm claims it and
        # the row reads "<unreadable> (environ)" — the sentinel wearing the
        # costume of the state it is not, which is precisely the collapse the
        # tri-state exists to prevent.
        #
        # THREE OF THE FOUR STATES ARE ORDER-INDEPENDENT, measured by swapping
        # the arms and rendering all four: argv-seated, declares-a-seat and
        # declares-nothing are byte-identical either way, and only the
        # unreadable row differs. So an arm that never drives an UNREADABLE
        # waiter is green under BOTH orderings, which is why the two rows
        # above could not catch this and a third one is not redundant.
        self.assertIn("? (environ unreadable)", unreadable_row[0],
                      "the unreadable row does not say the probe failed, so "
                      "'could not look' is being reported as an answer")
        self.assertNotIn("(environ)", unreadable_row[0],
                         "the ENVIRON_UNREADABLE sentinel is being rendered "
                         "as a declared seat name — the truthiness arm claimed "
                         "it, so the branch order that keeps the three states "
                         "apart has been swapped")

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
        # THE RECIPE MUST NAME WHAT THESE ROWS CARRY. This used to assert
        # `helm seat down <seat> && helm seat up <seat>` — and the advisory
        # row printed one line above is `pid 902 helm router`, which has no
        # `seat` key at all (only the WAITERS branch of scan() records one).
        # So the pinned string asked its reader for a substitution the output
        # could not supply, and `helm seat up/down` takes a proxy FAMILY
        # anyway, not a seat. The pin was holding an unfollowable instruction
        # in place; it now holds the followable one.
        self.assertIn("each row above names its PID", text)
        self.assertIn("pid 902", text)
        self.assertNotIn("<seat>", text)
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


# ---------------------------------------------------------------------------
# #65 — never signal a seat's LAST wake path
# ---------------------------------------------------------------------------

class SpareLastWakePathTest(RearmBase):
    """A seat's beacon IS its wake path: nothing external re-invokes a PTY
    agent, so a seat re-arms only ON a turn and only gets a turn if something
    wakes it. Signalling every waiter a seat owns makes it deaf permanently,
    not until its next turn."""

    def test_a_seats_ONLY_waiter_is_spared_even_though_it_is_stale(self):
        self.plant(901, self.waiter("kimi"), start_after_boot=100)   # stale, alone
        # POSITIVE CONTROL, unconditional and FIRST: a second seat whose stale
        # waiter has a current sibling IS killed in this same pass. Without it,
        # "self.killed == []" is equally satisfied by a harness where no kill
        # could ever be recorded, and the arm proves nothing about sparing.
        self.plant(902, self.waiter("codex-3"), start_after_boot=100)  # stale
        self.plant(903, self.waiter("codex-3"), start_after_boot=900)  # current
        actions = rearm.apply(rearm.scan())
        self.assertEqual([p for p, _s in self.killed], [902])   # the control fired
        self.assertEqual(actions["signaled"], [902])            # kimi's 901 did NOT
        self.assertEqual(actions["spared"], [[901, "kimi"]])

    def test_announce_recipe_teaches_idempotent_follow_and_never_replace(self):
        """task/2542. `--follow --replace` as a recipe is how a seat goes
        deaf: a compacted SUBAGENT reads a re-arm instruction as its own, is
        refused as a duplicate, re-arms with --replace, SIGTERMs the main
        conversation's beacon and takes its wake route. A bare --follow is
        idempotent (a duplicate exits cleanly), so it is the one safe re-arm
        to repeat.

        AND --replace IS NOT THE CURE FOR A DEAD INCUMBENT. A plain --follow
        from a seat with replacement authority already stops a dead or ghost
        waiter (beacons.arm keeps the replacement pass for those states, and
        seats_cli._cmd_wait passes may_reap for every follow). What only
        --replace does is rotate a LIVE waiter serving the same session, such
        as the stale one rearm spared, so the row names it for that and only
        from the main conversation. Round 1 of the review measured the row
        saying the opposite."""
        self.plant(905, self.waiter("kimi"), start_after_boot=100)  # stale, alone
        self.plant(906, self.waiter("codex"), start_after_boot=100)  # stale
        self.plant(907, self.waiter("codex"), start_after_boot=900)  # survivor
        actions = rearm.apply(rearm.scan())
        self.assertEqual(actions["spared"], [[905, "kimi"]])
        self.assertEqual(actions["signaled"], [906])  # forces the announce row
        text = self.post.call_args.args[0]
        from helm import seats_advice
        self.assertIn(seats_advice.beacon_monitor("<your-seat>"), text)
        self.assertIn(seats_advice.BEACON_EXPIRY, text)
        self.assertNotIn("--follow --replace", text)
        self.assertIn("main conversation", text)
        self.assertIn("subagent", text)
        self.assertNotIn("proven dead", text)
        self.assertIn("--replace only to rotate a LIVE waiter", text)
        # THE PROMISE CARRIES ITS CONDITIONS (row 5f1fc24c8d2a):
        # a live incumbent with a different room, --any, --ambient or timeout
        # makes a bare --follow exit 2 (beacons.arm conflict), and a caller
        # without replacement authority arms ALONGSIDE a dead incumbent
        # (seats_cli passes reap=False) instead of clearing it.
        self.assertNotIn("exits cleanly beside a live beacon of your session "
                         "and already clears", text)
        self.assertIn("same room, --any, --ambient and timeout", text)
        self.assertIn("exits 2 and names --replace", text)
        self.assertIn("with replacement authority it clears a dead or ghost "
                      "one", text)
        self.assertIn("arms alongside it", text)
        verbs = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs", "VERBS.md")
        with open(verbs, encoding="utf-8") as f:
            doc = f.read()
        self.assertNotIn("only against an incumbent proven dead", doc)
        self.assertIn("already stops a dead or ghost incumbent", doc)

    def test_a_seat_with_SIX_stale_waiters_loses_five_and_keeps_the_NEWEST(self):
        """The verb keeps its teeth. Sparing is one waiter per starved seat, not
        an amnesty — and the survivor is the newest, which is closest to HEAD and
        likeliest to speak the current protocol."""
        for pid, boot in ((911, 100), (912, 120), (913, 140),
                          (914, 160), (915, 180), (916, 200)):
            self.plant(pid, self.waiter("codex"), start_after_boot=boot)
        actions = rearm.apply(rearm.scan())
        self.assertEqual(sorted(actions["signaled"]), [911, 912, 913, 914, 915])
        self.assertEqual(actions["spared"], [[916, "codex"]])        # newest
        self.assertEqual(sorted(p for p, _s in self.killed),
                         [911, 912, 913, 914, 915])

    def test_a_CURRENT_sibling_is_a_survivor_so_nothing_is_spared(self):
        """The predicate is 'does my kill set remove this seat's last wake
        path', not 'is this waiter stale'. A current sibling survives the pass,
        so the stale one is signalled exactly as before this rule existed."""
        self.plant(921, self.waiter("codex-3"), start_after_boot=100)   # stale
        self.plant(922, self.waiter("codex-3"), start_after_boot=900)   # current
        actions = rearm.apply(rearm.scan())
        self.assertEqual(actions["signaled"], [921])
        self.assertEqual(actions["spared"], [])

    def test_an_UNKNOWN_waiter_counts_as_a_survivor(self):
        """A waiter whose start is unreadable is never signalable, so it is
        still a live wake path. A seat this pass cannot classify is not a seat
        this pass may leave deaf — and it must not be spared REDUNDANTLY
        either, or a stale sibling escapes for no reason."""
        self.plant(931, self.waiter("grok"), start_after_boot=100)   # stale
        self.plant(932, self.waiter("grok"))                         # no stat -> UNKNOWN
        actions = rearm.apply(rearm.scan())
        self.assertEqual(actions["signaled"], [931])
        self.assertEqual(actions["spared"], [])

    def test_seats_are_independent_one_starved_one_not(self):
        """Sparing is per-seat. A starved seat must not buy amnesty for a seat
        that can afford the kill, and vice versa."""
        self.plant(941, self.waiter("kimi"), start_after_boot=100)     # alone+stale
        self.plant(942, self.waiter("codex-3"), start_after_boot=100)  # stale
        self.plant(943, self.waiter("codex-3"), start_after_boot=900)  # current
        actions = rearm.apply(rearm.scan())
        self.assertEqual(actions["signaled"], [942])
        self.assertEqual(actions["spared"], [[941, "kimi"]])

    def test_the_DRY_RUN_names_the_spared_waiter_and_says_re_arm_by_hand(self):
        """The dry run is what someone reads BEFORE running the real thing, so
        it must show what --apply would spare. It re-derives the set from the
        plan rather than reading it out of an actions dict it does not have."""
        self.plant(951, self.waiter("kimi"), start_after_boot=100)
        self.plant(952, self.waiter("codex-3"), start_after_boot=100)  # stale
        self.plant(953, self.waiter("codex-3"), start_after_boot=900)  # current
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rearm.cmd_rearm([])
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("SPARED", text)
        self.assertIn("LAST wake path", text)
        self.assertIn("re-arm this seat by hand", text)
        self.assertEqual(self.killed, [])  # noqa: VACUOUS_ASSERTION — the
        # positive control is four lines below: the SAME plan is then run
        # through apply() and DOES kill 952, so this absence is measured
        # against a harness proven capable of recording a kill. It cannot
        # precede the dry run without polluting the thing under test.
        # POSITIVE CONTROL on that same observable: this exact plan DOES kill
        # under --apply, so "killed == []" above measures the DRY RUN and not a
        # fixture that was never capable of a kill.
        rearm.apply(rearm.scan())
        self.assertEqual([p for p, _s in self.killed], [952])

    def test_the_APPLY_summary_NAMES_the_starved_seat_not_just_a_count(self):
        """A spared waiter means a seat is stuck on pre-HEAD beacon code. A bare
        number would make that invisible in the log someone greps afterwards."""
        seen = {}
        self._patch(rearm.pk, "event",
                    lambda *a: seen.update(text=a[-1]))
        self.plant(961, self.waiter("sitka-inc-claude"), start_after_boot=100)
        self.plant(962, self.waiter("codex-3"), start_after_boot=100)
        self.plant(963, self.waiter("codex-3"), start_after_boot=900)
        rearm.apply(rearm.scan())
        self.assertIn("SPARED", seen.get("text", ""))
        self.assertIn("sitka-inc-claude", seen["text"])
        self.assertIn("re-arm by hand", seen["text"])


# ---------------------------------------------------------------------------
# THE REPORT-ONLY INVARIANT, AND THE GUARD THAT HOLDS IT.
#
# helm/rearm.py states it in one sentence beside the write: "`seat_declared`
# is the weaker, report-only answer; nothing that acts may read it." The
# separation is real and load-bearing — `seat` comes from ARGV, which is what
# the operator wrote, and `seat_declared` comes from the process ENVIRON,
# which is only what it inherited, so acting on the latter would SIGTERM a
# waiter on evidence the operator never supplied.
#
# THE KEY SITS ON A PUBLIC RETURN SURFACE. `scan()` RETURNS the waiter dicts,
# and six sites across three other modules consume them (beacons.py at four,
# seat_resume_all.py, seats_stop_signals.py), so any of them can reach the key
# and a sentence in a comment is not something a consumer has to read.
#
# THIS PINS LOCATION, NOT COUNT, because location is what the invariant
# actually says. A fourth READ inside the reporting function is still
# report-only and must not trip this; a read anywhere else is the violation,
# whatever the count. A count pin would have cried wolf at legitimate render
# work while staying silent on the one move it exists to catch — a consumer
# in another module reaching into the dict.
_SEAT_DECLARED_SITES = {
    ("rearm.py", "scan"): (
        "THE WRITE, and the only one. `_environ_seat(pid)` is stored on the "
        "waiter dict here, beside the argv-sourced `seat` and the "
        "`signalable` flag that decides who is signalled. This site is the "
        "reason the key exists and the reason the invariant is needed."),
    ("rearm.py", "_print_report"): (
        "THE READS. This is the reporting function — the whole permitted "
        "surface for the weaker answer. It renders the three states apart "
        "(a name from argv, a name from environ, and a probe that could not "
        "look) and annotates the skipped-waiter note with the declared seat "
        "so an operator can re-arm that seat BY HAND. Nothing here signals."),
}


def _seat_declared_sites(root=None):
    """(module path, enclosing function) for every site under helm/ naming the
    key -- as a string literal or as an attribute -- innermost function wins.

    IT WALKS, IT DOES NOT LIST. helm/ holds eight subpackages -- work, inject,
    cred, store, premise, clarity, configs, web_ui -- and `scan()` RETURNS the
    waiter dicts, so a consumer under any of them can reach `seat_declared`. A
    listdir-and-filter over helm/ sees only the top level and reports a
    confident nothing about that entire tier, silently, because the rot check
    below speaks only when an ALLOWLISTED site disappears. task/2258.

    THE KEY IS THE PATH RELATIVE TO helm/, not the basename, so two files of
    the same name in different packages cannot collide into one entry.

    AND IT IS QUALIFIED BY CLASS WHERE THERE IS ONE. Attributing a hit to its
    innermost enclosing FUNCTION name alone lets a method named `scan` or
    `_print_report` anywhere in the tree inherit an allowlist entry written for
    the module-level function of that name -- an exemption granted by
    coincidence of naming.
    """
    import ast as _ast
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg = root or os.path.join(here, "helm")
    out = set()

    def visit(node, where, rel):
        for child in _ast.iter_child_nodes(node):
            inner = where
            if isinstance(child, _ast.ClassDef):
                inner = child.name
            elif isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                # `node` is this child's PARENT, so this asks exactly "is it a
                # direct method", and `where` is that class's name.
                inner = ("%s.%s" % (where, child.name)
                         if isinstance(node, _ast.ClassDef) else child.name)
            hit = (isinstance(child, _ast.Constant)
                   and child.value == "seat_declared")
            hit = hit or (isinstance(child, _ast.Attribute)
                          and child.attr == "seat_declared")
            if hit:
                out.add((rel, where))
            visit(child, inner, rel)

    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, pkg)
            with open(full, encoding="utf-8") as fh:
                src = fh.read()
            if "seat_declared" not in src:
                continue
            visit(_ast.parse(src), None, rel)
    return out


class TheReportOnlyInvariantIsEnforcedTest(unittest.TestCase):

    def test_the_walk_can_see_a_subpackage_and_does_not_merge_by_basename(self):
        """THE REACH CONTROL, and the allowlist arm cannot stand in for it.

        Its `assertTrue(sites)` proves the walk found SOMETHING, and the
        something is in rearm.py -- a TOP-LEVEL file -- so it proves reach for
        exactly the tier that was never in doubt. helm/ has eight subpackages
        and a consumer added under any of them reaches scan()'s waiter dicts,
        about which a listdir-and-filter walk reports a confident nothing. So
        the positive has to be a synthetic violator of the construct class
        PLANTED IN A SUBPACKAGE (task/2258).

        THE NAME-COLLISION ROW is the second half: a method named `scan` must
        not inherit the allowlist entry written for rearm's MODULE-LEVEL
        `scan`, or an exemption is granted by coincidence of naming.
        """
        root = tempfile.mkdtemp(prefix="seatdecl-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, "work"))
        os.makedirs(os.path.join(root, "__pycache__"))
        reader = 'def act(w):\n    return w["seat_declared"]\n'
        with open(os.path.join(root, "toplevel_reader.py"), "w") as fh:
            fh.write(reader)
        with open(os.path.join(root, "work", "sub_reader.py"), "w") as fh:
            fh.write(reader)
        with open(os.path.join(root, "collide.py"), "w") as fh:
            fh.write('class Other:\n    def scan(self, w):\n'
                     '        return w["seat_declared"]\n')
        with open(os.path.join(root, "__pycache__", "junk.py"), "w") as fh:
            fh.write('x = "seat_declared"\n')

        found = _seat_declared_sites(root=root)
        self.assertIn(("toplevel_reader.py", "act"), found,
                      "the walk cannot see a TOP-LEVEL violator, so nothing "
                      "below is meaningful: %r" % (sorted(found),))
        self.assertIn(("work/sub_reader.py", "act"), found,
                      "the walk is blind to subpackages -- a consumer under "
                      "any of helm's eight of them reaches the waiter dicts "
                      "while this guard reports a confident nothing: %r"
                      % (sorted(found),))
        self.assertIn(("collide.py", "Other.scan"), found,
                      "a METHOD named scan is not keyed by its class: %r"
                      % (sorted(found),))
        self.assertNotIn(("collide.py", "scan"), found,
                         "the method was keyed as a bare `scan`, which is "
                         "exactly the allowlisted name -- an exemption "
                         "granted by coincidence of naming")
        self.assertFalse(
            [p for p, _ in found if p.startswith("__pycache__")],
            "the walk descended into __pycache__ and reports compiled "
            "leftovers as source sites: %r" % (sorted(found),))

    def test_seat_declared_is_named_only_where_it_is_allowed_to_be(self):
        """A READER THAT ACTS CANNOT BE ADDED WITHOUT A HUMAN SAYING SO.

        The offender set is computed both ways on purpose. An UNEXPECTED site
        is the failure the invariant names — a module that decides who gets a
        SIGTERM reaching for the weaker answer. A MISSING site means the
        allowlist has rotted: the key was renamed or the write moved, and an
        allowlist describing code that no longer exists would sit here
        reassuring readers while guarding nothing."""
        sites = _seat_declared_sites()
        self.assertTrue(sites,
                        "found NO seat_declared sites anywhere in helm/*.py — "
                        "this walk is blind, so its silence below would mean "
                        "nothing at all")
        self.assertFalse(
            sites - set(_SEAT_DECLARED_SITES),
            "seat_declared is named at un-allowlisted site(s) %r. It is the "
            "REPORT-ONLY answer, sourced from a process environ rather than "
            "from what the operator wrote: nothing that ACTS may read it, and "
            "scan() returns these dicts to beacons.py, seat_resume_all.py and "
            "seats_stop_signals.py. If the new site only renders, add it to "
            "_SEAT_DECLARED_SITES with a reason; if it decides anything, it "
            "must read the argv-sourced `seat` instead."
            % (sorted(sites - set(_SEAT_DECLARED_SITES)),))
        self.assertFalse(
            set(_SEAT_DECLARED_SITES) - sites,
            "allowlisted site(s) %r no longer name seat_declared — the "
            "allowlist has rotted and its reasons now describe code that is "
            "not there. Prune it, or fix the walk if the key was renamed."
            % (sorted(set(_SEAT_DECLARED_SITES) - sites),))
