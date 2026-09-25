#!/usr/bin/env python3
"""helm chat node — consensus-posture mirroring and honest start failures.

WHY THIS EXISTS: the chat node had never once started on the dev machine. Not a
regression, not a flake — the generated unit could not possibly work, and the two
bugs that hid it were both in helm.

dregg refuses to boot when the verified Lean executor archive is absent: it will
not "serve as if verified while running the un-verified executor". The escape
hatch belongs to the operator, and on this machine the operator had already
opened it — for the TEAM cave node, in a systemd drop-in, which is the standard
place to state machine-local posture without editing a shipped unit. helm's unit
was the same binary on the same machine and carried no Environment= lines, so it
restart-looped forever.

And helm reported that as "unit started but the API never answered". A timeout is
true of every possible cause, so it pointed nowhere; meanwhile the service had
already printed the exact remedy. Relaying a subordinate's precise diagnosis is
not optional for a provisioner that owns it.

The invariant these tests pin, in both directions: helm MIRRORS an existing
operator declaration and NEVER mints one. A machine that has not opted out of
verification must keep dregg's refusal.
"""
import contextlib
import io
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import chatnode  # noqa: E402


class PostureBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-posture-")
        self.prior_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp
        self.units = os.path.join(self.tmp, ".config", "systemd", "user")
        os.makedirs(self.units)

    def tearDown(self):
        if self.prior_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.prior_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def node_bin(self, name="dregg-node"):
        """A node binary `up` can hash and record; the unit never runs it.
        `up` writes that record into the chat-node state, so only an arm
        whose HELM_HOME is its own (BootBase) may drive `up`."""
        b = os.path.join(self.tmp, name)
        with open(b, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(b, 0o755)
        return mock.patch.object(chatnode, "bin_resolution", return_value={
            "path": b, "source": "env", "reason": None})

    def declare(self, body, unit=None, name="override.conf"):
        d = os.path.join(self.units, (unit or chatnode.PEER_UNIT) + ".d")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p


class DeclaredPostureTest(PostureBase):
    def test_reads_the_operators_dregg_flags_from_a_dropin(self):
        src = self.declare("[Service]\n"
                           "Environment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n"
                           "Environment=DREGG_ALLOW_UNAUDITED_PQ=1\n")
        got = chatnode.declared_posture()
        self.assertEqual([a for _s, a in got],
                         ["DREGG_ALLOW_UNVERIFIED_CONSENSUS=1",
                          "DREGG_ALLOW_UNAUDITED_PQ=1"])
        self.assertTrue(all(s == src for s, _a in got), "source must be named")

    def test_only_dregg_names_are_mirrored(self):
        """A drop-in may hold anything at all. Copying arbitrary Environment=
        lines into a second unit file would propagate unrelated — possibly
        secret-bearing — values onto disk in a new place. Only the flags that
        gate startup travel."""
        self.declare("[Service]\n"
                     "Environment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n"
                     "Environment=AWS_SECRET_ACCESS_KEY=hunter2\n"
                     "Environment=RUST_LOG=debug\n")
        got = [a for _s, a in chatnode.declared_posture()]
        self.assertEqual(got, ["DREGG_ALLOW_UNVERIFIED_CONSENSUS=1"])

    def test_a_multi_assignment_line_cannot_smuggle_a_secret(self):
        """kimi's HOLE 2, reproduced live before the fix. systemd's Environment=
        takes N whitespace-separated assignments on ONE line, so

            Environment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1 AWS_SECRET_ACCESS_KEY=x

        is two. The old filter asked whether the WHOLE string started with
        DREGG_, said yes, and mirrored the line whole — writing the secret into
        a second 0600 file on disk, which is exactly the leak the filter was
        cited as preventing. Validating the first token of an N-token grammar is
        not validation."""
        self.declare("[Service]\n"
                     "Environment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1 "
                     "AWS_SECRET_ACCESS_KEY=hunter2\n")
        got = [a for _s, a in chatnode.declared_posture()]
        self.assertEqual(got, ["DREGG_ALLOW_UNVERIFIED_CONSENSUS=1"])
        p = chatnode.write_posture_dropin(chatnode.declared_posture())
        self.assertNotIn("hunter2", open(p, encoding="utf-8").read(),
                         "a secret rode a multi-assignment line into the mirror")

    def test_quoted_assignments_are_split_the_way_systemd_splits_them(self):
        self.declare('[Service]\n'
                     'Environment="DREGG_A=one two" SECRET_B=nope\n')
        self.assertEqual([a for _s, a in chatnode.declared_posture()],
                         ["DREGG_A=one two"])

    def test_an_unbalanced_quote_mirrors_nothing_rather_than_guessing(self):
        """A line we cannot parse is not a line we may half-parse. Guessing at
        malformed input is how the multi-assignment hole would come back."""
        self.declare('[Service]\nEnvironment=DREGG_A=1 "unclosed\n')
        self.assertEqual(chatnode.declared_posture(), [])

    def test_a_name_merely_containing_DREGG_is_not_a_posture_flag(self):
        self.declare("[Service]\nEnvironment=NOT_DREGG_ALLOW=1\n")
        self.assertEqual(chatnode.declared_posture(), [])

    def test_no_dropin_directory_is_empty_not_an_error(self):
        self.assertEqual(chatnode.declared_posture(), [])

    def test_multiple_dropins_are_read_in_systemd_order(self):
        self.declare("[Service]\nEnvironment=DREGG_A=1\n", name="05-a.conf")
        self.declare("[Service]\nEnvironment=DREGG_B=2\n", name="90-b.conf")
        self.assertEqual([a for _s, a in chatnode.declared_posture()],
                         ["DREGG_A=1", "DREGG_B=2"])

    def test_non_conf_files_are_ignored(self):
        self.declare("[Service]\nEnvironment=DREGG_REAL=1\n", name="live.conf")
        self.declare("[Service]\nEnvironment=DREGG_STALE=1\n",
                     name="override.conf.bak")
        self.assertEqual([a for _s, a in chatnode.declared_posture()],
                         ["DREGG_REAL=1"])


class MirrorTest(PostureBase):
    def test_nothing_declared_writes_nothing(self):
        """THE LOAD-BEARING ONE. helm must never be the layer that quietly opts a
        node out of verification. No operator declaration => no drop-in => dregg's
        refusal stands, which is the correct outcome."""
        self.assertIsNone(chatnode.write_posture_dropin([]))
        d = chatnode._dropin_dir(chatnode.UNIT)
        self.assertFalse(os.path.exists(d), "minted a posture nobody declared")

    def test_mirrors_the_flags_and_names_its_source(self):
        src = self.declare("[Service]\nEnvironment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n")
        p = chatnode.write_posture_dropin(chatnode.declared_posture())
        body = open(p, encoding="utf-8").read()
        self.assertIn("Environment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1", body)
        self.assertIn("[Service]", body)
        self.assertIn(src, body, "a mirrored opt-out must cite where it came from")
        self.assertIn("MIRRORED, not decided here", body)
        self.assertEqual(oct(os.stat(p).st_mode)[-3:], "600")

    def test_rewriting_replaces_rather_than_appends(self):
        self.declare("[Service]\nEnvironment=DREGG_ONE=1\n")
        chatnode.write_posture_dropin(chatnode.declared_posture())
        os.remove(os.path.join(self.units, chatnode.PEER_UNIT + ".d",
                               "override.conf"))
        self.declare("[Service]\nEnvironment=DREGG_TWO=2\n")
        p = chatnode.write_posture_dropin(chatnode.declared_posture())
        body = open(p, encoding="utf-8").read()
        self.assertIn("DREGG_TWO=2", body)
        self.assertNotIn("DREGG_ONE=1", body,
                         "a withdrawn declaration must not survive in the mirror")

    def test_withdrawal_deletes_the_mirror_it_must_not_outlive_its_source(self):
        """kimi's HOLE 1, reproduced live before the fix. rewrite-replaces
        handled an EDIT; full WITHDRAWAL is a different terminal state. The
        operator deletes their declaration, declared_posture() returns [], and
        the previously-mirrored file stayed on disk still carrying
        Environment=DREGG_ALLOW_*=1 — so our copy kept granting an opt-out the
        operator had rescinded, and the node went on running unverified on an
        authority that no longer existed.

        Empty posture has two meanings: never-declared (write nothing) and
        withdrawn (clear). Conflating them is the whole bug."""
        src = self.declare("[Service]\nEnvironment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n")
        mirror = chatnode.write_posture_dropin(chatnode.declared_posture())
        self.assertTrue(os.path.exists(mirror))
        os.remove(src)                                   # operator rescinds
        self.assertEqual(chatnode.declared_posture(), [])
        returned = chatnode.write_posture_dropin(chatnode.declared_posture())
        self.assertFalse(os.path.exists(mirror),
                         "a withdrawn opt-out outlived its source")
        self.assertEqual(returned, mirror, "the removal must be reported")

    def test_never_declared_still_writes_nothing_after_the_withdrawal_fix(self):
        """The other half of the same discrimination: clearing on withdrawal must
        not turn into writing on never-declared."""
        self.assertIsNone(chatnode.write_posture_dropin([]))
        self.assertFalse(os.path.exists(chatnode._dropin_dir(chatnode.UNIT)))

    def test_the_generated_comment_does_not_promise_what_the_code_skips(self):
        """The first version's file said 'remove the source declaration and this
        file becomes empty on the next node up'. It did not. A false guarantee
        inside a generated file is worse than no comment — it is what a future
        reader checks INSTEAD of the code."""
        self.declare("[Service]\nEnvironment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n")
        body = open(chatnode.write_posture_dropin(chatnode.declared_posture()),
                    encoding="utf-8").read()
        self.assertIn("DELETES", body)
        self.assertNotIn("becomes empty", body)

    def test_the_mirror_targets_our_unit_not_the_peers(self):
        self.declare("[Service]\nEnvironment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n")
        p = chatnode.write_posture_dropin(chatnode.declared_posture())
        self.assertIn(chatnode.UNIT + ".d", p)
        self.assertNotIn(chatnode.PEER_UNIT + ".d", p,
                         "never write back into the operator's own declaration")


class FailureRelayTest(PostureBase):
    REFUSAL = ("2026-07-25T15:17:51Z ERROR dregg_node: REFUSING TO START: "
               "`dregg_lean_ffi::lean_available()` is false ... To deliberately "
               "run an un-verified node, set DREGG_ALLOW_UNVERIFIED_CONSENSUS=1.")

    def _journal(self, stdout):
        return mock.patch("helm.chatnode.subprocess.run",
                          return_value=mock.Mock(stdout=stdout, returncode=0))

    def test_the_services_own_words_are_returned(self):
        with self._journal("Started helm-chat-node.service\n" + self.REFUSAL + "\n"):
            self.assertEqual(chatnode.last_failure(), self.REFUSAL)

    def test_the_newest_error_wins(self):
        with self._journal("x ERROR old thing\ny ERROR newest thing\n"):
            self.assertIn("newest", chatnode.last_failure())

    def test_a_clean_journal_is_None_never_a_fabricated_reason(self):
        with self._journal("Started helm-chat-node.service\nlistening on 8898\n"):
            self.assertIsNone(chatnode.last_failure())

    def test_journalctl_missing_degrades_to_None(self):
        with mock.patch("helm.chatnode.subprocess.run", side_effect=OSError("nope")):
            self.assertIsNone(chatnode.last_failure())



# A real line from a rebased node's journal as `journalctl -o cat` returns it:
# the node colours its log whether or not stdout is a terminal, and the journal
# keeps the escapes (measured on this host's helm-chat-node journal).
ESC = "\x1b"
EPOCH_REFUSAL = (
    ESC + "[2m2026-09-24T03:55:38.604428Z" + ESC + "[0m " + ESC + "[31mERROR"
    + ESC + "[0m " + ESC + "[2mdregg_node" + ESC + "[0m" + ESC + "[2m:" + ESC
    + "[0m failed to initialize node state: failed to open store: integrity "
    "error: populated store has no canonical state schema epoch; refusing to "
    "reinterpret pre-v11 fields roots / pre-v3 ledger roots (re-genesis "
    "required)")


def _show(active, sub, restarts=0, result="", invocation="",
          started_ago=None):
    """`systemctl show` output. `started_ago` seconds sets the main process
    start (ExecMainStartTimestampMonotonic, CLOCK_MONOTONIC microseconds)."""
    started = (int((time.monotonic() - started_ago) * 1e6)
               if started_ago is not None else 0)
    return (0, "ActiveState=%s\nSubState=%s\nNRestarts=%d\nResult=%s\n"
               "InvocationID=%s\nExecMainStartTimestampMonotonic=%d"
            % (active, sub, restarts, result, invocation, started))


RUN_ID = "3a5e9072fdb646a5b04f327152efb149"
NONFATAL_SWAP_ERROR = (
    "2026-09-24T03:30:44.553574Z ERROR dregg::lean_shadow::producer: THE SWAP "
    "authority inversion: verified Lean executor (AUTHORITATIVE) and the "
    "demoted Rust reference DISAGREE on a covered turn")


class BootBase(PostureBase):
    """HOME and HELM_HOME in tmp, the boot-wait knob owned, and a systemctl
    stand-in whose `show` answer each arm sets."""

    KEYS = ("HELM_HOME", "HELM_CHAT_NODE_BOOT_WAIT_S",
            "MELD_CHAT_NODE_BOOT_WAIT_S", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL")

    def setUp(self):
        super().setUp()
        self.prior = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.shows = [_show("active", "running")]
        self.calls = []

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def systemctl(self, *args):
        self.calls.append(args)
        if args and args[0] == "show":
            return self.shows.pop(0) if len(self.shows) > 1 else self.shows[0]
        if args and args[0] == "is-active":
            return 0, "active"
        return 0, ""

    def journal(self, stdout):
        return mock.patch("helm.chatnode.subprocess.run",
                          return_value=mock.Mock(stdout=stdout, returncode=0))


class BootWaitTest(BootBase):
    def test_the_default_outlasts_a_rebased_nodes_measured_boot(self):
        """150.2-154.2 s to the API on a fast host; a slower one takes longer."""
        self.assertEqual(chatnode.boot_wait_s(), 600)
        self.assertGreater(chatnode.boot_wait_s(), 155)

    def test_the_knob_is_read_and_a_typo_falls_back_to_the_default(self):
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "45"
        self.assertEqual(chatnode.boot_wait_s(), 45.0)
        for bad in ("abc", "-3", "0"):
            os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = bad
            self.assertEqual(chatnode.boot_wait_s(), 600, bad)

    def test_an_exited_unit_ends_the_wait_at_once(self):
        """A failing fast-booting node must not cost the whole ten minutes:
        the wait ends as soon as systemd says the process is gone."""
        self.shows = [_show("activating", "auto-restart", 1)]
        t0 = time.time()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None):
            outcome, what = chatnode.wait_boot("http://127.0.0.1:8898",
                                               unit=chatnode.UNIT, seconds=4)
        self.assertEqual(outcome, "exited")
        self.assertEqual(what["sub"], "auto-restart")
        self.assertLess(time.time() - t0, 3)

    def test_a_restart_during_the_wait_is_an_exit_even_if_it_is_running_again(self):
        self.shows = [_show("active", "running", 0), _show("active", "running", 1)]
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None):
            outcome, _what = chatnode.wait_boot("http://127.0.0.1:8898",
                                                unit=chatnode.UNIT, seconds=6)
        self.assertEqual(outcome, "exited")

    def test_a_start_still_queued_is_not_an_exit(self):
        """`up` starts the unit with --no-block, so the first reads can see
        it inactive/dead before its start job runs; then prepare
        (start-pre) and the node itself. None of that is an exit."""
        self.shows = [_show("inactive", "dead"), _show("inactive", "dead"),
                      _show("activating", "start-pre"),
                      _show("active", "running")]
        answers = iter([None] * 6)
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json",
                                  side_effect=lambda *a, **k: next(answers, [])):
            outcome, _what = chatnode.wait_boot("http://127.0.0.1:8898",
                                                unit=chatnode.UNIT, seconds=30)
        self.assertEqual(outcome, "up")

    def test_a_unit_left_failed_by_an_earlier_crash_loop_is_not_this_starts_exit(self):
        """With --no-block the first reads still show the LAST run's
        `failed`; this start has begun only when the unit is active or its
        InvocationID changes. Measured by review: this read as `exited` in
        0.0 s and relayed the old journal as the new failure."""
        old_run, new_run = "a" * 32, "b" * 32
        self.shows = [_show("failed", "failed", 5, "exit-code", old_run),
                      _show("failed", "failed", 5, "exit-code", old_run),
                      _show("activating", "start-pre", 0, "", new_run),
                      _show("active", "running", 0, "", new_run)]
        answers = iter([None] * 6)
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json",
                                  side_effect=lambda *a, **k: next(answers, [])):
            outcome, _what = chatnode.wait_boot("http://127.0.0.1:8898",
                                                unit=chatnode.UNIT, seconds=30)
        self.assertEqual(outcome, "up")

    def test_a_new_run_that_fails_inside_the_grace_is_an_exit_at_once(self):
        old_run, new_run = "a" * 32, "b" * 32
        self.shows = [_show("failed", "failed", 5, "exit-code", old_run),
                      _show("failed", "failed", 0, "exit-code", new_run)]
        t0 = time.time()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None):
            outcome, what = chatnode.wait_boot("http://127.0.0.1:8898",
                                               unit=chatnode.UNIT, seconds=30)
        self.assertEqual((outcome, what["invocation"]), ("exited", new_run))
        self.assertLess(time.time() - t0, chatnode.START_GRACE_S)

    def test_up_never_snapshots_a_stranger_over_the_saved_key(self):
        """Measured by review: after a partial install, the node generated a
        random key; the next `up` snapshotted it over the only copy of the
        real one. identity_state already said matched=False and `up` never
        asked. The doubles bind both identity reads to a temp cave, never the
        live default data dir."""
        cave = os.path.join(self.tmp, "cave")
        os.makedirs(cave)
        with open(os.path.join(cave, "node.key"), "wb") as f:
            f.write(b"K" * 32)
        real_snap, real_state = chatnode.snapshot_identity, chatnode.identity_state
        real_snap(cave)
        with open(os.path.join(cave, "node.key"), "wb") as f:
            f.write(b"S" * 32)                     # the stranger it generated
        provisioned = []
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                self.node_bin("n"), \
                mock.patch.object(chatnode.cell, "get_json", return_value=[]), \
                mock.patch.object(chatnode, "snapshot_identity",
                                  lambda *_a, **_k: real_snap(cave)), \
                mock.patch.object(chatnode, "identity_state",
                                  lambda *_a, **_k: real_state(cave)), \
                mock.patch.object(chatnode, "provision",
                                  lambda *a, **k: provisioned.append(a) or
                                  (None, "not reached")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chatnode.cmd_node(["up"])
        self.assertEqual(rc, 1)
        self.assertIn("DISAGREES", err.getvalue())
        self.assertEqual(provisioned, [])
        with open(os.path.join(chatnode.identity_dir(), "node.key"), "rb") as f:
            self.assertEqual(f.read(), b"K" * 32)

    def test_up_clears_a_failed_unit_before_starting_it(self):
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "1"
        self._up()
        verbs = [c[0] for c in self.calls if c and c[0] != "show"]
        self.assertIn("reset-failed", verbs)
        self.assertLess(verbs.index("reset-failed"), verbs.index("enable"))

    def test_a_unit_that_never_starts_is_an_exit_after_the_grace(self):
        self.shows = [_show("inactive", "dead")]
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode, "START_GRACE_S", 1), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None):
            outcome, what = chatnode.wait_boot("http://127.0.0.1:8898",
                                               unit=chatnode.UNIT, seconds=20)
        self.assertEqual((outcome, what["active"]), ("exited", "inactive"))

    def test_a_live_process_past_the_wait_is_initializing_not_failed(self):
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None):
            outcome, secs = chatnode.wait_boot("http://127.0.0.1:8898",
                                               unit=chatnode.UNIT, seconds=1)
        self.assertEqual(outcome, "initializing")
        self.assertGreaterEqual(secs, 1)

    def test_an_answering_api_is_up_and_wait_api_stays_a_bool(self):
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=[]):
            self.assertEqual(chatnode.wait_boot(
                "http://127.0.0.1:8898", unit=chatnode.UNIT, seconds=1)[0], "up")
            self.assertIs(chatnode.wait_api("http://127.0.0.1:8898",
                                            seconds=1), True)
        with mock.patch.object(chatnode.cell, "get_json", return_value=None):
            self.assertIs(chatnode.wait_api("http://127.0.0.1:8898",
                                            seconds=0.2), False)

    def _up(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                self.node_bin(), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chatnode.cmd_node(["up"])
        return rc, out.getvalue(), err.getvalue()

    def test_up_reports_a_slow_boot_as_initializing_verified_runtime(self):
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "1"
        rc, out, err = self._up()
        self.assertEqual(rc, 1)
        self.assertIn("initializing verified runtime", out)
        self.assertIn("still initializing verified runtime", err)
        self.assertIn("has not failed", err)
        self.assertNotIn("did not come up", err)
        self.assertNotIn("exited", err)

    def test_up_against_a_hung_node_says_hung_at_once_in_status_words(self):
        """`up` re-run on a node that has been running two hours without
        its API: `enable --now` does not restart it, so wait_boot must see
        the old run's clock and say `hung` at the first look — the same line
        `helm chat node status` prints — never wait out the boot wait and
        call it "still initializing" while status says restart it."""
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "8"
        self.shows = [_show("active", "running", 0, "success", RUN_ID,
                            started_ago=7200)]
        t0 = time.time()
        rc, _out, err = self._up()
        self.assertEqual(rc, 1)
        self.assertLess(time.time() - t0, 5, "waited out the boot wait")
        self.assertNotIn("still initializing", err)
        status = io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None), \
                mock.patch("helm.chat.transport_status",
                           return_value={"mode": "unsigned"}), \
                contextlib.redirect_stdout(status):
            chatnode.cmd_node(["status"])
        hung = [ln for ln in status.getvalue().splitlines()
                if "NOT INITIALIZING" in ln]
        self.assertEqual(len(hung), 1, status.getvalue())
        # the same line word for word; only the running seconds may tick
        import re
        norm = lambda t: re.sub(r"running \d+s", "running Ns", t.strip())
        self.assertIn(norm(hung[0]), [norm(ln) for ln in err.splitlines()])
        self.assertIn("it is hung, not booting", err)
        self.assertIn("past the 600s boot wait", err)

    def test_up_relays_a_refusal_at_once_with_its_cure(self):
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "30"
        # `up` clears the unit (reset-failed) and queues the start; the new
        # run — a new InvocationID — refuses and fails
        self.shows = [_show("inactive", "dead"),
                      _show("failed", "failed", 1, "exit-code", "b" * 32)]
        t0 = time.time()
        with self.journal("Started helm-chat-node.service\n" + EPOCH_REFUSAL + "\n"):
            rc, _out, err = self._up()
        self.assertEqual(rc, 1)
        self.assertLess(time.time() - t0, 10, "waited out the clock on a dead node")
        self.assertIn("the service said:", err)
        self.assertIn("no canonical state schema epoch", err)
        self.assertIn("cure: re-genesis: archive the data dir and keep the "
                      "identity", err)
        self.assertNotIn(ESC, err)


class RefusalRemedyTest(BootBase):
    LINES = {
        "store-epoch": [
            "canonical state schema epoch 25 is incompatible with required "
            "epoch 26; re-genesis is required",
            "populated store has no canonical state schema epoch; refusing to "
            "reinterpret pre-v11 fields roots / pre-v3 ledger roots (re-genesis "
            "required)"],
        "clock-policy": [
            "ERROR dregg_node: blocklace requires consensus_genesis_unix_seconds "
            "+ consensus_time_mode in the shared genesis.json; refusing an "
            "implicit clock policy"],
        "pq-identity": [
            "[client-sign] error: node refused the turn: required post-quantum "
            "signer identity is neither Cell-committed nor independently "
            "enrolled for migration"],
        "swap-veto": [
            "[client-sign] error: node refused the turn: rejected: verified Lean "
            "executor vetoed the commit (THE SWAP strict mode): the legacy Rust "
            "executor accepted this turn but the verified kernel rejected it"],
    }

    def test_each_known_refusal_is_classified_by_the_nodes_own_words(self):
        for kind, lines in self.LINES.items():
            for line in lines:
                hit = chatnode.classify_refusal(line)
                self.assertIsNotNone(hit, line)
                self.assertEqual(hit["kind"], kind, line)
        self.assertIsNone(chatnode.classify_refusal(
            "ERROR dregg_node: something nobody has a cure for"))

    def test_the_regenesis_cure_keeps_the_identity_it_has(self):
        cave = os.path.join(self.tmp, "cave")
        os.makedirs(cave)
        with open(os.path.join(cave, "node.key"), "wb") as f:
            f.write(b"k" * 32)
        chatnode.snapshot_identity(cave)
        cure = chatnode.refusal_remedy(self.LINES["store-epoch"][1], data_dir=cave)
        self.assertIn("archive the data dir and keep the identity", cure)
        self.assertIn("mv %s %s.pre-regenesis-" % (cave, cave), cure)
        self.assertIn("prepare restores node.key from %s"
                      % chatnode.identity_dir(), cure)
        self.assertNotIn("NO identity snapshot", cure)

    def test_the_regenesis_cure_warns_when_no_identity_is_snapshotted(self):
        cure = chatnode.refusal_remedy(self.LINES["store-epoch"][0],
                                       data_dir="/dev/shm/x")
        self.assertIn("NO identity snapshot exists", cure)
        self.assertIn("node.key", cure)

    def test_the_cave_nodes_disk_copy_is_named_only_for_the_cave_node(self):
        """dregg-cave-restore copies ~/.local/share/dregg-cave/data back into
        /dev/shm/dregg-cave on every boot, so archiving only the cave node's
        tmpfs store brings the old-epoch store straight back. The chat node's
        cure must not tell the operator to archive the cave node's store —
        that directory is the rollback path's — and says it is untouched."""
        line = self.LINES["store-epoch"][0]
        disk = os.path.join(self.tmp, ".local", "share", "dregg-cave", "data")
        os.makedirs(disk)
        chat_cure = chatnode.refusal_remedy(line)
        self.assertNotIn("dregg-cave-restore", chat_cure)
        self.assertIn("untouched by this", chat_cure)
        cave_cure = chatnode.refusal_remedy(line, data_dir="/dev/shm/dregg-cave")
        self.assertIn("archive %s too" % disk, cave_cure)
        self.assertIn("dregg-cave-restore", cave_cure)

    def test_the_genesis_cure_is_the_successor_ceremony_that_keeps_the_key(self):
        cure = chatnode.refusal_remedy(self.LINES["clock-policy"][0])
        self.assertIn("successor ceremony", cure)
        self.assertIn("prepare mints the chain descriptor around the SAME key",
                      cure)

    def test_a_signer_side_refusal_records_its_cure_not_inspect_and_retry(self):
        from helm import chat
        d = chat._diag("send_failed", self.LINES["pq-identity"][0])
        self.assertIn("claimable stub", d["remediation"])
        d = chat._diag("send_failed", self.LINES["swap-veto"][0])
        self.assertIn("federation id", d["remediation"])
        d = chat._diag("send_failed", "node refused the turn: rate limited")
        self.assertIn("inspect `helm chat node status`", d["remediation"])


class ColouredJournalTest(BootBase):
    def test_a_coloured_error_line_is_found_and_uncoloured(self):
        """The real journal line carries escapes around ERROR, so it never
        contained " ERROR " and was never relayed."""
        with self.journal("Started helm-chat-node.service\n" + EPOCH_REFUSAL + "\n"):
            got = chatnode.last_failure()
        self.assertIsNotNone(got)
        self.assertIn("ERROR dregg_node: failed to initialize node state", got)
        self.assertNotIn(ESC, got)


class UnreachableDiagnosisTest(BootBase):
    """`helm chat node status` and `helm doctor` read one diagnosis."""

    def _status(self):
        out = io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None), \
                mock.patch("helm.chat.transport_status",
                           return_value={"mode": "unsigned"}), \
                contextlib.redirect_stdout(out):
            rc = chatnode.cmd_node(["status"])
        return rc, out.getvalue()

    def _doctor(self):
        from helm import cell, chat, doctor
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chat, "node_head", return_value=None), \
                mock.patch.object(chat, "transport_status",
                                  return_value={"mode": "unsigned"}), \
                mock.patch.object(cell, "bin_status",
                                  return_value={"configured": False,
                                                "usable": False}):
            return doctor.check_chat_node()

    def test_a_running_node_without_its_api_is_initializing_not_unreachable(self):
        rc, out = self._status()
        self.assertEqual(rc, 1)
        self.assertIn("INITIALIZING VERIFIED RUNTIME", out)
        self.assertNotIn("UNREACHABLE", out)
        rows = self._doctor()
        self.assertTrue(any(level == "WARN" and "INITIALIZING VERIFIED RUNTIME"
                            in text for level, text in rows), rows)

    def test_a_refused_node_names_the_refusal_and_its_cure(self):
        self.shows = [_show("failed", "failed", 5, "exit-code", RUN_ID)]
        with self.journal(EPOCH_REFUSAL + "\n"):
            _rc, out = self._status()
            rows = self._doctor()
        self.assertIn("REFUSED TO START", out)
        self.assertIn("cure: re-genesis", out)
        hit = [t for level, t in rows if level == "FAIL"]
        self.assertTrue(hit and "cure: re-genesis" in hit[0], rows)

    def test_a_url_that_is_not_this_units_is_never_diagnosed_from_its_journal(self):  # noqa: VACUOUS_ASSERTION — the spy is installed on the same observable, and test_this_units_url_asks_systemd_once is its positive control: the identical spy records one ("show", UNIT) call when the URL is this unit's
        """The spy is INSTALLED here, so an empty call list means systemd was
        never asked; the mirror arm below proves the same spy records a call
        when the URL is this unit's."""
        with mock.patch.object(chatnode, "_systemctl", self.systemctl):
            d = chatnode.boot_diagnosis("http://127.0.0.1:8899")
        self.assertEqual(d["state"], "unknown")
        self.assertEqual(self.calls, [])

    def test_this_units_url_asks_systemd_once(self):
        with mock.patch.object(chatnode, "_systemctl", self.systemctl):
            d = chatnode.boot_diagnosis("http://127.0.0.1:8898")
        self.assertEqual(d["state"], "initializing")
        self.assertEqual([c[:2] for c in self.calls], [("show", chatnode.UNIT)])

    def run_journal(self, current, older):
        """A journal that answers per scope: this run's lines when asked by
        _SYSTEMD_INVOCATION_ID, every line of the unit when asked by -u."""
        seen = []

        def run(argv, **_kw):
            seen.append(argv)
            scoped = any(a.startswith("_SYSTEMD_INVOCATION_ID=") for a in argv)
            return mock.Mock(returncode=0,
                             stdout=current if scoped else older + current)
        return mock.patch("helm.chatnode.subprocess.run", side_effect=run), seen

    def test_a_clean_stop_is_down_never_an_earlier_runs_refusal(self):
        """`helm chat node down` leaves inactive/dead with Result=success. An
        older run's store-epoch ERROR, twelve lines back in the journal, is
        not this stop's cause: status must not print REFUSED with a cure
        that moves the data dir aside, and doctor must not FAIL."""
        self.shows = [_show("inactive", "dead", 0, "success", RUN_ID)]
        older = EPOCH_REFUSAL + "\n" + "INFO dregg_node: served a turn\n" * 12
        # and THIS run logged an ERROR that has a cure but did not end it — a
        # running node reports THE SWAP authority inversion at ERROR and
        # keeps serving (measured on a scratch node). Result=success, not
        # the absence of an error line, is what says the stop was clean.
        current = (NONFATAL_SWAP_ERROR + "\n"
                   + "INFO dregg_node: HTTP server shut down gracefully\n")
        patch, _seen = self.run_journal(current, older)
        with patch:
            _rc, out = self._status()
            rows = self._doctor()
        self.assertIn("UNREACHABLE", out)
        self.assertNotIn("REFUSED", out)
        self.assertNotIn("re-genesis", out)
        self.assertEqual([t for level, t in rows if level == "FAIL"], [])
        self.assertTrue(any(level == "WARN" and "UNREACHABLE" in t
                            for level, t in rows), rows)

    def test_the_journal_is_read_for_the_run_that_exited(self):
        """A crash in THIS run is relayed from this run's lines; an older
        run's refusal further back never is."""
        self.shows = [_show("failed", "failed", 5, "exit-code", RUN_ID)]
        now_line = ("2026-09-24T04:04:40.680720Z ERROR dregg_node: failed to "
                    "bind 127.0.0.1:8898: address already in use")
        patch, seen = self.run_journal(now_line + "\n", EPOCH_REFUSAL + "\n")
        with patch:
            _rc, out = self._status()
        self.assertIn("the service said: " + now_line, out)
        self.assertNotIn("re-genesis", out)
        self.assertIn("_SYSTEMD_INVOCATION_ID=" + RUN_ID, seen[0])
        self.assertNotIn("-u", seen[0])

    def test_a_node_running_past_the_boot_wait_is_hung_not_initializing(self):
        self.shows = [_show("active", "running", 0, "success", RUN_ID,
                            started_ago=7200)]
        _rc, out = self._status()
        rows = self._doctor()
        self.assertIn("NOT INITIALIZING", out)
        self.assertNotIn("INITIALIZING VERIFIED RUNTIME", out)
        self.assertIn("hung", out)
        self.assertTrue(any(level == "FAIL" and "NOT INITIALIZING" in t
                            for level, t in rows), rows)

    def test_a_running_node_that_restarted_before_is_initializing(self):
        """NRestarts counts every restart since the unit was loaded; a node
        that crashed yesterday and is booting now has not exited now."""
        self.shows = [_show("active", "running", 3, "success", RUN_ID,
                            started_ago=20)]
        with self.journal(EPOCH_REFUSAL + "\n"), \
                mock.patch.object(chatnode, "_systemctl", self.systemctl):
            d = chatnode.boot_diagnosis("http://127.0.0.1:8898")
        self.assertEqual(d["state"], "initializing")

    def _diagnosis(self, *show, **kw):
        self.shows = [_show(*show, **kw)]
        with mock.patch.object(chatnode, "_systemctl", self.systemctl):
            return chatnode.boot_diagnosis("http://127.0.0.1:8898")

    def test_a_lowered_boot_wait_never_makes_a_booting_node_hung(self):
        """HELM_CHAT_NODE_BOOT_WAIT_S is `up`'s patience, and the fee-loop
        build answers in under a second, so 30 is a reasonable setting. It
        must not become the hung threshold: a rebased node 45 s into its
        150 s Lean init is booting, and doctor FAILing it (with status
        saying `down` and `up`) would kill a healthy boot. The threshold is
        the wait floored at its default; raising the wait raises it."""
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "30"
        booting = ("active", "running", 0, "success", RUN_ID)
        self.assertEqual(chatnode.hung_after_s(), 600)
        self.assertEqual(self._diagnosis(*booting, started_ago=45)["state"],
                         "initializing")
        self.assertEqual(self._diagnosis(*booting, started_ago=599)["state"],
                         "initializing")
        d = self._diagnosis(*booting, started_ago=601)
        self.assertEqual(d["state"], "hung")
        self.assertIn("600s boot wait", d["line"])
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "1200"
        self.assertEqual(chatnode.hung_after_s(), 1200)
        self.assertEqual(self._diagnosis(*booting, started_ago=601)["state"],
                         "initializing")
        self.assertEqual(self._diagnosis(*booting, started_ago=1201)["state"],
                         "hung")

    def test_a_node_being_stopped_is_down_not_hung(self):
        """`systemctl stop` on a node that served for two hours leaves it
        `deactivating` for up to TimeoutStopSec. Its process has run far
        past the boot wait without (now) answering, but a stop in flight is
        not a hung boot: down, no FAIL, no `down` and `up` advice."""
        self.shows = [_show("deactivating", "stop-sigterm", 0, "success",
                            RUN_ID, started_ago=7200)]
        with mock.patch.object(chatnode, "_systemctl", self.systemctl):
            d = chatnode.boot_diagnosis("http://127.0.0.1:8898")
        self.assertEqual(d, {"state": "down", "line": None, "remedy": None})
        _rc, out = self._status()
        rows = self._doctor()
        self.assertIn("UNREACHABLE", out)
        self.assertNotIn("hung", out)
        self.assertTrue(any(level == "WARN" and "UNREACHABLE" in t
                            for level, t in rows), rows)
        self.assertEqual([t for level, t in rows if level == "FAIL"], [])


if __name__ == "__main__":
    unittest.main()
