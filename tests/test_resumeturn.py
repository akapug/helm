#!/usr/bin/env python3
"""helm.resumeturn tests — the post-compaction resume leg.

Pins the laws that make `compaction-has-no-resume-leg` un-reoccurrable:
SOURCE-GATED (only SessionStart source=compact ever acts); DETACHED (the hook
forks a child and returns, it never injects inline); the directive is the
seat's OWN fresh handoff and falls back to the generic re-ground line rather
than a confident wrong one; an unidentifiable pane ALERTS loudly and never
guesses; and the loop guard makes compact→resume→compact impossible by
construction (debounce / spiral / rolling cap).

Hermetic: HELM_HOME + HELM_CHAT_DIR are tmp dirs, the child spawn seam and
chat.post are recorders, a fake adapter stands in for the metaharness. No real
pane, no real process, no chat node.
"""
import contextlib
import fcntl
import io
import json
import os
import re
import shlex
import shutil
import tempfile
import time
import unittest
from unittest import mock

from helm import (composers, handoff, harness, hooks, inject, orcaadopt, pk,
                  resumeturn, seat, seats, tasks)

SID = "33333333-3333-3333-3333-333333333333"
_ENV = ("HELM_SUBMIT_SETTLE_S", "HELM_PROC",
        "HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME",
        # HELM_ADOPTED_DIR joins the restore set because the end-to-end
        # cleared-seat test plants a real store to gather from. A module that
        # sets an env var and does not put it back is what
        # test_env_hygiene.test_every_test_module_restores_the_env_vars_it_sets
        # exists to catch, and it caught this one.
        "HELM_ADOPTED_DIR",
        "HELM_RESUME_TURN", "HELM_RESUME_TURN_SETTLE_S",
        "HELM_RESUME_TURN_DEBOUNCE_S", "HELM_RESUME_TURN_SPIRAL_S",
        "HELM_RESUME_TURN_MAX", "HELM_RESUME_TURN_WINDOW_S",
        "HELM_RESUME_TURN_INJECTION_TTL_S",
        "HELM_RESUME_TURN_RECOVERY_PERSIST_S",
        "HELM_RESUME_TURN_RECOVERY_BACKOFF_S",
        "HELM_HANDOFF_EVENT_FRESH_H")


# A pane whose composer is EMPTY — one that took its turn. Synthetic, but a
# real frame's shape: `submit` proves delivery by reading the composer back, so
# a double returning "" models an UNREADABLE pane (UNKNOWN), not a working one.
ADVANCED_PANE = "\n".join(("─" * 40, "❯", "─" * 40,
                           "  opus-5 | ~/dev/example/repo",
                           "  ⏵⏵ bypass permissions on"))

# The SAME pane with a draft sitting in the composer — built from
# ADVANCED_PANE's own shape so the two differ in exactly one thing: the text
# on the prompt line. That is what makes the pair below a discrimination.
HELD_PANE = "\n".join(("─" * 40,
                       "❯\xa0Run `helm seat boot-brief --rearm`.",
                       "─" * 40,
                       "  opus-5 | ~/dev/example/repo",
                       "  ⏵⏵ bypass permissions on"))


class FakeAdapter(harness._CLIAdapter):
    """Inherits the REAL `submit` (split send + read-back) from the base, so a
    second implementation cannot agree with a broken one."""
    name = "fake"

    def __init__(self, panes=({"handle": "h1", "title": "codex",
                               "status": "connected"},)):
        self.panes, self.sent = list(panes), []
        self.typed = None
        self.enter_attempted = False

    def list(self):
        return self.panes

    def read(self, handle, limit=3000, timeout=60):
        if self.typed and not self.enter_attempted:
            return "\n".join(("─" * 40, "❯\xa0" + self.typed, "─" * 40,
                              "  opus-5 | ~/dev/example/repo"))
        return ADVANCED_PANE

    def send(self, handle, text, enter=True):
        if enter:
            self.enter_attempted = True
        else:
            self.typed = text
        self.sent.append((handle, text, enter))


class ResumeTurnBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-resume-")
        self._env = {k: os.environ.pop(k, None) for k in _ENV}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NAME"] = "codex"      # this process IS the seat
        os.environ["HELM_RESUME_TURN_SETTLE_S"] = "0"
        os.environ["HELM_SUBMIT_SETTLE_S"] = "0"   # submit's type->Enter gap
        # ...and the gap between its verify reads. The pane doubles here
        # answer per read and never consult the clock, so all
        # SUBMIT_VERIFY_READS reads still run and a submit that never
        # verifies still fails after the last one; only the 0.8s sleeps
        # between them go.
        gap = mock.patch.object(harness, "SUBMIT_VERIFY_INTERVAL_S", 0)
        gap.start()
        self.addCleanup(gap.stop)
        os.environ["HELM_RESUME_TURN_RECOVERY_PERSIST_S"] = "0"
        os.environ["HELM_RESUME_TURN_RECOVERY_BACKOFF_S"] = "0"
        self.cwd = os.path.join(self.tmp, "repo")
        os.makedirs(self.cwd)
        self.d = seat._instance_dir("codex", "codex")
        os.makedirs(self.d, exist_ok=True)
        self.spawn()
        self.posts = []
        self.post_kw = []       # kwargs beside each post — the DM lane rides here
        self.spawned = []
        self.wake_payloads = []

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, session=SID, harness="fake", handle="h1", seat_name="codex"):
        with open(os.path.join(self.d, "spawn.json"), "w") as f:
            json.dump({"v": 1, "seat": seat_name, "harness": harness,
                       "handle": handle, "session": session}, f)

    def transcript_path(self, *lines):
        """A real transcript file, because the hook only forwards a
        TRUTHY transcript_path across the fork."""
        p = os.path.join(self.tmp, "transcript.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        return p

    def payload(self, source="compact", sid=SID, transcript=""):
        return {"source": source, "session_id": sid, "cwd": self.cwd,
                "transcript_path": transcript, "hook_event_name": "SessionStart"}

    def journal(self, next_line="finish the landreq delivery leg", age_s=0,
                sid=SID, project="proj", seat=None):
        d = os.path.join(handoff.home.project_dir(project), "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-28-handoff-%s.md" % sid[:8])
        pk.atomic_write(p, "\n".join([
            "---", "name: h", "metadata:", "  type: handoff",
            "  session_id: " + sid, "  next: " + next_line,
            "  seat: " + (seat or ""),
            "  done: shipped the guard", "  remaining: docs",
            "---", "", "body", ""]))
        if age_s:
            t = time.time() - age_s
            os.utime(p, (t, t))
        return p

    def run_hook(self, payload=None, project="proj"):
        """hook() with the spawn seam + chat.post recorded, project resolved."""
        with mock.patch.object(inject, "project_for_cwd", return_value=project), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.record_spawn), \
                mock.patch.object(seats, "beacon_procs", return_value=([], None)), \
                mock.patch("helm.chat.post",
                           side_effect=lambda text, **kw:
                           (self.posts.append(text),
                            self.post_kw.append(kw))):
            return resumeturn.hook(payload or self.payload())

    def record_spawn(self, argv, pass_fds=()):
        """The spawn seam recorder: keeps the argv AND drains any wake pipe
        at spawn time — the parent closes its read end right after the real
        spawn would inherit it, so the payload must be read here or never."""
        self.spawned.append(argv)
        if "--alert-fd" in argv:
            fd = int(argv[argv.index("--alert-fd") + 1])
            raw = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
            self.wake_payloads.append(json.loads(raw.decode("utf-8")))

    def plant_holder(self, seat, pid=4242):
        """A fixture /proc tree: one live pid whose stat carries a starttime
        and whose environ claims `seat` — the kernel evidence _holder_proves
        re-reads. HELM_PROC points the prover here."""
        proc = os.path.join(self.tmp, "proc", str(pid))
        os.makedirs(proc, exist_ok=True)
        with open(os.path.join(proc, "stat"), "w", encoding="utf-8") as f:
            f.write("%d (claude) S " % pid
                    + " ".join(str(n) for n in range(3, 25)))
        with open(os.path.join(proc, "environ"), "wb") as f:
            f.write(b"HELM_CHAT_NAME=" + seat.encode("utf-8") + b"\x00X=1\x00")
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        from helm import orcaadopt
        return "%d:%s" % (pid, orcaadopt.proc_start(pid))

    def wake_children(self):
        """The hook-authored wake payloads, as drained at spawn time."""
        return list(self.wake_payloads)

    def run_wakes(self, calls, armed=True):
        """Execute every spawned wake payload through the REAL child seams
        (resolver included), recording chat.post and measuring a synthetic
        beacon — the hook/child composition in one process."""
        outs = []
        with mock.patch("helm.chat.post",
                        side_effect=lambda text, **kw:
                        calls.append((text, kw))), \
                mock.patch.object(seats, "beacon_procs",
                                  return_value=([123] if armed else [],
                                                None)):
            for p in self.wake_children():
                outs.append(resumeturn.wake_alert(p))
        return outs

    def child_text(self):
        """The text the spawned child was handed — read from the file the hook
        wrote, exactly as the real child reads it."""
        argv = self.spawned[-1]
        with open(argv[argv.index("--text-file") + 1], encoding="utf-8") as f:
            return f.read()

    def run_installed(self, payload):
        """The hook as hooks.py installs it: payload on stdin, stdout read;
        its stderr is kept on `self.last_stderr` for the arms that assert
        the one diagnostic line."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.record_spawn), \
                mock.patch.object(seats, "beacon_procs",
                                  return_value=([], None)), \
                mock.patch("helm.chat.post",
                           side_effect=lambda text, **kw:
                           (self.posts.append(text),
                            self.post_kw.append(kw))), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", err):
            rc = resumeturn.cmd_resume_turn(["--hook-json"])
        self.last_stderr = err.getvalue()
        return rc, out.getvalue()


def _mode_for_delivered():
    """What the REAL `deliver` returns on a successful submit.

    DERIVED, NEVER TRANSCRIBED. `deliver` ends in `_mode_for(state, proof)`,
    which maps harness.DELIVERED -> "resumed"; a double returning the raw
    adapter state hands back "delivered", a vocabulary production never emits
    at this seam. Every arm asserting mode == "resumed" then fails against a
    fixture defect rather than a code one — measured on train 4, receipt
    da638251f84ac232, four WithdrawalTest arms. Calling the real mapper means
    a future change to that vocabulary moves the double with it.
    """
    return resumeturn._mode_for(harness.DELIVERED, "typed")


class SourceGateTest(ResumeTurnBase):
    def test_only_a_compaction_arms_the_resume(self):  # noqa: VACUOUS_ASSERTION — the empty-`spawned` assertions are the SOURCE GATE's contract, and the unconditional positive control is the compact case below the loop: same observable, same fixture, asserting len(self.spawned) == 1
        """SessionStart fires on startup/resume/clear/compact. Every other
        source already HAS a live turn loop; arming there would inject a
        directive into a session that is about to be given one by a human."""
        for src in ("startup", "resume", "clear", ""):
            with self.subTest(source=src):
                self.spawned = []
                res = self.run_hook(self.payload(source=src))
                self.assertEqual(res["action"], "skip")
                self.assertEqual(self.spawned, [])
        # THE FIXTURE MUST CARRY WHAT THE ASSERTION ASKS FOR. `payload`
        # defaults transcript to "", `_child_argv` appends --transcript
        # only when truthy, so asserting the flag on the default payload
        # asserted a flag the fixture guaranteed absent. A real
        # compaction always carries transcript_path.
        res = self.run_hook(self.payload(source="compact",
                                         transcript=self.transcript_path()))
        self.assertEqual(res["action"], "spawned")
        # THE FORK IS STILL ARMED ON A COMPACTION AND THAT IS NOT THE SAME
        # CLAIM IT USED TO BE. It once meant "the pane WILL be typed into";
        # it now means "a child is armed to type into the pane UNLESS it can
        # prove the seat already woke". The withdrawal lives in the child, so
        # this arm — which is about the SOURCE GATE — is untouched by the
        # cure, and it stays here to keep proving that startup/resume/clear
        # arm nothing. `WithdrawalTest` below owns the second question.
        self.assertEqual(len(self.spawned), 1)
        argv = self.spawned[-1]
        # THE CHILD CANNOT WITHDRAW WITHOUT THE EVIDENCE, so the hook owes it
        # across the fork. Asserting the argv here is what makes the wiring
        # observable from the source-gate side: a refactor that stops passing
        # either flag turns the cure off silently and every WithdrawalTest arm
        # below would still pass, because they call `child` directly.
        self.assertIn("--transcript", argv)
        self.assertIn("--since", argv)

    def test_every_session_start_source_drops_the_suppression(self):
        """THE PREDICATE IS "DID THE CONTEXT GO AWAY", NOT "IS IT COMPACT", and
        it is a DENY-LIST so an unknown source resets. /clear wipes the context
        and KEEPS the session id, so the seen file still names every entry the
        seat was sent while the seat holds none of it. The source gate used to
        return BEFORE forget_session, which made inject's own landed comment
        ("COMPACTION or /clear -> FIRES") false, and session-long JIT
        suppression turned that into a silencer. The owner /clears seats.

        CONTEXT_PRESERVING_SOURCES IS EMPTY, so `resume` resets too. It proves
        a transcript can be OPENED, not that the retained context equals the
        seen-file's claim: a seat was resumed onto a session `cv prune` had
        stripped of 1,260 turns, and that resume returned STRICTLY LESS context
        than the session it resumed. The unknown-source arm carries the
        asymmetry — staying suppressed after an unrecognised context loss makes
        a seat MUTE and unable to ask for what it does not know is missing,
        while resetting needlessly costs ONE re-delivery. So this asserts a
        source nobody has enumerated resets too.

        The two behaviours are SEPARATE and this pins both — every context loss
        forgets, while only a compaction ever arms the resume. (two reviewers
        found the /clear leg independently; deny-list default per
        the integrator, board #181; empty tuple by unanimous meld
        e:1785817308.)"""
        seen = inject._seen_path(SID)
        # UNCONDITIONAL CONTROL, outside the loop: planting really writes, and
        # it writes INSIDE this test's tmp home. Without this, an empty or
        # skipped loop would satisfy every assertion below by running none.
        os.makedirs(os.path.dirname(seen), exist_ok=True)
        pk.write_json(seen, {"v": 1, "ts": pk.now_ts(), "turn": 1, "fired": {}})
        self.assertTrue(os.path.exists(seen))
        self.assertTrue(seen.startswith(self.tmp), seen)
        for src, must_forget in (("clear", True), ("compact", True),
                                 ("startup", True), ("", True),
                                 ("a-source-that-does-not-exist-yet", True),
                                 ("resume", True)):
            with self.subTest(source=src):
                os.makedirs(os.path.dirname(seen), exist_ok=True)
                pk.write_json(seen, {"v": 1, "ts": pk.now_ts(), "turn": 4,
                                     "fired": {"jit-a": [1, 0.8]},
                                     "pinned": "a-fingerprint"})
                # CONTROL: the state is really there before the hook runs, so
                # "it is gone" below cannot be satisfied by a file that never
                # existed or a path pointing outside this tmp home.
                self.assertTrue(os.path.exists(seen))
                res = self.run_hook(self.payload(source=src))
                self.assertEqual(not os.path.exists(seen), must_forget,
                                 "source=%s forget mismatch" % src)
                if src != "compact":
                    self.assertEqual(res["action"], "skip",
                                     "only a compaction ever arms the resume")

    def test_a_cleared_seat_actually_receives_again_on_its_next_turn(self):  # noqa: VACUOUS_ASSERTION — `first` is an unconditional positive control on the SAME observable (premise_lines()): it must be non-empty before the empty-assertion runs, and the final assertion compares back to it
        """END TO END, because "the file is gone" is a mechanism and what the
        owner cares about is that a cleared seat gets its premises BACK. Runs
        the real gather across the boundary rather than asserting on state."""
        from helm import store
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"], exist_ok=True)
        store.write_prior({"id": "pin-a", "statement": "always truth",
                           "confidence": "1.0", "pin": "true"})
        def premise_lines():
            # THE PREMISE ONLY. This fixture names itself `codex`, so the
            # codex-only SA nudges ride the pinned lane and STILL fire when the
            # premise block is suppressed (they are appended after the
            # fingerprint and inherit the freed budget). Asserting on the whole
            # lane would have made this test about the nudges.
            return [l for l in inject.gather("anything", session=SID)["pinned"]
                    if "always truth" in l]
        first = premise_lines()
        self.assertTrue(first, "CONTROL: the premise must fire on the warming turn")
        self.assertEqual(premise_lines(), [], "CONTROL: and be suppressed next")
        self.run_hook(self.payload(source="clear"))
        self.assertEqual(premise_lines(), first,
                         "a /clear'd seat must receive its premises again")

    def test_an_unlinkable_seen_file_is_truncated_and_never_silently_kept(self):  # noqa: VACUOUS_ASSERTION — the populated _seen_load assertion above is an unconditional positive control on the SAME observable, and the file is proven readable-and-unlinkable before the fresh-read is asserted
        """A review at 6f7cb572. forget_session swallowed every OSError from
        unlink, so a READABLE seen file under a NON-WRITABLE PARENT survived a
        context boundary and the seat stayed suppressed against premises it no
        longer held — mute, and unable to notice.

        Everywhere else in the injector fail-open is right because the failure
        direction is a wasted re-delivery. HERE it inverts. Unlink needs write
        on the DIRECTORY; truncation needs write on the FILE, a different
        permission and the one that survives this storage class, and a
        truncated file reads as a fresh session."""
        from helm.inject import _ledger
        seen = inject._seen_path(SID)
        os.makedirs(os.path.dirname(seen), exist_ok=True)
        pk.write_json(seen, {"v": 1, "ts": pk.now_ts(), "turn": 4,
                             "fired": {"jit-a": [1, 0.8]}, "pinned": "fp"})
        # CONTROL: the state is real and the loaded record is populated, so
        # "it is gone" below is a mutation and not an absent fixture.
        self.assertEqual(_ledger._seen_load(SID)["fired"], {"jit-a": [1, 0.8]})
        d = os.path.dirname(seen)
        os.chmod(d, 0o500)                     # readable file, unwritable parent
        self.addCleanup(os.chmod, d, 0o700)
        with open(seen) as f:                  # CONTROL: still readable...
            self.assertIn("jit-a", f.read())
        with self.assertRaises(OSError):       # ...and genuinely unlinkable
            os.remove(seen)
        self.assertTrue(_ledger.forget_session(SID),
                        "truncation must succeed where unlink cannot")
        self.assertEqual(_ledger._seen_load(SID),
                         {"turn": 0, "fired": {}, "pinned": None,
                          "nudges": {}, "lines": {}, "epoch": None,
                          "who": None, "subs": [], "posture": None},
                         "a truncated file must read as a FRESH session")

    def test_a_boundary_that_cannot_clear_alerts_instead_of_shrugging(self):
        """The seat cannot detect this itself, so the alert IS the remedy."""
        from helm.inject import _ledger
        with mock.patch.object(_ledger, "forget_session", return_value=False):
            self.run_hook(self.payload(source="clear"))
        wakes = self.wake_children()
        self.assertTrue(any("SUPPRESSION SURVIVED" in w.get("reason", "")
                            for w in wakes),
                        "a survived boundary must be LOUD: %r" % (wakes,))
        self.assertTrue(all(not w.get("seat") for w in wakes),
                        "the suppression alert is room-only by design")
        # CONTROL: the same path stays quiet when the clear succeeds, so the
        # assertion above is the failure arm and not an alert that always fires.
        self.spawned, self.wake_payloads = [], []
        with mock.patch.object(_ledger, "forget_session", return_value=True):
            self.run_hook(self.payload(source="clear"))
        self.assertFalse(any("SUPPRESSION SURVIVED" in w.get("reason", "")
                             for w in self.wake_children()))

    def test_a_missing_seen_file_is_idempotent_success(self):
        from helm.inject import _ledger
        seen = inject._seen_path("never-existed")
        # CONTROL FIRST: this path really is where a file for that session
        # WOULD live, proven by putting one there and clearing it. Without it,
        # "absent" could just mean I computed a path nothing ever uses.
        os.makedirs(os.path.dirname(seen), exist_ok=True)
        pk.write_json(seen, {"v": 1, "ts": pk.now_ts(), "turn": 1, "fired": {}})
        self.assertTrue(os.path.exists(seen))
        self.assertTrue(_ledger.forget_session("never-existed"))
        self.assertFalse(os.path.exists(seen))
        # and AGAIN on the now-missing file — idempotent, not merely lucky
        self.assertTrue(_ledger.forget_session("never-existed"))

    def test_kill_switch_disarms_without_an_edit(self):
        os.environ["HELM_RESUME_TURN"] = "0"
        res = self.run_hook()
        self.assertEqual(res["action"], "off")
        self.assertEqual(self.spawned, [])


class DetachedInjectionTest(ResumeTurnBase):
    def test_the_hook_forks_a_child_and_never_injects_inline(self):
        """A SessionStart hook runs BEFORE the session resumes, so the hook
        itself must not touch the pane — it hands the injection to a detached
        child that waits out the composer settle."""
        ad = FakeAdapter()
        with mock.patch("helm.harness.detect", return_value=ad):
            res = self.run_hook()
        self.assertEqual(res["action"], "spawned")
        self.assertEqual(ad.sent, [], "the hook itself must never inject")
        argv = self.spawned[0]
        self.assertIn("--deliver", argv)
        self.assertEqual(argv[argv.index("--seat") + 1], "codex")
        self.assertEqual(argv[argv.index("--session") + 1], SID)
        self.assertTrue(argv[0].endswith("bin/helm"), argv[0])

    def test_the_settle_delay_rides_the_child_and_is_env_overridable(self):
        os.environ["HELM_RESUME_TURN_SETTLE_S"] = "4.5"
        self.run_hook()
        argv = self.spawned[0]
        self.assertEqual(float(argv[argv.index("--delay") + 1]), 4.5)
        self.assertEqual(resumeturn.settle_s(), 4.5)

    def test_the_child_injects_the_directive_into_the_registered_pane(self):
        ad = FakeAdapter()
        mode, detail = resumeturn.child("codex", SID, "GO NOW", 0, adapter=ad)
        self.assertEqual(mode, "resumed", detail)
        self.assertEqual(ad.sent, [("h1", "GO NOW", False), ("h1", "", True)])

    def test_a_multiline_directive_becomes_one_line_then_a_bare_enter(self):
        ad = FakeAdapter()
        mode, detail = resumeturn.child(
            "codex", SID, "continue this lane\nthen run the focused gate", 0,
            adapter=ad)
        self.assertEqual(mode, "resumed", detail)
        self.assertEqual(
            ad.sent,
            [("h1", "continue this lane then run the focused gate", False),
             ("h1", "", True)],
            "continuation mode must never absorb a trailing Enter")

    def test_blind_preread_retries_whole_delivery_and_reproves_identity(self):  # noqa: VACUOUS_ASSERTION — resumed mode, two pane-action calls, and exact h2 sends positively control the retry
        class BlindFirstHandle(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                if handle == "h1":
                    return "pane repainting"
                return super().read(handle, limit=limit, timeout=timeout)

        handles = iter(("h1", "h2"))
        ad = BlindFirstHandle()

        def reproved(_row, _adapter, action, **_kw):
            handle = next(handles)
            return action(ad, handle, "re-proved %s" % handle), None

        with mock.patch("helm.autocompact._pane_action",
                        side_effect=reproved) as pane_action, \
                mock.patch.object(resumeturn, "_alert") as alert:
            mode, detail = resumeturn.child(
                "codex", SID, "GO NOW", 0, adapter=ad)
        self.assertEqual(mode, "resumed", detail)
        self.assertEqual(pane_action.call_count, 2)
        self.assertEqual(ad.sent,
                         [("h2", "GO NOW", False), ("h2", "", True)])
        alert.assert_not_called()

    def test_blind_preread_alerts_only_after_whole_delivery_retries(self):  # noqa: VACUOUS_ASSERTION — exact call count and zero sends positively control retry exhaustion before alert
        class Blind(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                return "pane repainting"

        ad = Blind()
        calls = {"n": 0}

        def reproved(_row, _adapter, action, **_kw):
            calls["n"] += 1
            handle = "h%d" % calls["n"]
            return action(ad, handle, "re-proved %s" % handle), None

        with mock.patch("helm.autocompact._pane_action",
                        side_effect=reproved) as pane_action, \
                mock.patch.object(resumeturn, "_alert") as alert:
            mode, detail = resumeturn.child(
                "codex", SID, "GO NOW", 0, adapter=ad)
        self.assertEqual(mode, "unverified", detail)
        self.assertEqual(pane_action.call_count,
                         resumeturn.RECOVERY_ATTEMPTS)
        self.assertIn("whole-delivery pre-read attempts exhausted", detail)
        self.assertIn("attempt 2", detail)
        self.assertEqual(ad.sent, [])
        alert.assert_called_once()

    def test_dirty_composer_alerts_once_without_exporting_or_retrying_draft(self):  # noqa: VACUOUS_ASSERTION — exact one transaction, zero sends, redacted alert, and persisted detail positively control the refusal
        secret = "unfinished draft with sk-test-secret"
        dirty = "\n".join(("─" * 40, "❯\xa0" + secret, "─" * 40,
                            "  opus-5 | ~/dev/example/repo"))

        class Dirty(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                return dirty

        ad = Dirty()

        def reproved(_row, _adapter, action, **_kw):
            return action(ad, "h1", "re-proved h1"), None

        with mock.patch("helm.autocompact._pane_action",
                        side_effect=reproved) as pane_action, \
                mock.patch.object(resumeturn, "_alert") as alert:
            mode, detail = resumeturn.child(
                "codex", SID, "GO NOW", 0, adapter=ad)
        self.assertEqual(mode, "unverified", detail)
        self.assertEqual(pane_action.call_count, 1)
        self.assertEqual(ad.sent, [])
        self.assertNotIn(secret, detail)
        self.assertIn("redacted", detail)
        self.assertNotIn(secret, str(alert.call_args))
        self.assertNotIn(secret, resumeturn._peek("codex")["detail"])

    def test_provenance_write_failure_after_typing_never_retypes(self):
        class UnreadableAfterEnter(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                if self.enter_attempted:
                    raise harness.HarnessError("tail unreadable after Enter")
                return super().read(handle, limit=limit, timeout=timeout)

        ad = UnreadableAfterEnter()

        def reproved(_row, _adapter, action, **_kw):
            return action(ad, "h1", "re-proved h1"), None

        with mock.patch("helm.autocompact._pane_action",
                        side_effect=reproved) as pane_action, \
                mock.patch.object(resumeturn, "_record_injection",
                                  return_value=False), \
                mock.patch.object(resumeturn, "_alert") as alert:
            mode, detail = resumeturn.child(
                "codex", SID, "GO NOW", 0, adapter=ad)
        self.assertEqual(mode, "unverified", detail)
        self.assertEqual(pane_action.call_count, 1)
        self.assertEqual(ad.sent,
                         [("h1", "GO NOW", False), ("h1", "", True)])
        alert.assert_called_once()

    def test_long_directive_uses_a_short_unique_retrieval_prompt(self):  # noqa: VACUOUS_ASSERTION — first/second are non-empty positive controls before length and inequality checks
        text = "continue the assigned lane " * 20
        first = resumeturn._wire_text("codex", SID, text)
        second = resumeturn._wire_text("seat-b", SID, text)
        self.assertLessEqual(len(first), resumeturn.WIRE_TEXT_BYTES)
        self.assertNotEqual(first, second,
                            "identical directives for two seats must not collide")
        token = first.split("--show ", 1)[1].rstrip("`")
        self.assertEqual(resumeturn.show_directive(token),
                         " ".join(text.split()))

    def test_show_surface_resolves_the_stored_long_directive(self):
        text = "continue the assigned lane " * 20
        wire = resumeturn._wire_text("codex", SID, text)
        token = wire.split("--show ", 1)[1].rstrip("`")
        with mock.patch("builtins.print") as printed:
            rc = resumeturn.cmd_resume_turn(["--show", token])
        self.assertEqual(rc, 0)
        printed.assert_called_once_with(" ".join(text.split()))

    def test_child_submits_the_short_wire_and_keeps_the_full_directive(self):  # noqa: VACUOUS_ASSERTION — resumed mode and ad.sent are positive controls on the same delivery
        text = "continue the assigned lane " * 20
        ad = FakeAdapter()
        mode, detail = resumeturn.child("codex", SID, text, 0, adapter=ad)
        self.assertEqual(mode, "resumed", detail)
        placed = ad.sent[0][1]
        self.assertLessEqual(len(placed), resumeturn.WIRE_TEXT_BYTES)
        self.assertNotEqual(placed, text)
        token = placed.split("--show ", 1)[1].rstrip("`")
        self.assertEqual(resumeturn.show_directive(token),
                         " ".join(text.split()))

    def test_a_persistent_recorded_injection_recovers_without_human_input(self):
        text = "GO NOW"
        held = "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                           "  opus-5 | ~/dev/example/repo"))

        class StuckThenMoves(FakeAdapter):
            def __init__(self):
                super().__init__()
                reads = harness.SUBMIT_VERIFY_READS
                self.tails = ([ADVANCED_PANE, held] + [held] * reads
                              + [held, ADVANCED_PANE])

            def read(self, handle, limit=3000, timeout=60):
                return self.tails.pop(0)

        ad = StuckThenMoves()
        with mock.patch.object(resumeturn, "_route_recovery",
                               return_value=False):
            mode, detail = resumeturn.child(
                "codex", SID, text, 0, adapter=ad)
        self.assertEqual(mode, "resumed", detail)
        self.assertEqual(ad.sent,
                         [("h1", text, False), ("h1", "", True),
                          ("h1", "", True)])
        self.assertNotIn("injection", resumeturn._peek("codex"),
                         "measured delivery clears the provenance record")

    def test_persistence_without_recorded_identity_never_authorizes_enter(self):  # noqa: VACUOUS_ASSERTION — two populated pane rows positively control the zero-Enter refusal
        text = "GO NOW"
        panes = ({"handle": "h1", "title": "codex", "worktree": "",
                  "last_output_at": 1},
                 {"handle": "h2", "title": "peer", "worktree": "",
                  "last_output_at": 1})

        class UnownedHeld(FakeAdapter):
            def __init__(self):
                super().__init__(panes)

            def read(self, handle, limit=3000, timeout=60):
                if handle == "h1":
                    return "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                                      "  opus-5 | ~/dev/example/repo"))
                return ADVANCED_PANE

        ad = UnownedHeld()
        for _ in range(2):
            rows, err = composers.scan(adapter=ad)
            self.assertIsNone(err)
            by_handle = {r["handle"]: r for r in rows}
            self.assertEqual(by_handle["h1"]["state"], composers.HELD)
            self.assertEqual(by_handle["h2"]["state"], composers.CLEAR)
        state, detail = composers.submit("h1", adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        # THIS PANE HAS NO RECORD AT ALL, which is a different sentence from
        # "the record expired" — and the refusal used to conflate them by
        # saying "no unique, UNEXPIRED record" in both cases. task/1988 split
        # them, so assert the DISCRIMINATION rather than transcribing a
        # sentence: absence is named, and expiry is NOT blamed for it.
        self.assertIn("no unique Helm injection record", detail)
        self.assertNotIn("expired", detail)
        self.assertNotIn("identity proven", detail)
        self.assertEqual(ad.sent, [],
                         "persistence alone must spend zero Enters")

    def test_one_recorded_snapshot_never_authorizes_enter(self):  # noqa: VACUOUS_ASSERTION — pending and clear pane rows positively control the zero-Enter refusal
        text = "GO NOW"
        self.assertTrue(resumeturn._record_injection(
            "codex", SID, "h1", text))
        panes = ({"handle": "h1", "title": "codex", "worktree": "",
                  "last_output_at": 1},
                 {"handle": "h2", "title": "peer", "worktree": "",
                  "last_output_at": 1})

        class OneSnapshot(FakeAdapter):
            def __init__(self):
                super().__init__(panes)

            def read(self, handle, limit=3000, timeout=60):
                if handle == "h1":
                    return "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                                      "  opus-5 | ~/dev/example/repo"))
                return ADVANCED_PANE

        ad = OneSnapshot()
        with mock.patch.object(resumeturn, "recovery_persist_s",
                               return_value=30), \
                mock.patch.object(composers.time, "time", return_value=100):
            rows, err = composers.scan(adapter=ad)
        self.assertIsNone(err)
        by_handle = {r["handle"]: r for r in rows}
        self.assertEqual(by_handle["h1"]["state"], composers.HELM_PENDING)
        self.assertEqual(by_handle["h2"]["state"], composers.CLEAR)
        with mock.patch.object(resumeturn, "recovery_persist_s",
                               return_value=30):
            state, detail = composers.submit("h1", adapter=ad, now=100)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("has not remained held", detail)
        self.assertEqual(ad.sent, [],
                         "one recorded snapshot must spend zero Enters")

    def test_typed_but_unreadable_injection_routes_without_recovery_enter(self):  # noqa: VACUOUS_ASSERTION — unverified mode and the exact initial two sends positively control zero retry Enter
        class UnreadableAfterEnter(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                if self.enter_attempted:
                    raise harness.HarnessError("composer tail scrolled away")
                return super().read(handle, limit=limit, timeout=timeout)

        ad = UnreadableAfterEnter()
        with mock.patch.object(resumeturn, "_route_recovery",
                               return_value=(None, "task ledger refused")) as route, \
                mock.patch.object(resumeturn, "_alert") as alert:
            mode, detail = resumeturn.child(
                "codex", SID, "GO NOW", 0, adapter=ad)
        self.assertEqual(mode, "unverified", detail)
        self.assertEqual(ad.sent,
                         [("h1", "GO NOW", False), ("h1", "", True)],
                         "UNKNOWN must not guess with another bare Enter")
        route.assert_called_once()
        self.assertEqual(route.call_args.args[0], "codex")
        self.assertEqual(route.call_args.args[1]["handle"], "h1")
        self.assertTrue(route.call_args.args[1]["generation"])
        self.assertIn("recovery routing UNKNOWN: task ledger refused", detail)
        alert.assert_not_called()

    def test_direct_delivery_clear_failure_mints_terminal_task(self):  # noqa: VACUOUS_ASSERTION — resumed mode and the closed terminal row positively control the blocked second Enter
        ad = FakeAdapter()
        mutate = resumeturn._mutate_entry
        calls = {"n": 0}
        def fail_clear(key, change, nonblocking=False):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("state store unwritable")
            return mutate(key, change, nonblocking=nonblocking)
        with mock.patch.object(resumeturn, "_mutate_entry",
                               side_effect=fail_clear):
            mode, detail = resumeturn.child(
                "codex", SID, "GO NOW", 0, adapter=ad)
        self.assertEqual(mode, "resumed", detail)
        injection = resumeturn.recorded_injections()["h1"]
        rows, unavailable = tasks.snapshot(strict=True)
        self.assertIsNone(unavailable)
        terminal = next(iter(rows.values()))
        self.assertEqual(terminal["status"], "closed")
        self.assertIn("exact recorded injection delivered",
                      terminal["closed_reason"])
        spent = list(ad.sent)
        state, _detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, spent,
                         "terminal task must block a second recovery Enter")

    def test_both_terminal_stores_failing_is_loud_unknown_not_resumed(self):  # noqa: VACUOUS_ASSERTION — unverified detail and surviving injection positively control the no-alert refusal
        ad = FakeAdapter()
        mutate = resumeturn._mutate_entry
        calls = {"n": 0}
        def fail_clear(key, change, nonblocking=False):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("state store unwritable")
            return mutate(key, change, nonblocking=nonblocking)
        with mock.patch.object(resumeturn, "_mutate_entry",
                               side_effect=fail_clear), \
                mock.patch.object(tasks, "add",
                                  return_value=(None, "task store unwritable")), \
                mock.patch.object(resumeturn, "_recovery_owner",
                                  return_value=(None, None)), \
                mock.patch.object(resumeturn, "_alert") as alert:
            mode, detail = resumeturn.child(
                "codex", SID, "GO NOW", 0, adapter=ad)
        self.assertEqual(mode, "unverified", detail)
        self.assertIn("no terminal authority persisted", detail)
        self.assertIn("recovery routing UNKNOWN", detail)
        self.assertIn("injection", resumeturn._peek("codex"))
        alert.assert_not_called()

    def test_scan_requires_identity_then_a_later_persistent_read(self):  # noqa: VACUOUS_ASSERTION — both scans return populated rows before the error-absence checks
        text = "GO NOW"
        self.assertTrue(resumeturn._record_injection(
            "codex", SID, "h1", text))
        pane = {"handle": "h1", "title": "codex", "worktree": "",
                "last_output_at": 1}

        class Held(FakeAdapter):
            def __init__(self):
                super().__init__((pane,))

            def read(self, handle, limit=3000, timeout=60):
                return "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                                  "  opus-5 | ~/dev/example/repo"))

        ad = Held()
        with mock.patch.object(resumeturn, "recovery_persist_s",
                               return_value=30), \
                mock.patch.object(composers.time, "time", return_value=100):
            first, err = composers.scan(adapter=ad)
        self.assertIsNone(err)
        self.assertEqual(first[0]["state"], composers.HELM_PENDING)
        self.assertEqual(resumeturn._peek("codex")["injection"]["held_at"], 100)
        with mock.patch.object(resumeturn, "recovery_persist_s",
                               return_value=30), \
                mock.patch.object(composers.time, "time", return_value=131):
            later, err = composers.scan(adapter=ad)
        self.assertIsNone(err)
        self.assertEqual(later[0]["state"], composers.HELM_STRANDED)

    def test_readable_clear_breaks_persistence_before_a_later_match(self):  # noqa: VACUOUS_ASSERTION — pending/clear/pending states positively control the held_at absence
        text = "GO NOW"
        generation = resumeturn._record_injection("codex", SID, "h1", text)
        pane = {"handle": "h1", "title": "codex", "worktree": "",
                "last_output_at": 1}

        class Mutable(FakeAdapter):
            body = text

            def __init__(self):
                super().__init__((pane,))

            def read(self, handle, limit=3000, timeout=60):
                return "\n".join(("─" * 40,
                                  "❯\xa0" + self.body if self.body else "❯",
                                  "─" * 40,
                                  "  opus-5 | ~/dev/example/repo"))

        ad = Mutable()
        with mock.patch.object(resumeturn, "recovery_persist_s",
                               return_value=30), \
                mock.patch.object(composers.time, "time", return_value=100):
            first, _ = composers.scan(adapter=ad)
        self.assertEqual(first[0]["state"], composers.HELM_PENDING)
        ad.body = ""
        with mock.patch.object(composers.time, "time", return_value=110):
            cleared, _ = composers.scan(adapter=ad)
        self.assertEqual(cleared[0]["state"], composers.CLEAR)
        self.assertIsNone(resumeturn._peek("codex")["injection"]["held_at"])
        ad.body = text
        with mock.patch.object(resumeturn, "recovery_persist_s",
                               return_value=30), \
                mock.patch.object(composers.time, "time", return_value=200):
            again, _ = composers.scan(adapter=ad)
        self.assertEqual(again[0]["state"], composers.HELM_PENDING)
        self.assertEqual(resumeturn._peek("codex")["injection"]["held_at"], 200)
        self.assertEqual(resumeturn._peek("codex")["injection"]["generation"],
                         generation)

    def test_only_one_recovery_claim_can_spend_enter_for_a_generation(self):
        text = "GO NOW"
        generation = resumeturn._record_injection("codex", SID, "h1", text)
        resumeturn._observe_injection(
            "codex", "h1", text, generation, now=time.time() - 1)
        injection = resumeturn.recorded_injections()["h1"]
        first = resumeturn._claim_injection_recovery(injection)
        self.assertTrue(first)
        self.assertIsNone(resumeturn._claim_injection_recovery(injection))
        self.assertTrue(resumeturn._release_injection_recovery(injection, first))

    def test_every_recovery_actuator_logs_its_bare_enter_attempt(self):  # noqa: VACUOUS_ASSERTION — DELIVERED and one recorded bare Enter positively control the event assertion
        text = "GO NOW"
        generation = resumeturn._record_injection("codex", SID, "h1", text)
        resumeturn._observe_injection(
            "codex", "h1", text, generation, now=time.time() - 1)
        injection = resumeturn.recorded_injections()["h1"]
        held = "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                           "  opus-5 | ~/dev/example/repo"))

        class Moves(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                return ADVANCED_PANE if self.enter_attempted else held

        ad = Moves()

        def current(_row, _adapter, action, **_kw):
            return action(ad, "h1", "registered pane re-proved"), None

        with mock.patch("helm.autocompact._pane_action", side_effect=current), \
                mock.patch.object(pk, "event") as event:
            state, detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [("h1", "", True)])
        self.assertTrue(any("bare Enter recovery" in str(c)
                            for c in event.call_args_list))

    def test_stale_delivery_callback_cannot_clear_an_identical_successor(self):  # noqa: VACUOUS_ASSERTION — the surviving successor generation is the positive state control
        text = "GO NOW"
        old = resumeturn._record_injection("codex", SID, "h1", text)
        new = resumeturn._record_injection("codex", SID, "h1", text)
        self.assertNotEqual(old, new)
        self.assertFalse(resumeturn._clear_injection("codex", "h1", text, old))
        self.assertEqual(resumeturn._peek("codex")["injection"]["generation"],
                         new)

    def test_changed_registered_handle_refuses_before_recovery_enter(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN detail positively proves the changed-handle refusal before zero sends
        text = "GO NOW"
        generation = resumeturn._record_injection("codex", SID, "h1", text)
        resumeturn._observe_injection(
            "codex", "h1", text, generation, now=time.time() - 1)
        injection = resumeturn.recorded_injections()["h1"]
        ad = FakeAdapter()

        def changed(_row, _adapter, action, **_kw):
            return action(ad, "h2", "re-proved a replacement pane"), None

        with mock.patch("helm.autocompact._pane_action", side_effect=changed):
            state, detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertEqual(ad.sent, [])

    def test_changed_adopted_process_identity_refuses_before_enter(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN birth-change detail positively controls the zero-send assertion
        text = "GO NOW"
        generation = resumeturn._record_injection(
            "codex", SID, "h1", text, adapter="fake", pids=["4242:99"])
        resumeturn._observe_injection(
            "codex", "h1", text, generation, now=time.time() - 1)
        injection = resumeturn.recorded_injections()["h1"]
        ad = FakeAdapter()
        with mock.patch.object(orcaadopt, "authorized_handle",
                               return_value=(None, "process birth changed")):
            state, detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("birth changed", detail)
        self.assertEqual(ad.sent, [])

    def test_human_append_is_never_submitted_by_the_recovery_actuator(self):  # noqa: VACUOUS_ASSERTION — preserved injection plus UNKNOWN positively controls zero Enter
        text = "GO NOW"
        generation = resumeturn._record_injection(
            "codex", SID, "h1", text)
        self.assertTrue(generation)
        self.assertIsNotNone(resumeturn._observe_injection(
            "codex", "h1", text, generation, now=time.time() - 10))

        class HumanDraft(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                return "\n".join(("─" * 40,
                                  "❯\xa0GO NOW and this is my unfinished draft",
                                  "─" * 40,
                                  "  opus-5 | ~/dev/example/repo"))

        ad = HumanDraft()
        state, detail = composers.submit("h1", adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertEqual(ad.sent, [], "a human draft must spend zero Enters")
        self.assertIn("injection", resumeturn._peek("codex"),
                      "refusal preserves evidence for later inspection")

    def test_a_session_that_no_longer_owns_the_pane_is_never_injected(self):
        """The settle wait is a window in which the pane can be replaced, so
        identity is re-proven at SEND time, not at hook time."""
        ad = FakeAdapter()
        self.spawn(session="99999999-9999-9999-9999-999999999999")
        with mock.patch.object(resumeturn, "spawn_child",
                               side_effect=self.record_spawn):
            mode, detail = resumeturn.child("codex", SID, "GO NOW", 0, adapter=ad)
        self.assertNotEqual(mode, "resumed")
        self.assertEqual(ad.sent, [], "a stale session must not reach the pane")
        # the failure must ARM a wake-child naming the seat — loud, and the
        # child (not this process) owns the room row + DM delivery
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 1, self.spawned)
        self.assertEqual(wakes[0]["seat"], "codex")
        self.assertTrue(wakes[0]["reason"], wakes)


class InjectionIdentityOutlivesAuthorizationTest(ResumeTurnBase):
    """task/1988: the TTL bounds the AUTHORISATION; identity has no clock.

    `expires_at` is stamped ABSOLUTELY at record time, and every other clause
    of `_valid_injection` — text, digest, handle, generation — answers a
    provenance question the clock cannot change. Dropping the whole record at
    the TTL threw the provenance out with the permission, so
    `composers.classify` got injection=None and fell through to "unmatched
    text may be a human's unfinished draft" ABOUT HELM'S OWN TEXT. Then
    autocompact refuses on unsent input, submit refuses claiming it cannot
    prove ownership, and resume-turn refuses: three individually-correct
    guards and no exit, on exactly the panes nobody rescued inside the hour.
    """

    TEXT = "Run helm seat resume-turn --show"
    HANDLE = "h-ttl"

    def _record(self, expires_at, held_at=1):
        from helm import pk, resumeturn
        # `held_at` is what injection_persistent reads, and classify checks
        # persistence BEFORE the expiry branch — without it every arm here
        # stops at HELM_PENDING and never reaches what it means to test.
        # `generation` is only required NON-EMPTY by _valid_injection; it is
        # NOT matched against an expected value on this path, so this fixture
        # supplies a placeholder and the arms claim nothing about matching.
        inj = {"text": self.TEXT, "handle": self.HANDLE, "generation": "g1",
               "digest": resumeturn._injection_digest(self.TEXT),
               "held_at": held_at, "expires_at": expires_at,
               "session": SID, "adapter": "fake", "recorded_at": 0}
        path = resumeturn.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, {"k1": {"injection": inj}})
        return inj

    def test_expiry_drops_the_permission_and_keeps_the_identity(self):
        from helm import resumeturn
        self._record(expires_at=1000)

        # POSITIVE CONTROL, unconditional: before the stamp the record is
        # readable by BOTH doors and is not marked expired.
        live = resumeturn.recorded_injections(now=999)
        self.assertIn(self.HANDLE, live)
        self.assertFalse(live[self.HANDLE]["expired"])
        self.assertEqual(live[self.HANDLE]["text"], self.TEXT)

        # MUST-MISS: the default door is UNCHANGED — the permission still
        # expires, which is the half that must not widen.
        self.assertNotIn(self.HANDLE, resumeturn.recorded_injections(now=1001))

        # ...and the identity door still answers, marked.
        aged = resumeturn.recorded_injections(now=1001, include_expired=True)
        self.assertIn(self.HANDLE, aged)
        self.assertTrue(aged[self.HANDLE]["expired"])
        self.assertEqual(aged[self.HANDLE]["text"], self.TEXT,
                         "identity must survive its own authorization")

    def test_an_expired_injection_is_helm_stranded_not_a_human_draft(self):
        from helm import composers, harness, seat_lifecycle
        inj = dict(self._record(expires_at=1000), expired=True)
        tail = "> %s" % self.TEXT
        # `last_output_at`, not lastOutputAt — classify reads the _pane_row
        # mapping, and the camelCase spelling silently made every pane blind.
        pane = {"handle": self.HANDLE, "last_output_at": 1}

        # `_current_prompt_line` is the LOCATOR that runs before composer_body;
        # without it classify returns CANNOT_TELL and never reaches the branch
        # under test. Patching the module attribute works because classify
        # does `from .seat_lifecycle import ...` at CALL time.
        with mock.patch.object(seat_lifecycle, "_current_prompt_line",
                               return_value=tail), \
             mock.patch.object(harness, "composer_exactly_holds",
                               return_value=True), \
             mock.patch.object(harness, "composer_body",
                               return_value=self.TEXT), \
             mock.patch.object(harness, "composer_is_placeholder",
                               return_value=False):
            # POSITIVE CONTROL FIRST: an UNEXPIRED record classifies as
            # HELM_STRANDED and says it is eligible, so a difference below is
            # the expiry being read and not the fixture failing to match.
            fresh = composers.classify(
                pane, tail, injection=dict(inj, expired=False), now=999)
            self.assertEqual(fresh[0], composers.HELM_STRANDED, fresh[2])
            self.assertIn("eligible", fresh[2])

            state, _body, why = composers.classify(
                pane, tail, injection=inj, now=1001)

        self.assertEqual(state, composers.HELM_STRANDED,
                         "an expired record still proves the text is Helm's: %s"
                         % why)
        self.assertNotEqual(state, composers.HELD)
        # THE CENSUS STILL REPORTS THE HORIZON -- that half is a cure
        # obligation, not an accident: an operator must be able to see that a
        # record is stale even though staleness no longer forbids recovery.
        self.assertIn("FRESHNESS HORIZON", why)
        self.assertIn("identity is proven", why)
        # ...AND IT NO LONGER CLAIMS A REFUSAL THAT DOES NOT HAPPEN. The
        # sentence this replaced said recovery "is refused on the lapsed
        # authorization", which contradicts the contract submit now honours.
        self.assertNotIn("refused", why)
        self.assertIn("re-reads the pane at the moment of action", why)

    # The wording these two arms police is the OUTPUT, not a comment. The
    # previous round documented the truth in this fixture — `generation` is
    # required only NON-EMPTY on this path — and left both operator-facing
    # sentences still claiming it MATCHED. A comment cannot be read by an
    # operator, so the claim survived its own correction.
    #
    # `_valid_injection` as `recorded_injections` calls it (handle=None,
    # text=None, generation=None) checks: text non-empty, generation
    # NON-EMPTY, and digest == sha256(text). It compares generation to
    # nothing. Uniqueness-per-handle is enforced separately by
    # recorded_injections dropping ambiguous handles.
    GENERATION_MATCH_CLAIM = re.compile(
        r"generation[^.;]{0,40}?\bmatch|\bmatch\w*\b[^.;]{0,40}?generation",
        re.I)

    # MUST-HIT CONTROLS: the exact sentences that shipped, so a detector that
    # silently stopped detecting fails here instead of passing the two
    # assertions below vacuously.
    OVERCLAIMED = (
        "identity is proven (digest, handle and generation all match), so",
        "is provably Helm's (digest, handle and generation match), but its",
        "the recorded generation matches the live one",
    )

    def _adapter(self, tail, error=None):
        """Synthetic I/O; claim, exact-read, retry and verification stay real."""
        class _Ad(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.tail, self.error = tail, error
                self.reads = 0
                self.advance = True
                self.on_read = None

            def read(self, handle, limit=200, timeout=60):
                self.reads += 1
                if self.error is not None:
                    raise harness.HarnessError(self.error)
                result = ADVANCED_PANE if self.enter_attempted and self.advance \
                    else self.tail
                if self.on_read is not None:
                    self.on_read()
                return result

        return _Ad()

    def _held(self, text=None):
        return "\n".join(("─" * 40, "❯\xa0" + (text or self.TEXT), "─" * 40,
                          "  opus-5 | ~/dev/example/repo"))

    def _drive(self, ad, now=1001, direct=False, at_action=None,
               on_attempt=None):
        """Public submit or common owner helper, NOT the child entrypoint."""
        clock = now if callable(now) else lambda: now

        def current(row, adapter, action, **kw):
            self.assertEqual(row, {"seat": "k1", "registered_session": SID})
            self.assertIs(adapter, ad)
            self.assertTrue(kw["for_send"])
            if at_action is not None:
                at_action()
            return action(ad, self.HANDLE, "registered pane re-proved"), None

        with mock.patch.object(resumeturn.time, "time", side_effect=clock), \
                mock.patch("helm.autocompact._pane_action", side_effect=current), \
                mock.patch.object(harness, "SUBMIT_VERIFY_READS", 1), \
                mock.patch.object(harness, "SUBMIT_VERIFY_INTERVAL_S", 0):
            if direct:
                row = resumeturn._peek("k1")["injection"]
                injection = resumeturn._matching_injection(
                    "k1", self.HANDLE, row["text"], row["generation"])
                self.assertIsNotNone(injection, "common recovery must reach its gate")
                return resumeturn.recover_injection(
                    injection, adapter=ad, on_attempt=on_attempt)
            self.assertIsNone(on_attempt, "public submit has no callback parameter")
            return composers.submit(self.HANDLE, adapter=ad, now=clock())

    def _scan(self, ad, now=1001):
        ad.panes = [{"handle": self.HANDLE, "title": "k1", "last_output_at": 1}]
        clock = now if callable(now) else lambda: now
        with mock.patch.object(composers.time, "time", side_effect=clock):
            rows, err = composers.scan(adapter=ad)
        self.assertIsNone(err)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["handle"], self.HANDLE)
        return rows[0]

    def _wire_record(self):
        # The directive really precedes the injection, as in the child path.
        with mock.patch.object(resumeturn, "injection_ttl_s", return_value=100):
            with mock.patch.object(resumeturn.time, "time", return_value=1000):
                wire = resumeturn._wire_text("k1", SID, "the real long brief " * 8)
            with mock.patch.object(resumeturn.time, "time", return_value=1020):
                generation = resumeturn._record_injection(
                    "k1", SID, self.HANDLE, wire, adapter="fake")
        self.assertTrue(generation)
        self.assertTrue(resumeturn._observe_injection(
            "k1", self.HANDLE, wire, generation, now=1021))
        available, token = resumeturn.indirection_payload_available(wire, now=1050)
        self.assertTrue(available)
        self.assertIsNotNone(token, "fixture must be emitted as a real wire pointer")
        return wire, token

    def test_submit_RECOVERS_an_expired_record_whose_composer_still_holds_it(self):
        self._record(expires_at=1000)
        aged = resumeturn.recorded_injections(now=1001, include_expired=True)
        self.assertTrue(aged[self.HANDLE]["expired"])
        ad = self._adapter(self._held())
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])
        self.assertNotIn("injection", resumeturn._peek("k1"))

    def test_submit_REFUSES_when_the_live_composer_has_moved_on(self):
        self._record(expires_at=1000)
        ad = self._adapter(self._held(self.TEXT + " plus my draft"))
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("does not exactly equal", detail)
        self.assertEqual(ad.reads, 1)
        self.assertEqual(ad.sent, [])
        self.assertIsNone(resumeturn._peek("k1")["injection"]["held_at"])

    def test_submit_refuses_when_the_pane_CANNOT_BE_READ(self):
        self._record(expires_at=1000)
        ad = self._adapter(None, error="pane gone")
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("read failed before Enter", detail)
        self.assertEqual(ad.reads, 1)
        self.assertEqual(ad.sent, [])
        self.assertEqual(resumeturn._peek("k1")["injection"]["held_at"], 1)
        self.assertNotIn("recovery", resumeturn._peek("k1")["injection"])
        ad.error, ad.tail = None, self._held()
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

    def test_submit_refuses_when_the_composer_is_not_VISIBLE_enough(self):
        self._record(expires_at=1000)
        ad = self._adapter(self._held("[Pasted text #1 +3 lines]"))
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("could not be identified exactly", detail)
        self.assertEqual(ad.reads, 1)
        self.assertEqual(ad.sent, [])
        self.assertEqual(resumeturn._peek("k1")["injection"]["held_at"], 1)
        self.assertNotIn("recovery", resumeturn._peek("k1")["injection"])

    def test_the_CLAIM_door_also_stops_gating_on_the_clock(self):
        """THE SECOND DOOR ON THE SAME CLOCK, and the one a mocked actuator
        cannot see. submit reaching recover_injection proves nothing if
        _claim_injection_recovery then refuses the same record for the same
        reason -- the cure would be inert in production with every arm above
        still green. So this drives the real claim function.
        """
        from helm import resumeturn
        inj = self._record(expires_at=1000)
        record = dict(inj, key="k1")

        with mock.patch.object(resumeturn, "_recovery_task_delivered",
                               return_value=(False, None)):
            token = resumeturn._claim_injection_recovery(record)
        self.assertTrue(token, "an expired but proven record must still be "
                               "claimable; the clock is not evidence about "
                               "ownership")

        # MUST-DIFFER on the same door, ON A FIXTURE THAT HAS NEVER BEEN
        # CLAIMED. Reusing the record above would let the SURVIVING RECOVERY
        # TOKEN produce the refusal, so a weakened provenance predicate would
        # still look green -- the negative would never reach the clause it
        # claims to test. The state is rewritten from scratch first.
        self._record(expires_at=1000)
        bad = dict(record, text=self.TEXT + "!")
        with mock.patch.object(resumeturn, "_recovery_task_delivered",
                               return_value=(False, None)):
            self.assertIsNone(resumeturn._claim_injection_recovery(bad),
                              "a text/digest mismatch must still refuse")
        # ...and the SAME freshly-written state still admits the good record,
        # which proves the refusal above came from the provenance clause and
        # not from a fixture that refuses everything.
        with mock.patch.object(resumeturn, "_recovery_task_delivered",
                               return_value=(False, None)):
            self.assertTrue(resumeturn._claim_injection_recovery(record),
                            "the control fixture must still admit the good "
                            "record, or the negative proves nothing")

    def test_a_witnessed_edit_through_SUBMIT_breaks_persistence(self):
        self._record(expires_at=1000)
        ad = self._adapter(self._held(self.TEXT + " plus my draft"))
        with mock.patch.object(resumeturn, "recovery_persist_s", return_value=30):
            state, detail = self._drive(ad)
            self.assertEqual(state, harness.UNKNOWN, detail)
            self.assertIsNone(resumeturn._peek("k1")["injection"]["held_at"])
            self.assertNotIn("recovery", resumeturn._peek("k1")["injection"])
            ad.tail = self._held()
            reads = ad.reads
            state, detail = self._drive(ad)
            self.assertEqual(state, harness.UNKNOWN)
            self.assertIn("persistence interval", detail)
            self.assertEqual(ad.reads, reads)
            self.assertEqual(ad.sent, [])
            self.assertTrue(resumeturn._observe_injection(
                "k1", self.HANDLE, self.TEXT, "g1", now=1002))
            state, _ = self._drive(ad, now=1002)
            self.assertEqual(state, harness.UNKNOWN)
            self.assertEqual(ad.sent, [])
            state, detail = self._drive(ad, now=1033)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

    def test_an_UNREADABLE_pane_does_not_cost_a_mature_observation(self):
        self._record(expires_at=1000)
        ad = self._adapter(None, error="pane gone")
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        after = resumeturn._peek("k1")["injection"]
        self.assertEqual(after["held_at"], 1)
        self.assertNotIn("recovery", after)
        self.assertEqual(ad.reads, 1)
        self.assertEqual(ad.sent, [])

    def test_the_stale_lifecycle_is_coherent_end_to_end(self):
        """OBSERVE, RESET, CLAIM AND RELEASE ALL SEE THE SAME EXPIRED RECORD.

        Admitting a stale record to CLAIMS while the other three owners still
        refused it produced three separate wedges: a never-observed record
        could not mature, a witnessed edit could not reset it, and -- worst --
        a claim could never be RELEASED after a proven non-delivery, so its
        surviving token blocked every later claim on that generation forever.
        """
        from helm import resumeturn
        inj = self._record(expires_at=1000)
        record = dict(inj, key="k1")
        args = ("k1", self.HANDLE, self.TEXT, "g1")

        # OBSERVE: a stale record can still acquire its first observation.
        resumeturn._reset_injection_observation(*args)
        self.assertTrue(resumeturn._observe_injection(*args, now=1001),
                        "a stale record must be able to mature")
        held = resumeturn.recorded_injections(
            now=1001, include_expired=True)[self.HANDLE]
        self.assertEqual(held.get("held_at"), 1001)

        # RESET: a witnessed edit can still break that observation.
        self.assertTrue(resumeturn._reset_injection_observation(*args))
        cleared = resumeturn.recorded_injections(
            now=1001, include_expired=True)[self.HANDLE]
        self.assertIsNone(cleared.get("held_at"))

        # CLAIM then RELEASE: the token must be returnable, or one failed
        # recovery strands the generation permanently.
        resumeturn._observe_injection(*args, now=1)
        with mock.patch.object(resumeturn, "_recovery_task_delivered",
                               return_value=(False, None)):
            token = resumeturn._claim_injection_recovery(record)
        self.assertTrue(token)
        self.assertTrue(resumeturn._release_injection_recovery(record, token),
                        "a stale claim must be releasable after a proven "
                        "non-delivery, or its token blocks every later claim")

        # ...and the release actually freed it: a SECOND claim succeeds.
        with mock.patch.object(resumeturn, "_recovery_task_delivered",
                               return_value=(False, None)):
            self.assertTrue(resumeturn._claim_injection_recovery(record),
                            "the released generation must be claimable again")

    def test_a_retrieval_prompt_whose_directive_is_gone_REFUSES(self):
        wire, _ = self._wire_record()
        ad = self._adapter(self._held(wire))
        state, detail = self._drive(ad, now=1050)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

        wire, token = self._wire_record()
        ad = self._adapter(self._held(wire))
        state, detail = self._drive(ad, now=1121)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("RETRIEVAL PROMPT", detail)
        self.assertIn(token, detail)
        self.assertIn("no longer available", detail)
        self.assertEqual(ad.reads, 1, "the common action must see the real composer")
        self.assertEqual(ad.sent, [])
        self.assertNotIn("recovery", resumeturn._peek("k1")["injection"])

    def test_failed_edit_reset_keeps_the_claim_after_storage_recovers(self):
        self._record(expires_at=1000)
        ad = self._adapter(self._held(self.TEXT + " plus my draft"))
        witnessed, failed = [], []

        def observed():
            # A failed claim must never reach this read. The safety evidence
            # already exists durably when the edited composer is observed.
            claim = resumeturn._peek("k1")["injection"].get("recovery")
            self.assertTrue(claim)
            witnessed.append(claim["token"])

        ad.on_read = observed
        write = pk.write_json

        def fail_reset(path, data):
            if path == resumeturn.state_path() and \
                    data["k1"]["injection"].get("held_at") is None:
                failed.append(True)
                raise OSError("reset storage temporarily unavailable")
            return write(path, data)

        with mock.patch.object(pk, "write_json", side_effect=fail_reset):
            state, detail = self._drive(ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("persistence reset failed; recovery authority retained", detail)
        self.assertEqual(failed, [True], "the failed write must be the reset")
        surviving = resumeturn._peek("k1")["injection"]
        self.assertEqual(surviving["held_at"], 1)
        self.assertEqual(witnessed, [surviving["recovery"]["token"]])
        self.assertEqual(ad.sent, [])

        # Storage now works and the human restores the original bytes. The
        # consumed claim, not just an error sentence, prevents immediate reuse.
        ad.tail = self._held()
        reads = ad.reads
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("already recovering", detail)
        self.assertEqual(ad.reads, reads)
        self.assertEqual(ad.sent, [])
        self.assertEqual(resumeturn._peek("k1")["injection"]["recovery"],
                         surviving["recovery"])

    def test_scan_failed_edit_finalization_keeps_preread_reservation(self):
        write = pk.write_json
        for expiry in (900, 5000):
            for held_at in (None, 990, 1):
                for tail in (self._held(self.TEXT + " human edit"), ADVANCED_PANE):
                    with self.subTest(expiry=expiry, held_at=held_at, tail=tail):
                        self._record(expiry, held_at=held_at)
                        clock, witnessed, failed = [1001], [], []
                        ad = self._adapter(tail)

                        def after_read():
                            witnessed.append(resumeturn._peek("k1")["injection"]["recovery"])
                            clock[0] = 1031  # pending observation matures DURING read

                        def fail_finalization(path, value, *args, **kw):
                            inj = value.get("k1", {}).get("injection", {})
                            if path == resumeturn.state_path() and "recovery" not in inj:
                                self.assertEqual(ad.reads, 1)
                                self.assertEqual(len(witnessed), 1)
                                self.assertIsNone(inj["held_at"])
                                self.assertEqual(resumeturn._peek("k1")["injection"]["recovery"],
                                                 witnessed[0])
                                failed.append(True)
                                raise OSError("observation finalization storage unavailable")
                            return write(path, value, *args, **kw)

                        ad.on_read = after_read
                        with mock.patch.object(pk, "write_json", side_effect=fail_finalization):
                            row = self._scan(ad, now=lambda: clock[0])
                        self.assertEqual(row["state"], composers.CANNOT_TELL)
                        self.assertIn("finalization failed", row["why"])
                        self.assertEqual(failed, [True])
                        surviving = resumeturn._peek("k1")["injection"]
                        self.assertEqual(surviving["held_at"], held_at)
                        self.assertEqual(surviving["recovery"], witnessed[0])
                        ad.tail, ad.on_read = self._held(), None
                        with mock.patch.object(resumeturn, "recovery_persist_s", return_value=30):
                            state, detail = self._drive(ad, now=1032)
                            later = self._scan(ad, now=1032)
                        self.assertEqual(state, harness.UNKNOWN, detail)
                        self.assertEqual(later["state"], composers.CANNOT_TELL)
                        self.assertEqual(ad.reads, 1)
                        self.assertEqual(ad.sent, [])
                        self.assertEqual(resumeturn._peek("k1")["injection"], surviving)

    def test_scan_first_observation_uses_read_time_and_does_not_renew(self):
        for expiry in (900, 5000):
            with self.subTest(expiry=expiry):
                self._record(expiry, held_at=None)
                clock = [1000]
                ad = self._adapter(self._held())
                with mock.patch.object(resumeturn, "recovery_persist_s", return_value=30):
                    state, detail = self._drive(ad, now=1000)
                    self.assertEqual(state, harness.UNKNOWN, detail)
                    self.assertEqual(ad.reads, 0)

                    def after_read():
                        self.assertTrue(resumeturn._peek("k1")["injection"]["recovery"])
                        clock[0] = 1020

                    ad.on_read = after_read
                    first = self._scan(ad, now=lambda: clock[0])
                    self.assertEqual(first["state"], composers.HELM_PENDING)
                    first_record = resumeturn._peek("k1")["injection"]
                    self.assertEqual(first_record["held_at"], 1020)
                    self.assertNotIn("recovery", first_record)
                    ad.on_read = None
                    state, detail = self._drive(ad, now=1021)
                    self.assertEqual(state, harness.UNKNOWN, detail)
                    self.assertEqual(ad.reads, 1)
                    later = self._scan(ad, now=1040)
                    self.assertEqual(later["state"], composers.HELM_PENDING)
                    self.assertEqual(resumeturn._peek("k1")["injection"]["held_at"], 1020)
                    state, detail = self._drive(ad, now=1051)
                self.assertEqual(state, harness.DELIVERED, detail)
                self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

    def test_scan_successful_clear_or_edit_resets_without_clearing_provenance(self):
        for tail, expected in ((ADVANCED_PANE, composers.CLEAR),
                               (self._held(self.TEXT + " edit"), composers.HELD)):
            with self.subTest(expected=expected):
                original = self._record(900)
                ad = self._adapter(tail)
                with mock.patch.object(resumeturn, "_clear_injection") as clear, \
                        mock.patch.object(resumeturn, "_close_recovery_task") as close:
                    row = self._scan(ad)
                self.assertEqual(row["state"], expected)
                clear.assert_not_called()
                close.assert_not_called()
                remaining = resumeturn._peek("k1")["injection"]
                self.assertEqual(remaining["generation"], original["generation"])
                self.assertEqual(remaining["text"], original["text"])
                self.assertIsNone(remaining["held_at"])
                self.assertNotIn("recovery", remaining)
                ad.tail = self._held()
                state, detail = self._drive(ad, now=1002)
                self.assertEqual(state, harness.UNKNOWN, detail)
                self.assertEqual(ad.reads, 1)
                self.assertEqual(ad.sent, [])

    def test_scan_reservation_write_failure_precedes_read_and_can_retry(self):
        self._record(900)
        ad = self._adapter(self._held())
        write, failed = pk.write_json, []

        def fail_reservation(path, value, *args, **kw):
            if path == resumeturn.state_path() and value["k1"]["injection"].get("recovery"):
                failed.append(True)
                raise OSError("reservation storage unavailable")
            return write(path, value, *args, **kw)

        with mock.patch.object(pk, "write_json", side_effect=fail_reservation):
            row = self._scan(ad)
        self.assertEqual(row["state"], composers.CANNOT_TELL)
        self.assertIn("reservation unavailable", row["why"])
        self.assertEqual(failed, [True])
        self.assertEqual(ad.reads, 0)
        self.assertNotIn("recovery", resumeturn._peek("k1")["injection"])
        row = self._scan(ad)
        self.assertEqual(row["state"], composers.HELM_STRANDED)
        self.assertEqual(ad.reads, 1)
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

    def test_scan_exact_and_unreadable_release_without_renewing_timestamp(self):
        for held_at in (None, 990, 1):
            for error in (None, "unreadable", "opaque"):
                with self.subTest(held_at=held_at, error=error):
                    self._record(900, held_at=held_at)
                    ad, reserved = self._adapter(self._held()), []
                    # Unlocatable bytes and read errors both mean unknown,
                    # never a witnessed clear or permission to reset the clock.
                    if error == "unreadable":
                        ad.error = error
                    elif error == "opaque":
                        ad.tail = "unlocatable frame"
                    ad.on_read = lambda: reserved.append(
                        resumeturn._peek("k1")["injection"]["recovery"])
                    with mock.patch.object(resumeturn, "recovery_persist_s", return_value=30):
                        row = self._scan(ad)
                    self.assertEqual(ad.reads, 1)
                    remaining = resumeturn._peek("k1")["injection"]
                    self.assertNotIn("recovery", remaining)
                    self.assertEqual(remaining["held_at"],
                                     1001 if held_at is None and not error else held_at)
                    self.assertEqual(ad.sent, [])
                    if error:
                        self.assertEqual(row["state"], composers.CANNOT_TELL)
                    else:
                        self.assertEqual(len(reserved), 1)
                        self.assertTrue(reserved[0])
                        self.assertEqual(row["state"], composers.HELM_STRANDED
                                         if held_at == 1 else composers.HELM_PENDING)

    def test_scan_nested_observer_and_submit_cannot_steal_reservation(self):
        for held_at in (None, 990, 1):
            with self.subTest(held_at=held_at):
                self._record(900, held_at=held_at)
                ad, claims, clock = self._adapter(self._held()), [], [1001]

                def nested():
                    ad.on_read = None  # a broken guard fails finitely, not recursively
                    clock[0] = 1031
                    claim = resumeturn._peek("k1")["injection"]["recovery"]
                    claims.append(claim)
                    row = self._scan(ad, now=1031)
                    self.assertEqual(row["state"], composers.CANNOT_TELL)
                    state, detail = self._drive(ad, now=1031)
                    self.assertEqual(state, harness.UNKNOWN, detail)
                    self.assertEqual(ad.reads, 1)
                    self.assertEqual(ad.sent, [])
                    self.assertEqual(resumeturn._peek("k1")["injection"]["recovery"], claim)

                ad.on_read = nested
                with mock.patch.object(resumeturn, "recovery_persist_s", return_value=30):
                    row = self._scan(ad, now=lambda: clock[0])
                self.assertEqual(len(claims), 1)
                self.assertEqual(ad.reads, 1)
                self.assertNotEqual(row["state"], composers.CANNOT_TELL)
                remaining = resumeturn._peek("k1")["injection"]
                self.assertNotIn("recovery", remaining)
                self.assertEqual(remaining["held_at"], 1031 if held_at is None else held_at)

    def test_observation_reservation_does_not_relax_recovery_maturity(self):
        for expiry in (900, 5000):
            for held_at in (None, 990, 1):
                with self.subTest(expiry=expiry, held_at=held_at):
                    self._record(expiry, held_at=held_at)
                    with mock.patch.object(resumeturn, "recovery_persist_s", return_value=30), \
                            mock.patch.object(resumeturn.time, "time", return_value=1001):
                        injection = resumeturn.recorded_injections(
                            include_expired=True)[self.HANDLE]
                        recovery = resumeturn._claim_injection_recovery(injection)
                        if held_at == 1:
                            self.assertTrue(recovery)
                            self.assertTrue(resumeturn._release_injection_recovery(
                                injection, recovery))
                        else:
                            self.assertIsNone(recovery)
                        observation = resumeturn._claim_injection_observation(injection)
                        self.assertTrue(observation)
                        observed = resumeturn._finish_injection_observation(
                            injection, observation, None, 1001)
                        self.assertIsInstance(observed, dict)
                        canonical = resumeturn._peek("k1")["injection"]
                        self.assertEqual(observed, dict(canonical, key="k1", expired=expiry < 1001))
                        self.assertEqual(observed["held_at"], held_at)
                        self.assertNotIn("recovery", observed)

    def test_scan_classifies_committed_record_after_prereservation_interleaving(self):
        reserve = resumeturn._claim_injection_observation
        for reset in (True, False):
            with self.subTest(reset=reset):
                # Both initial snapshots are fresh. The outer read happens
                # after expiry, and must report the committed observation age.
                self._record(1002, held_at=1 if reset else None)
                clock, raced, peer_rows = [1001], [], []
                ad = self._adapter(self._held())
                peer = self._adapter(self._held(self.TEXT + " edit") if reset else self._held())

                def competing_observer(injection):
                    if not raced:
                        self.assertEqual(injection["held_at"], 1 if reset else None)
                        self.assertFalse(injection["expired"])
                        raced.append(True)
                        clock[0] = 1002
                        peer_rows.append(self._scan(peer, now=lambda: clock[0]))
                        # The edited text is restored before the outer read;
                        # conversely a peer's first exact observation can mature
                        # while the original snapshot still says unobserved.
                        clock[0] = 1003 if reset else 1033
                    return reserve(injection)

                with mock.patch.object(resumeturn, "recovery_persist_s", return_value=30), \
                        mock.patch.object(resumeturn, "_claim_injection_observation",
                                          side_effect=competing_observer), \
                        mock.patch.object(composers, "classify", wraps=composers.classify) as classify:
                    row = self._scan(ad, now=lambda: clock[0])
                self.assertEqual(raced, [True])
                self.assertEqual(peer_rows[0]["state"], composers.HELD if reset else composers.HELM_PENDING)
                self.assertEqual((ad.reads, peer.reads), (1, 1))
                canonical = resumeturn._peek("k1")["injection"]
                observed = classify.call_args[1]["injection"]
                self.assertIsInstance(observed, dict)
                self.assertEqual(observed, dict(canonical, key="k1", expired=True))
                self.assertEqual(classify.call_args[1]["now"], clock[0])
                self.assertEqual(observed["held_at"], 1003 if reset else 1002)
                self.assertNotIn("recovery", observed)
                self.assertEqual(row["state"], composers.HELM_PENDING if reset else composers.HELM_STRANDED)
                if not reset:
                    self.assertIn("FRESHNESS HORIZON", row["why"])
                with mock.patch.object(resumeturn, "recovery_persist_s", return_value=30):
                    state, detail = self._drive(ad, now=clock[0])
                    if reset:
                        self.assertEqual(state, harness.UNKNOWN, detail)
                        self.assertIn("has not remained held", detail)
                        self.assertEqual(ad.reads, 1)
                        self.assertEqual(ad.sent, [])
                        state, detail = self._drive(ad, now=1034)
                    self.assertEqual(state, harness.DELIVERED, detail)
                    self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

    def test_scan_uses_finalizer_snapshot_not_a_postrelease_reread(self):
        self._record(5000, held_at=None)
        ad, committed = self._adapter(self._held()), []
        finish = resumeturn._finish_injection_observation

        def finalize_then_compete(injection, token, exact, observed_at):
            observed = finish(injection, token, exact, observed_at)
            self.assertIsInstance(observed, dict)
            self.assertEqual(observed["held_at"], 1001)
            committed.append(dict(observed))
            # A later observer changes canonical persistence after release.
            # Census must present its own transaction, not mix that later
            # state with the earlier pane frame. Submission still revalidates.
            self.assertTrue(resumeturn._reset_injection_observation(
                "k1", self.HANDLE, self.TEXT, observed["generation"]))
            self.assertTrue(resumeturn._observe_injection(
                "k1", self.HANDLE, self.TEXT, observed["generation"], now=900))
            return observed

        with mock.patch.object(resumeturn, "recovery_persist_s", return_value=30), \
                mock.patch.object(resumeturn, "_finish_injection_observation",
                                  side_effect=finalize_then_compete), \
                mock.patch.object(composers, "classify", wraps=composers.classify) as classify:
            row = self._scan(ad)
        self.assertEqual(len(committed), 1)
        self.assertEqual(row["state"], composers.HELM_PENDING)
        self.assertEqual(classify.call_args[1]["injection"], committed[0])
        self.assertEqual(resumeturn._peek("k1")["injection"]["held_at"], 900)
        with mock.patch.object(resumeturn, "recovery_persist_s", return_value=30):
            state, detail = self._drive(ad)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

    def test_scan_existing_or_terminal_authority_refuses_without_read(self):
        self._record(5000)
        ad = self._adapter(self._held())
        self.assertEqual(self._scan(ad)["state"], composers.HELM_STRANDED)
        self.assertEqual(ad.reads, 1)
        injection = resumeturn.recorded_injections(now=1001)[self.HANDLE]
        with mock.patch.object(resumeturn.time, "time", return_value=1001):
            token = resumeturn._claim_injection_recovery(injection)
        self.assertTrue(token)
        reserved = resumeturn._peek("k1")["injection"]
        row = self._scan(ad)
        self.assertEqual(row["state"], composers.CANNOT_TELL)
        self.assertEqual(ad.reads, 1)
        self.assertIsNone(resumeturn._finish_injection_observation(
            injection, "not-the-owner", False, 1002))
        self.assertEqual(resumeturn._peek("k1")["injection"], reserved)
        self.assertTrue(resumeturn._release_injection_recovery(injection, token))
        terminal, why = resumeturn._close_recovery_task(
            self.HANDLE, injection["generation"], ensure_terminal=True)
        self.assertTrue(terminal, why)
        self.assertEqual(resumeturn._recovery_task_delivered(
            self.HANDLE, injection["generation"]), (True, None))
        before = resumeturn._peek("k1")["injection"]
        row = self._scan(ad)
        self.assertEqual(row["state"], composers.CANNOT_TELL)
        self.assertEqual(ad.reads, 1)
        self.assertEqual(ad.sent, [])
        self.assertEqual(resumeturn._peek("k1")["injection"], before)

    def test_scan_stale_finalizer_cannot_change_successor_generation_or_token(self):
        self._record(5000)
        old = resumeturn.recorded_injections(now=1001)[self.HANDLE]
        ad, old_tokens, successors = self._adapter(self._held(self.TEXT + " edit")), [], []

        def replace_during_read():
            ad.on_read = None
            old_tokens.append(resumeturn._peek("k1")["injection"]["recovery"]["token"])
            generation = resumeturn._record_injection(
                "k1", SID, self.HANDLE, self.TEXT, adapter="fake")
            self.assertTrue(generation)
            self.assertNotEqual(generation, old["generation"])
            self.assertTrue(resumeturn._observe_injection(
                "k1", self.HANDLE, self.TEXT, generation, now=800))
            new = resumeturn.recorded_injections()[self.HANDLE]
            token = resumeturn._claim_injection_observation(new)
            self.assertTrue(token)
            successors.append((new, token, resumeturn._peek("k1")["injection"]))

        ad.on_read = replace_during_read
        row = self._scan(ad)
        self.assertEqual(row["state"], composers.CANNOT_TELL)
        self.assertEqual(len(old_tokens), 1)
        self.assertEqual(len(successors), 1)
        new, token, reserved = successors[0]
        self.assertEqual(resumeturn._peek("k1")["injection"], reserved)
        self.assertIsNone(resumeturn._finish_injection_observation(
            old, old_tokens[0], False, 1002))
        self.assertEqual(resumeturn._peek("k1")["injection"], reserved)
        ad.tail = self._held()
        state, detail = self._drive(ad, now=1002)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertEqual(ad.reads, 1)
        self.assertEqual(ad.sent, [])
        observed = resumeturn._finish_injection_observation(new, token, True, 1002)
        self.assertIsInstance(observed, dict)
        self.assertEqual(observed["generation"], new["generation"])
        self.assertEqual(observed["held_at"], 800)
        self.assertNotIn("recovery", observed)
        self.assertEqual(resumeturn._peek("k1")["injection"]["held_at"], 800)
        state, detail = self._drive(ad, now=1002)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

    def test_scan_reconciles_edit_before_fallible_classification(self):
        self._record(900)
        ad = self._adapter(self._held(self.TEXT + " edit"))
        with mock.patch.object(composers, "classify", side_effect=ValueError("formatting failed")):
            with self.assertRaisesRegex(ValueError, "formatting failed"):
                self._scan(ad)
        self.assertEqual(ad.reads, 1)
        remaining = resumeturn._peek("k1")["injection"]
        self.assertIsNone(remaining["held_at"])
        self.assertNotIn("recovery", remaining)
        ad.tail = self._held()
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertEqual(ad.reads, 1)
        self.assertEqual(ad.sent, [])

    def test_scan_unassociated_or_ambiguous_panes_remain_read_only(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                if ambiguous:
                    injection = self._record(900)
                    pk.write_json(resumeturn.state_path(), {
                        "k1": {"injection": injection},
                        "k2": {"injection": dict(injection, generation="g2")}})
                else:
                    os.makedirs(os.path.dirname(resumeturn.state_path()), exist_ok=True)
                    pk.write_json(resumeturn.state_path(), {})
                before = pk.read_json(resumeturn.state_path(), {})
                ad = self._adapter(self._held())
                with mock.patch.object(pk, "write_json") as write:
                    row = self._scan(ad)
                self.assertEqual(row["state"], composers.HELD)
                self.assertEqual(ad.reads, 1)
                self.assertEqual(ad.sent, [])
                write.assert_not_called()
                self.assertEqual(pk.read_json(resumeturn.state_path(), {}), before)

    def test_failed_claim_persistence_refuses_before_any_composer_read(self):
        self._record(expires_at=1000)
        ad = self._adapter(self._held(self.TEXT + " plus my draft"))
        with mock.patch.object(pk, "write_json",
                               side_effect=OSError("claim storage unavailable")):
            state, detail = self._drive(ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertEqual(ad.reads, 0)
        self.assertEqual(ad.sent, [])
        self.assertNotIn("recovery", resumeturn._peek("k1")["injection"])
        # No edit was witnessed without durable authority. A healthy, exact
        # call on the same record is the positive control for that refusal.
        ad.tail = self._held()
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

    def test_payload_is_rechecked_after_identity_wait_public_and_common(self):
        wire, _ = self._wire_record()
        good = self._adapter(self._held(wire))
        state, detail = self._drive(good, now=1050, direct=True)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(good.sent, [(self.HANDLE, "", True)])

        for direct in (False, True):
            for change in ("expire", "remove"):
                with self.subTest(direct=direct, change=change):
                    wire, token = self._wire_record()
                    clock = [1050]
                    ad = self._adapter(self._held(wire))
                    # _wire_record already proved the target available. The
                    # routing/lock wait ends only after that premise changes.
                    def at_action():
                        if change == "expire":
                            clock[0] = 1101
                        else:
                            def remove(entry):
                                entry.pop("directive")
                                return None, True
                            resumeturn._mutate_entry("k1", remove)

                    state, detail = self._drive(
                        ad, now=lambda: clock[0], direct=direct,
                        at_action=at_action)
                    self.assertEqual(state, harness.UNKNOWN)
                    self.assertIn("RETRIEVAL PROMPT", detail)
                    self.assertIn(token, detail)
                    self.assertEqual(ad.reads, 1)
                    self.assertEqual(ad.sent, [])
                    self.assertNotIn("recovery", resumeturn._peek("k1")["injection"])

    def test_payload_expiry_inside_final_store_read_withholds_enter(self):
        read = pk.read_json
        for direct in (False, True):
            for expire_at in (None, 1, 2):
                with self.subTest(direct=direct, expire_at=expire_at):
                    wire, token = self._wire_record()
                    clock, armed, gate_reads, claims = [1050], [False], [], []
                    ad = self._adapter(self._held(wire))
                    ad.advance = expire_at is None

                    def preparation(_event, _key, detail):
                        self.assertIn("preparing bare Enter recovery", detail)
                        self.assertFalse(armed[0])
                        armed[0] = True

                    def delayed_store_read(path, *args, **kw):
                        value = read(path, *args, **kw)
                        if path == resumeturn.state_path() and armed[0]:
                            # Preparation has finished. This is the directive
                            # store read INSIDE final admission, not an earlier
                            # identity/claim/readback operation.
                            armed[0] = False
                            gate_reads.append(clock[0])
                            self.assertEqual(value["k1"]["directive"]["token"], token)
                            self.assertEqual(value["k1"]["directive"]["expires_at"], 1100)
                            self.assertEqual(clock[0], 1050)
                            if len(gate_reads) == expire_at:
                                claims.append(value["k1"]["injection"]["recovery"])
                                clock[0] = 1101
                        return value

                    with mock.patch.object(pk, "event", side_effect=preparation), \
                            mock.patch.object(pk, "read_json", side_effect=delayed_store_read):
                        state, detail = self._drive(ad, now=lambda: clock[0], direct=direct)
                    self.assertFalse(armed[0])
                    self.assertEqual(len(gate_reads), expire_at or 1)
                    if expire_at is None:
                        self.assertEqual(state, harness.DELIVERED, detail)
                        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])
                        self.assertNotIn("injection", resumeturn._peek("k1"))
                        continue
                    self.assertEqual(state, harness.UNKNOWN, detail)
                    self.assertIn("RETRIEVAL PROMPT", detail)
                    self.assertIn(token, detail)
                    self.assertEqual(ad.reads, 2 * expire_at - 1)
                    self.assertEqual(ad.sent, [(self.HANDLE, "", True)] * (expire_at - 1))
                    self.assertEqual(len(claims), 1)
                    remaining = resumeturn._peek("k1")["injection"]
                    if expire_at == 1:
                        self.assertNotIn("recovery", remaining)
                    else:
                        self.assertEqual(remaining["recovery"], claims[0])
                        state, detail = self._drive(ad, now=1102, direct=direct)
                        self.assertEqual(state, harness.UNKNOWN, detail)
                        self.assertIn("already recovering", detail)
                        self.assertEqual(ad.reads, 3)
                        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

    def test_show_directive_preserves_explicit_now_after_slow_store_read(self):
        wire, token = self._wire_record()
        read, reads = pk.read_json, []
        clock = [1050]
        expected = resumeturn.show_directive(token, now=1050)
        self.assertTrue(expected)

        def delayed_store_read(path, *args, **kw):
            value = read(path, *args, **kw)
            if path == resumeturn.state_path():
                reads.append(True)
                clock[0] = 1101
            return value

        with mock.patch.object(resumeturn.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(pk, "read_json", side_effect=delayed_store_read):
            self.assertIsNone(resumeturn.show_directive(token))
            self.assertEqual(resumeturn.show_directive(token, now=1050), expected)
            self.assertEqual(resumeturn.show_directive(token, now=1100), expected)
            self.assertIsNone(resumeturn.show_directive(token, now=1101))
            self.assertEqual(resumeturn.indirection_payload_available(wire, now=1100), (True, token))
            self.assertEqual(resumeturn.indirection_payload_available(wire), (False, token))
        self.assertEqual(len(reads), 6)
        self.assertEqual(resumeturn._peek("k1")["directive"]["expires_at"], 1100)

    def test_final_payload_gate_follows_journal_and_outer_callback(self):
        wire, _ = self._wire_record()
        good = self._adapter(self._held(wire))
        prepared = []
        state, detail = self._drive(
            good, now=1050, direct=True, on_attempt=prepared.append)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(prepared, [1])
        self.assertEqual(good.sent, [(self.HANDLE, "", True)])

        for source in ("journal", "outer"):
            for change in ("expire", "remove"):
                for at in (1, 2):
                    with self.subTest(source=source, change=change, at=at):
                        wire, token = self._wire_record()
                        clock, prepared, journaled, claims = [1050], [], [], []
                        ad = self._adapter(self._held(wire))
                        ad.advance = False

                        def change_target(n):
                            if n != at:
                                return
                            self.assertTrue(resumeturn.indirection_payload_available(
                                wire, now=clock[0])[0])
                            claims.append(resumeturn._peek("k1")["injection"]["recovery"])
                            if change == "expire":
                                clock[0] = 1101
                            else:
                                def remove(entry):
                                    entry.pop("directive")
                                    return None, True
                                resumeturn._mutate_entry("k1", remove)

                        def journal(_event, key, detail):
                            self.assertEqual(key, "k1")
                            self.assertIn("preparing bare Enter recovery", detail)
                            journaled.append(detail)
                            if source == "journal":
                                change_target(len(journaled))

                        def outer(n):
                            prepared.append(n)
                            if source == "outer":
                                change_target(n)

                        with mock.patch.object(pk, "event", side_effect=journal):
                            state, detail = self._drive(
                                ad, now=lambda: clock[0], direct=True,
                                on_attempt=outer)
                        self.assertEqual(state, harness.UNKNOWN, detail)
                        self.assertIn("RETRIEVAL PROMPT", detail)
                        self.assertIn(token, detail)
                        self.assertEqual(prepared, list(range(1, at + 1)))
                        self.assertEqual(len(journaled), at)
                        self.assertEqual(len(claims), 1)
                        self.assertEqual(ad.reads, 2 * at - 1)
                        self.assertEqual(ad.sent, [(self.HANDLE, "", True)] * (at - 1))
                        remaining = resumeturn._peek("k1")["injection"]
                        self.assertEqual(remaining["text"], wire)
                        if at == 1:
                            self.assertNotIn("recovery", remaining,
                                             "preparation must not count a withheld Enter")
                        else:
                            self.assertEqual(remaining["recovery"], claims[0])
                            state, detail = self._drive(
                                ad, now=lambda: clock[0], direct=True)
                            self.assertEqual(state, harness.UNKNOWN)
                            self.assertIn("already recovering", detail)
                            self.assertEqual(ad.reads, 3)
                            self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

    def test_child_checks_payload_after_preparation_without_extra_recovery_enter(self):
        # Actual child -> real wire/deliver/submit/observe/match/recover. Only
        # pane I/O, identity resolution, clock, journal and task routing are fake.
        for change in (None, "expire", "remove"):
            with self.subTest(change=change):
                clock, action_results, prepared = [1000], [], []

                class InitiallyHeld(FakeAdapter):
                    def __init__(self):
                        super().__init__()
                        self.reads = 0

                    def send(self, handle, text, enter=True):
                        super().send(handle, text, enter=enter)
                        if not enter:
                            clock[0] = 1020  # directive predates injection

                    def read(self, handle, limit=200, timeout=60):
                        self.reads += 1
                        enters = sum(enter for _, _, enter in self.sent)
                        if not self.typed or enters >= 2:
                            return ADVANCED_PANE
                        return "\n".join(("─" * 40, "❯\xa0" + self.typed,
                                          "─" * 40, "  opus-5 | ~/dev/example/repo"))

                ad = InitiallyHeld()

                def current(row, adapter, action, **kw):
                    self.assertEqual(row, {"seat": "codex", "registered_session": SID})
                    self.assertIs(adapter, ad)
                    self.assertTrue(kw["for_send"])
                    result = action(ad, "h1", "synthetic registered pane")
                    action_results.append(result)
                    return result, None

                def wait(seconds):
                    self.assertEqual(seconds, 30)
                    self.assertEqual(action_results[0][0], "manual")
                    clock[0] += seconds

                def journal(_event, _key, detail):
                    if "preparing bare Enter recovery" not in detail:
                        return
                    inj = resumeturn._peek("codex")["injection"]
                    prepared.append(inj)
                    self.assertTrue(resumeturn.injection_persistent(inj))
                    self.assertEqual(inj["held_at"], 1020)
                    self.assertEqual(inj["expires_at"], 1120)
                    self.assertEqual(inj["digest"], resumeturn._injection_digest(ad.typed))
                    self.assertTrue(inj["generation"])
                    self.assertTrue(inj["recovery"])
                    self.assertEqual(inj["session"], SID)
                    self.assertTrue(resumeturn.indirection_payload_available(ad.typed)[0])
                    if change == "expire":
                        clock[0] = 1101
                    elif change == "remove":
                        def remove(entry):
                            entry.pop("directive")
                            return None, True
                        resumeturn._mutate_entry("codex", remove)

                with mock.patch.object(resumeturn.time, "time", side_effect=lambda: clock[0]), \
                        mock.patch.object(resumeturn.time, "sleep", side_effect=wait) as slept, \
                        mock.patch.object(resumeturn, "injection_ttl_s", return_value=100), \
                        mock.patch.object(resumeturn, "recovery_persist_s", return_value=30), \
                        mock.patch.object(harness, "SUBMIT_VERIFY_READS", 1), \
                        mock.patch.object(harness, "SUBMIT_VERIFY_INTERVAL_S", 0), \
                        mock.patch("helm.autocompact._pane_action", side_effect=current), \
                        mock.patch.object(pk, "event", side_effect=journal), \
                        mock.patch.object(resumeturn, "_route_recovery",
                                          return_value=(None, "synthetic route")) as route, \
                        mock.patch.object(resumeturn, "_alert") as alert:
                    mode, detail = resumeturn.child(
                        "codex", SID, "the actual child's long directive " * 8,
                        0, adapter=ad)
                self.assertEqual(len(action_results), 2)
                self.assertEqual(action_results[0][0], "manual")
                self.assertIn("STILL holds", action_results[0][1])
                slept.assert_called_once_with(30)
                self.assertEqual(len(prepared), 1)
                self.assertTrue(ad.typed.startswith("Run `helm seat resume-turn --show "))
                self.assertEqual(ad.sent[:2], [("h1", ad.typed, False), ("h1", "", True)])
                alert.assert_not_called()
                if change is None:
                    self.assertEqual(mode, "resumed", detail)
                    self.assertEqual(ad.sent[2:], [("h1", "", True)])
                    self.assertEqual(ad.reads, 5)
                    self.assertNotIn("injection", resumeturn._peek("codex"))
                    route.assert_not_called()
                else:
                    self.assertEqual(mode, "unverified", detail)
                    self.assertIn("RETRIEVAL PROMPT", detail)
                    self.assertEqual(ad.sent[2:], [])
                    self.assertEqual(ad.reads, 4)
                    remaining = resumeturn._peek("codex")["injection"]
                    self.assertEqual(remaining["generation"], prepared[0]["generation"])
                    self.assertEqual(remaining["text"], ad.typed)
                    self.assertEqual(remaining["held_at"], 1020)
                    self.assertNotIn("recovery", remaining)
                    route.assert_called_once()
                    self.assertEqual(route.call_args[0][0], "codex")
                    self.assertEqual(route.call_args[0][1], {
                        "handle": "h1", "generation": remaining["generation"]})
                    self.assertEqual(resumeturn._peek("codex")["mode"], "unverified")

    def test_payload_expiry_before_retry_retains_the_already_used_claim(self):
        wire, token = self._wire_record()
        clock = [1050]
        ad = self._adapter(self._held(wire))
        ad.advance = False

        def after_read():
            # First exact read permits Enter. Its verification proves held,
            # but the directive expires before the retry's admission check.
            if ad.reads == 2:
                clock[0] = 1101

        ad.on_read = after_read
        state, detail = self._drive(ad, now=lambda: clock[0], direct=True)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("RETRIEVAL PROMPT", detail)
        self.assertIn(token, detail)
        self.assertEqual(ad.reads, 3)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])
        claim = resumeturn._peek("k1")["injection"]["recovery"]
        self.assertTrue(claim)
        state, detail = self._drive(ad, now=1102, direct=True)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("already recovering", detail)
        self.assertEqual(ad.reads, 3)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])
        self.assertEqual(resumeturn._peek("k1")["injection"]["recovery"], claim)

    def test_unreadable_retry_does_not_release_an_already_used_claim(self):
        self._record(expires_at=1000)
        ad = self._adapter(self._held())
        ad.advance = False

        def after_read():
            if ad.reads == 2:
                ad.error = "retry frame unreadable"

        ad.on_read = after_read
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("read failed before Enter", detail)
        self.assertEqual(ad.reads, 3)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])
        claim = resumeturn._peek("k1")["injection"]["recovery"]
        self.assertTrue(claim)
        ad.error, ad.advance, ad.enter_attempted = None, True, False
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("already recovering", detail)
        self.assertEqual(ad.reads, 3)
        self.assertEqual(ad.sent, [(self.HANDLE, "", True)])

    def test_the_recovery_event_carries_the_records_AGE(self):
        """An audit must be able to see that a recovery acted on a
        stale-but-proven record. An event naming only the attempt cannot show
        that afterwards.
        """
        from helm import harness, pk, resumeturn

        class _Ad(object):
            name = "orca"

            def retry_held_submission(_s, handle, text, attempts=1,
                                      backoff=0, on_attempt=None,
                                      on_observation=None, before_attempt=None,
                                      prepare_attempt=None):
                if on_observation is not None:
                    on_observation(True)
                if prepare_attempt is not None:
                    prepare_attempt(1)
                if before_attempt is not None:
                    refusal = before_attempt()
                    if refusal is not None:
                        return harness.UNKNOWN, refusal
                if on_attempt is not None:
                    on_attempt(1)
                return harness.DELIVERED, "typed"

        ad = _Ad()

        def drive(_row, _adapter, action, **_kw):
            return action(ad, self.HANDLE, "re-proved"), None

        def run(expired):
            inj = {"key": "k1", "session": "s1", "handle": self.HANDLE,
                   "text": self.TEXT, "generation": "g1", "expired": expired,
                   "adapter": "", "pids": [],
                   "digest": resumeturn._injection_digest(self.TEXT),
                   "held_at": 1, "recorded_at": time.time() - 7200}
            said = []
            with mock.patch.object(resumeturn, "_claim_injection_recovery",
                                   return_value="tok"), \
                 mock.patch.object(resumeturn, "_clear_injection",
                                   return_value=True), \
                 mock.patch("helm.autocompact._pane_action",
                            side_effect=drive), \
                 mock.patch.object(pk, "event",
                                   side_effect=lambda *a: said.append(a[2])):
                state, detail = resumeturn.recover_injection(inj, adapter=ad)
            self.assertEqual(state, harness.DELIVERED, detail)
            self.assertTrue(said, "no recovery event was emitted at all")
            return " ".join(said)

        aged = run(True)
        self.assertIn("record age", aged)
        self.assertIn("past its freshness horizon", aged)
        # MUST-DIFFER on the same emitter: a FRESH record's event still
        # carries the age and does NOT claim the horizon was passed, so the
        # assertion above is about EXPIRY and not about a string that is
        # always present.
        fresh = run(False)
        self.assertIn("record age", fresh)
        self.assertNotIn("past its freshness horizon", fresh)

    def test_the_detector_fires_on_the_wording_that_actually_shipped(self):
        # UNCONDITIONAL POSITIVE FIRST, outside the loop: an emptied
        # OVERCLAIMED tuple would make the loop below iterate zero times
        # and pass, which is the same vacuity this arm exists to prevent
        # elsewhere.
        self.assertTrue(self.GENERATION_MATCH_CLAIM.search(
            "digest, handle and generation all match"))
        self.assertEqual(len(self.OVERCLAIMED), 3)
        for sentence in self.OVERCLAIMED:
            self.assertTrue(self.GENERATION_MATCH_CLAIM.search(sentence),
                            "detector missed a real over-claim: %r" % sentence)
        # ...and does not fire on a sentence that merely mentions both words
        # far apart, or matches something OTHER than the generation.
        # The observable here is a LITERAL sentence, so there is no bound
        # name for the guard to see covered. The cover is real and it is
        # the two assertions above, which run unconditionally and fail
        # before this line: this arm IS the positive control that keeps
        # the two output arms below from passing vacuously.
        self.assertFalse(self.GENERATION_MATCH_CLAIM.search(  # noqa: VACUOUS_ASSERTION — literal observable; covered by the unconditional assertTrue and length assertion above
            "the live composer exactly matches the recorded text, and the "
            "record separately carries a nonempty generation value"),
            "a match claim about the TEXT is not a claim about the "
            "generation; a detector that cannot tell them apart would "
            "make the two arms below unsatisfiable rather than true")

    def test_the_stranded_reason_claims_no_generation_match(self):
        from helm import composers, harness, seat_lifecycle
        inj = dict(self._record(expires_at=1000), expired=True)
        tail = "> %s" % self.TEXT
        pane = {"handle": self.HANDLE, "last_output_at": 1}
        with mock.patch.object(seat_lifecycle, "_current_prompt_line",
                               return_value=tail), \
             mock.patch.object(harness, "composer_exactly_holds",
                               return_value=True), \
             mock.patch.object(harness, "composer_body",
                               return_value=self.TEXT), \
             mock.patch.object(harness, "composer_is_placeholder",
                               return_value=False):
            _state, _body, why = composers.classify(
                pane, tail, injection=inj, now=1001)
        self.assertIsNone(self.GENERATION_MATCH_CLAIM.search(why),
                          "classify claims a generation match it never made: %s"
                          % why)
        # POSITIVE HALF — it must still say what it DID check, or the negative
        # above is satisfiable by saying nothing at all.
        self.assertIn("carries a generation", why)
        self.assertIn("digest validates", why)
        self.assertIn("only VALID record under this handle", why)

    def test_the_submit_refusal_claims_no_generation_match(self):
        """A canonical generation match is not a live-pane generation proof."""
        self._record(expires_at=1000)
        ad = self._adapter(self._held(self.TEXT + " plus my draft"))
        state, detail = self._drive(ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIsNone(self.GENERATION_MATCH_CLAIM.search(detail), detail)
        self.assertIn("does not exactly equal", detail)
        self.assertEqual(ad.reads, 1)

    def test_an_invalid_sibling_record_does_not_make_the_handle_ambiguous(self):
        """Why the sentences say only VALID record and not only record.

        `recorded_injections` filters through `_valid_injection` and only THEN
        drops handles claimed twice, so a second state entry under the same
        handle that fails the provenance check is invisible to the ambiguity
        rule. "the only record under this handle" is therefore false of exactly
        this arrangement while "the only VALID record" stays true, and the
        difference is not cosmetic: the operator is being told what was
        checked.
        """
        from helm import pk, resumeturn
        good = {"text": self.TEXT, "handle": self.HANDLE, "generation": "g1",
                "digest": resumeturn._injection_digest(self.TEXT),
                "held_at": 1, "expires_at": 1000}
        # SAME HANDLE, BROKEN DIGEST — the one clause `_valid_injection` checks
        # that no other field can stand in for.
        bad = dict(good, text="a different composer entirely")
        path = resumeturn.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, {"k1": {"injection": good},
                             "k2": {"injection": bad}})

        aged = resumeturn.recorded_injections(now=1001, include_expired=True)
        # THE POINT: two records name this handle and it is NOT ambiguous.
        self.assertIn(self.HANDLE, aged,
                      "an invalid sibling must not suppress a valid record")
        self.assertEqual(aged[self.HANDLE]["text"], self.TEXT)
        self.assertEqual(aged[self.HANDLE]["key"], "k1")

        # MUST-HIT CONTROL: make the sibling VALID and the handle DOES go
        # ambiguous — without this, the assertion above would also pass if the
        # ambiguity rule had simply stopped working.
        pk.write_json(path, {"k1": {"injection": good},
                             "k2": {"injection": dict(good)}})
        self.assertNotIn(
            self.HANDLE,
            resumeturn.recorded_injections(now=1001, include_expired=True),
            "two VALID records under one handle must still be ambiguous")

    def test_the_census_prints_the_rows_reason_not_a_fixed_submit_hint(self):
        """The census recommended a command that must refuse.

        Every HELM_STRANDED row printed the same fixed line — "persistent
        recorded injection; --submit may recover it" — including a row whose
        injection authorization has EXPIRED, where `submit` REFUSES by design.
        So the operator was sent to run something the census already knew
        would not work, and the `why` that said so was dropped on the floor.
        classify writes the correct sentence per case; the census carries it.

        This drives the REAL CLI print path and reads its stdout.
        """
        import contextlib
        import io
        from helm import composers

        def render(why):
            rows = [{"handle": "h-x", "title": "t",
                     "state": composers.HELM_STRANDED,
                     "composer": "body", "why": why}]
            buf = io.StringIO()
            with mock.patch.object(composers, "scan",
                                   return_value=(rows, None)), \
                    contextlib.redirect_stdout(buf):
                composers.cmd_composers([])
            return buf.getvalue()

        # POSITIVE CONTROL, unconditional: a LIVE stranded row still tells the
        # operator it is recoverable, so the refusal below is the reason being
        # carried and not the census having gone silent.
        live = render("composer exactly matches a persistent Helm injection "
                      "and is eligible for guarded bare-Enter recovery")
        self.assertIn("eligible for guarded bare-Enter recovery", live)

        expired = render("composer exactly matches a persistent Helm "
                         "injection whose injection authorization has EXPIRED "
                         "- recovery is refused on the lapsed authorization")
        self.assertIn("EXPIRED", expired)
        self.assertIn("refused on the lapsed authorization", expired)
        # the fixed hint that contradicted the row must be gone entirely
        self.assertNotIn("--submit may recover it", expired)
        self.assertNotIn("--submit may recover it", live)



class ComposerVisibilityTest(unittest.TestCase):
    def test_never_observed_is_classified_from_the_TEXT_scrolled_away_is_not(self):
        """ONE OF THESE TWO UNKNOWNS WAS NOT AN UNKNOWN. This arm used to
        assert that a pane with `last_output_at: None` classifies CANNOT_TELL
        "never observed output" — a shrug driven by METADATA while the pane's
        actual text sat in the caller's hand, unread.

        Measured (task/1906): that metadata is stale after a reboot and says
        nothing about readability; a handle the census called never-observed
        read back a real draft. So when the text IS available it decides, and
        never-observed stops being an unknown at all.

        THE SECOND CASE IS THE REAL ONE AND IS UNCHANGED: newer output sitting
        BELOW the last prompt means the composer scrolled away, the text cannot
        be located in the tail, and CANNOT_TELL is the honest answer. Keeping
        it here is the point — this arm still proves the two cases are
        DISTINCT, it has just stopped calling both of them unknown.
        """
        # Never observed, and the text says the composer is clear.
        state, _body, _why = composers.classify(
            {"last_output_at": None}, ADVANCED_PANE)
        self.assertEqual(state, composers.CLEAR)

        # Never observed, and the SAME reader says HELD when the text holds a
        # draft — so the line above is the text deciding, not a constant.
        state, _body, _why = composers.classify(
            {"last_output_at": None}, HELD_PANE)
        self.assertEqual(state, composers.HELD)

        # Scrolled away: still a genuine unknown, and still says why.
        state, _body, why = composers.classify(
            {"last_output_at": 1}, "❯ old submitted\nnew semantic output")
        self.assertEqual(state, composers.CANNOT_TELL)
        self.assertIn("newer output sits below the last prompt", why)


class RecoveryRoutingTest(ResumeTurnBase):
    INJECTION = {"handle": "h1", "generation": "generation-1"}

    def test_live_armed_non_owner_gets_the_persisted_task(self):
        with mock.patch("helm.seats_work_offer._live_seats",
                        return_value={"codex", "captain", "peer-a"}), \
                mock.patch.object(seats, "owner_names",
                                  return_value={"captain"}), \
                mock.patch.object(seats, "roster_checked",
                                  return_value=({"codex": {}, "captain": {},
                                                "Peer-A": {}}, False)), \
                mock.patch.object(seats, "beacon_procs",
                                  return_value=([22], None)) as beacon, \
                mock.patch.object(seats, "dm", return_value=({"id": "m"}, None)) \
                as dm:
            row, err = resumeturn._route_recovery(
                "codex", self.INJECTION, "stuck")
        self.assertIsNone(err)
        self.assertEqual(row["owner"], "Peer-A")
        self.assertEqual(row["status"], "in_progress")
        beacon.assert_called_once_with("Peer-A", strict=True)
        self.assertEqual(dm.call_args.args[0], "Peer-A")

    def test_quiet_checked_roster_peer_is_the_fallback(self):  # noqa: VACUOUS_ASSERTION — the persisted owned row is the positive fallback control
        with mock.patch("helm.seats_work_offer._live_seats", return_value=set()), \
                mock.patch.object(seats, "owner_names", return_value={"captain"}), \
                mock.patch.object(seats, "roster_checked",
                                  return_value=({"quiet": {}}, False)), \
                mock.patch("helm.dispatches._default_lander",
                           return_value="missing-lander"), \
                mock.patch("helm.beacons.roll",
                           return_value=["graveyard", "quiet"]), \
                mock.patch.object(seats, "dm", return_value=({"id": "m"}, None)):
            row, err = resumeturn._route_recovery(
                "codex", self.INJECTION, "stuck")
        self.assertIsNone(err)
        self.assertEqual((row["owner"], row["status"]),
                         ("quiet", "in_progress"))

    def test_no_non_owner_candidate_persists_an_offerable_unowned_task(self):  # noqa: VACUOUS_ASSERTION — the persisted open unowned row positively controls no DM
        with mock.patch.object(resumeturn, "_recovery_owner",
                               return_value=(None, None)), \
                mock.patch.object(seats, "dm") as dm:
            row, err = resumeturn._route_recovery(
                "codex", self.INJECTION, "stuck")
        self.assertIsNone(err)
        self.assertEqual((row["owner"], row["status"]), (None, "open"))
        dm.assert_not_called()

    def test_repeated_route_reuses_the_exact_task_identity(self):  # noqa: VACUOUS_ASSERTION — equal returned ids and the one-row ledger positively control deduplication
        with mock.patch.object(resumeturn, "_recovery_owner",
                               return_value=(None, None)):
            first, err = resumeturn._route_recovery(
                "codex", self.INJECTION, "stuck")
            self.assertIsNone(err)
            second, err = resumeturn._route_recovery(
                "codex", self.INJECTION, "stuck again")
        self.assertIsNone(err)
        self.assertEqual(first["id"], second["id"])
        rows, unavailable = tasks.snapshot(strict=True)
        self.assertIsNone(unavailable)
        self.assertEqual(list(rows), [first["id"]])

    def test_task_write_refusal_stays_explicit_and_never_dms(self):  # noqa: VACUOUS_ASSERTION — the explicit write error positively controls the absent row and DM
        with mock.patch.object(resumeturn, "_recovery_owner",
                               return_value=("peer-a", None)), \
                mock.patch.object(tasks, "add",
                                  return_value=(None, "ledger refused write")), \
                mock.patch.object(seats, "dm") as dm:
            row, err = resumeturn._route_recovery(
                "codex", self.INJECTION, "stuck")
        self.assertIsNone(row)
        self.assertIn("recovery task write failed", err)
        dm.assert_not_called()

    def test_candidate_probe_exception_files_unowned_and_never_owner_alerts(self):  # noqa: VACUOUS_ASSERTION — the persisted degraded-route task positively controls no owner alert
        class UnreadableAfterEnter(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                if self.enter_attempted:
                    raise harness.HarnessError("composer tail scrolled away")
                return super().read(handle, limit=limit, timeout=timeout)

        ad = UnreadableAfterEnter()
        with mock.patch.object(seats, "owner_names", return_value={"captain"}), \
                mock.patch("helm.seats_work_offer._live_seats",
                           side_effect=RuntimeError("presence store broke")), \
                mock.patch.object(seats, "roster_checked",
                                  side_effect=RuntimeError("roster broke")), \
                mock.patch.object(resumeturn, "_alert") as alert:
            mode, detail = resumeturn.child(
                "codex", SID, "GO NOW", 0, adapter=ad)
        self.assertEqual(mode, "unverified", detail)
        self.assertIn("recovery task task/resume-turn-", detail)
        self.assertIn("live-seat probe failed", detail)
        self.assertIn("checked roster failed", detail)
        rows, unavailable = tasks.snapshot(strict=True)
        self.assertIsNone(unavailable)
        row = next(iter(rows.values()))
        self.assertEqual((row["owner"], row["status"]), (None, "open"))
        alert.assert_not_called()

    def test_failed_peer_dm_keeps_task_and_never_falls_to_owner_alert(self):  # noqa: VACUOUS_ASSERTION — the owned persisted task and exact send pair positively control no owner fallback
        class UnreadableAfterEnter(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                if self.enter_attempted:
                    raise harness.HarnessError("composer tail scrolled away")
                return super().read(handle, limit=limit, timeout=timeout)

        ad = UnreadableAfterEnter()
        with mock.patch.object(resumeturn, "_recovery_owner",
                               return_value=("peer-a", None)), \
                mock.patch.object(seats, "dm",
                                  return_value=(None, "peer route offline")), \
                mock.patch.object(resumeturn, "_alert") as alert:
            mode, detail = resumeturn.child(
                "codex", SID, "GO NOW", 0, adapter=ad)
        self.assertEqual(mode, "unverified", detail)
        self.assertIn("recovery task task/resume-turn-", detail)
        row = next(iter(tasks.rows().values()))
        self.assertEqual((row["owner"], row["status"]),
                         ("peer-a", "in_progress"))
        self.assertEqual(ad.sent,
                         [("h1", "GO NOW", False), ("h1", "", True)])
        alert.assert_not_called()

    def test_task_instructions_establish_proof_then_delivery_closes_it(self):  # noqa: VACUOUS_ASSERTION — pending→stranded→delivered states and the closed row positively control the workflow
        text = "GO NOW"
        generation = resumeturn._record_injection("codex", SID, "h1", text)
        injection = resumeturn.recorded_injections()["h1"]
        self.assertIsNone(injection["held_at"])
        with mock.patch.object(resumeturn, "_recovery_owner",
                               return_value=(None, None)), \
                mock.patch.object(resumeturn, "recovery_persist_s",
                                  return_value=30):
            task, err = resumeturn._route_recovery(
                "codex", injection, "composer unreadable")
        self.assertIsNone(err)
        self.assertLess(task["note"].index("helm seat composers`"),
                        task["note"].index("wait at least 30.0s"))
        self.assertLess(task["note"].index("wait at least 30.0s"),
                        task["note"].index("scan again"))
        self.assertLess(task["note"].index("scan again"),
                        task["note"].index("--submit h1"))

        held = "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                           "  opus-5 | ~/dev/example/repo"))

        class HeldThenMoves(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                return ADVANCED_PANE if self.enter_attempted else held

        ad = HeldThenMoves()
        pane = {"handle": "h1", "title": "codex", "worktree": "",
                "last_output_at": 1}
        ad.panes = [pane]
        with mock.patch.object(resumeturn, "recovery_persist_s",
                               return_value=30), \
                mock.patch.object(composers.time, "time", return_value=100):
            first, _ = composers.scan(adapter=ad)
        self.assertEqual({r["handle"]: r for r in first}["h1"]["state"],
                         composers.HELM_PENDING)
        with mock.patch.object(resumeturn, "recovery_persist_s",
                               return_value=30), \
                mock.patch.object(composers.time, "time", return_value=131):
            later, _ = composers.scan(adapter=ad)
            state, detail = composers.submit("h1", adapter=ad, now=131)
        self.assertEqual({r["handle"]: r for r in later}["h1"]["state"],
                         composers.HELM_STRANDED)
        self.assertEqual(state, harness.DELIVERED, detail)
        closed = tasks.get(task["id"])
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["closed_reason"],
                         "exact recorded injection delivered; composer advanced")

    def test_task_close_failure_never_preserves_enter_authority(self):  # noqa: VACUOUS_ASSERTION — delivered state, absent provenance, and open task positively control zero future Enter
        text = "GO NOW"
        generation = resumeturn._record_injection("codex", SID, "h1", text)
        resumeturn._observe_injection(
            "codex", "h1", text, generation, now=time.time() - 1)
        injection = resumeturn.recorded_injections()["h1"]
        with mock.patch.object(resumeturn, "_recovery_owner",
                               return_value=(None, None)):
            task, err = resumeturn._route_recovery(
                "codex", injection, "stuck")
        self.assertIsNone(err)
        held = "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                           "  opus-5 | ~/dev/example/repo"))

        class Moves(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                return ADVANCED_PANE if self.enter_attempted else held

        ad = Moves()
        def current(_row, _adapter, action, **_kw):
            return action(ad, "h1", "registered pane re-proved"), None
        with mock.patch("helm.autocompact._pane_action", side_effect=current), \
                mock.patch.object(tasks, "update",
                                  return_value=(None, "ledger unwritable")):
            state, detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertNotIn("injection", resumeturn._peek("codex"))
        self.assertIn(tasks.get(task["id"])["status"], tasks.OPEN_STATUSES)
        spent = list(ad.sent)
        state, _detail = composers.submit("h1", adapter=ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, spent,
                         "a task-close failure must not restore Enter authority")

    def test_clear_failure_closes_task_and_blocks_a_second_enter(self):  # noqa: VACUOUS_ASSERTION — surviving provenance and closed task positively control the second terminal authority
        text = "GO NOW"
        generation = resumeturn._record_injection("codex", SID, "h1", text)
        resumeturn._observe_injection(
            "codex", "h1", text, generation, now=time.time() - 1)
        injection = resumeturn.recorded_injections()["h1"]
        with mock.patch.object(resumeturn, "_recovery_owner",
                               return_value=(None, None)):
            task, err = resumeturn._route_recovery(
                "codex", injection, "stuck")
        self.assertIsNone(err)
        held = "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                           "  opus-5 | ~/dev/example/repo"))

        class Moves(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                return ADVANCED_PANE if self.enter_attempted else held

        ad = Moves()
        def current(_row, _adapter, action, **_kw):
            return action(ad, "h1", "registered pane re-proved"), None
        mutate = resumeturn._mutate_entry
        calls = {"n": 0}
        def fail_clear(key, change, nonblocking=False):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("state store unwritable")
            return mutate(key, change, nonblocking=nonblocking)
        with mock.patch("helm.autocompact._pane_action", side_effect=current), \
                mock.patch.object(resumeturn, "_mutate_entry",
                                  side_effect=fail_clear):
            state, detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertIn("injection", resumeturn._peek("codex"),
                      "the fixture must prove the clear really failed")
        self.assertEqual(tasks.get(task["id"])["status"], "closed")
        spent = list(ad.sent)
        state, _detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, spent,
                         "the delivered task is the second terminal authority")

    def test_dual_store_failure_consumes_recovery_authority_forever(self):  # noqa: VACUOUS_ASSERTION — surviving token, open task, and first send positively control zero second Enter
        text = "GO NOW"
        generation = resumeturn._record_injection("codex", SID, "h1", text)
        resumeturn._observe_injection(
            "codex", "h1", text, generation, now=time.time() - 1)
        injection = resumeturn.recorded_injections()["h1"]
        with mock.patch.object(resumeturn, "_recovery_owner",
                               return_value=(None, None)):
            task, err = resumeturn._route_recovery(
                "codex", injection, "stuck")
        self.assertIsNone(err)
        held = "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                           "  opus-5 | ~/dev/example/repo"))

        class Moves(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                return ADVANCED_PANE if self.enter_attempted else held

        ad = Moves()
        def current(_row, _adapter, action, **_kw):
            return action(ad, "h1", "registered pane re-proved"), None
        mutate = resumeturn._mutate_entry
        calls = {"n": 0}
        def fail_clear(key, change, nonblocking=False):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("state store unwritable")
            return mutate(key, change, nonblocking=nonblocking)
        with mock.patch("helm.autocompact._pane_action", side_effect=current), \
                mock.patch.object(resumeturn, "_mutate_entry",
                                  side_effect=fail_clear), \
                mock.patch.object(tasks, "update",
                                  return_value=(None, "task ledger unwritable")):
            state, detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        surviving = resumeturn.recorded_injections()["h1"]
        self.assertTrue(surviving["recovery"],
                        "the pre-Enter claim is the consumed authority")
        self.assertIn(tasks.get(task["id"])["status"], tasks.OPEN_STATUSES)

        ad.enter_attempted = False       # identical text reappears after 120s
        spent = list(ad.sent)
        future = surviving["recorded_at"] + 121
        with mock.patch.object(resumeturn.time, "time", return_value=future), \
                mock.patch("helm.autocompact._pane_action", side_effect=current):
            state, _detail = resumeturn.recover_injection(
                resumeturn.recorded_injections(now=future)["h1"], adapter=ad)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, spent,
                         "a consumed recovery token must never renew Enter")

    def test_human_append_then_restore_needs_fresh_persistence(self):  # noqa: VACUOUS_ASSERTION — append refusal, cleared held_at, and delayed delivery positively control the fresh cycle
        text = "GO NOW"
        generation = resumeturn._record_injection("codex", SID, "h1", text)
        now = time.time()
        resumeturn._observe_injection(
            "codex", "h1", text, generation, now=now - 31)
        injection = resumeturn.recorded_injections()["h1"]
        held = "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                           "  opus-5 | ~/dev/example/repo"))

        class EditedThenRestored(FakeAdapter):
            edited = True

            def read(self, handle, limit=3000, timeout=60):
                if self.enter_attempted:
                    return ADVANCED_PANE
                return held.replace(text, text + " plus my draft") \
                    if self.edited else held

        ad = EditedThenRestored()
        def current(_row, _adapter, action, **_kw):
            return action(ad, "h1", "registered pane re-proved"), None
        with mock.patch.object(resumeturn, "recovery_persist_s",
                               return_value=30), \
                mock.patch("helm.autocompact._pane_action",
                           side_effect=current):
            state, detail = resumeturn.recover_injection(injection, adapter=ad)
            self.assertEqual(state, harness.UNKNOWN, detail)
            self.assertEqual(ad.sent, [], "a human append must spend zero Enters")
            self.assertIsNone(
                resumeturn.recorded_injections()["h1"]["held_at"],
                "a readable mismatch must consume the stale observation")

            ad.edited = False
            state, _detail = resumeturn.recover_injection(injection, adapter=ad)
            self.assertEqual(state, harness.UNKNOWN)
            self.assertEqual(ad.sent, [],
                             "restored text has no fresh persistence yet")

            observed_at = time.time()
            self.assertTrue(resumeturn._observe_injection(
                "codex", "h1", text, generation, now=observed_at))
            state, _detail = resumeturn.recover_injection(
                resumeturn.recorded_injections()["h1"], adapter=ad)
            self.assertEqual(state, harness.UNKNOWN)
            self.assertEqual(ad.sent, [],
                             "one fresh observation is still snapshot-only")

            with mock.patch.object(resumeturn.time, "time",
                                   return_value=observed_at + 31):
                state, detail = resumeturn.recover_injection(
                    resumeturn.recorded_injections(now=observed_at + 31)["h1"],
                    adapter=ad)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [("h1", "", True)],
                         "only the later persistent cycle may spend Enter")

    def test_post_enter_unknown_consumes_recovery_authority_forever(self):  # noqa: VACUOUS_ASSERTION — first bare Enter and surviving claim positively control zero second Enter
        text = "GO NOW"
        generation = resumeturn._record_injection("codex", SID, "h1", text)
        resumeturn._observe_injection(
            "codex", "h1", text, generation, now=time.time() - 1)
        injection = resumeturn.recorded_injections()["h1"]
        held = "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                           "  opus-5 | ~/dev/example/repo"))

        class UnreadableAfterEnter(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                if self.enter_attempted:
                    raise harness.HarnessError("composer tail scrolled away")
                return held

        ad = UnreadableAfterEnter()
        def current(_row, _adapter, action, **_kw):
            return action(ad, "h1", "registered pane re-proved"), None
        with mock.patch("helm.autocompact._pane_action", side_effect=current):
            state, detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertEqual(ad.sent, [("h1", "", True)])
        self.assertTrue(resumeturn.recorded_injections()["h1"]["recovery"],
                        "post-Enter UNKNOWN must retain consumed authority")

        ad.enter_attempted = False
        spent = list(ad.sent)
        with mock.patch("helm.autocompact._pane_action", side_effect=current):
            state, detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("already recovering", detail)
        self.assertEqual(ad.sent, spent,
                         "unreadable verification must never authorize Enter 2")

    def test_unknown_and_not_delivered_leave_the_task_open(self):  # noqa: VACUOUS_ASSERTION — exact UNKNOWN/NOT_DELIVERED states positively control both open tasks
        text = "GO NOW"
        held = "\n".join(("─" * 40, "❯\xa0" + text, "─" * 40,
                           "  opus-5 | ~/dev/example/repo"))
        for name, tail in (("unknown", held.replace(text, text + " edited")),
                           ("not-delivered", held)):
            with self.subTest(state=name):
                generation = resumeturn._record_injection(
                    "codex", SID, "h1", text)
                resumeturn._observe_injection(
                    "codex", "h1", text, generation, now=time.time() - 1)
                injection = resumeturn.recorded_injections()["h1"]
                with mock.patch.object(resumeturn, "_recovery_owner",
                                       return_value=(None, None)):
                    task, err = resumeturn._route_recovery(
                        "codex", injection, name)
                self.assertIsNone(err)

                class Static(FakeAdapter):
                    def read(self, handle, limit=3000, timeout=60):
                        return tail

                ad = Static()
                def current(_row, _adapter, action, **_kw):
                    return action(ad, "h1", "registered pane re-proved"), None
                with mock.patch("helm.autocompact._pane_action",
                                side_effect=current):
                    state, _detail = resumeturn.recover_injection(
                        injection, adapter=ad)
                expected = (harness.UNKNOWN if name == "unknown" else
                            harness.NOT_DELIVERED)
                self.assertEqual(state, expected)
                self.assertIn(tasks.get(task["id"])["status"],
                              tasks.OPEN_STATUSES)


class PaneIdentityTest(ResumeTurnBase):
    def test_an_unidentifiable_pane_alerts_loudly_and_never_guesses(self):
        cases = {
            "no register": lambda: os.remove(os.path.join(self.d, "spawn.json")),
            "headless": lambda: self.spawn(harness="headless"),
            "unbound session": lambda: self.spawn(session=None),
            "other session": lambda: self.spawn(session="other-session-id"),
        }
        holder_pid = 4242
        self.plant_holder("codex", pid=holder_pid)
        for name, break_it in cases.items():
            with self.subTest(case=name):
                self.posts, self.post_kw = [], []
                self.spawned, self.wake_payloads = [], []
                break_it()
                # production shape: the hook's PARENT is the seat's own CC
                # process, whose environ claims the seat — that holder is
                # exactly what routes the wake when the register cannot
                with mock.patch("os.getppid", return_value=holder_pid):
                    res = self.run_hook()
                self.assertEqual(res["action"], "alert")
                self.assertEqual(
                    [a for a in self.spawned if "--deliver" in a], [],
                    "an unidentifiable pane must not be guessed at")
                # The hook arms ONE wake-child; the child owns both lanes.
                # An unidentifiable PANE does not mean an unknown SEAT, and
                # the DM is exactly what reaches a seat whose pane cannot
                # be trusted. Still loud, still no guessing.
                wakes = self.wake_children()
                self.assertEqual(len(wakes), 1, self.spawned)
                self.assertEqual(wakes[0]["seat"], "codex")
                calls = []
                self.run_wakes(calls)
                rooms = [txt for txt, kw in calls if not kw.get("dm")]
                self.assertEqual(len(rooms), 1, calls)
                self.assertIn("RESUME-TURN", rooms[0])
                self.assertIn("codex", rooms[0])
                dms = [str(kw["dm"]) for _txt, kw in calls if kw.get("dm")]
                self.assertEqual(dms, ["codex"],
                                 "the wake DM rides the trusted seat name")

    def test_a_session_with_no_seat_name_alerts_only_AFTER_looking(self):
        """Pre-fix this refused at the missing name BEFORE consulting any
        process evidence — a verdict from a check that never looked. Now the
        refusal happens only after the /proc scan, and it says what the scan
        actually saw (the argv signal is absent), not "cannot address"."""
        os.environ.pop("HELM_CHAT_NAME")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], [])):
            res = self.run_hook()
        self.assertEqual(res["action"], "alert")
        self.assertEqual([a for a in self.spawned if "--deliver" in a], [],
                         "a nameless session must not be guessed at")
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 1, self.spawned)
        self.assertFalse(wakes[0]["seat"],
                         "no proved name, no DM authority in the artifact")
        self.assertIn("HELM_CHAT_NAME", wakes[0]["reason"])
        self.assertIn("no live claude argv", wakes[0]["reason"])


class AlertRecoveryTest(unittest.TestCase):
    """The alert reports the measured wake route, never an unconditional cure."""

    def test_an_armed_beacon_is_the_primary_self_wake_route(self):
        with mock.patch.object(seats, "beacon_procs",
                               return_value=([123], None)) as probe:
            text = resumeturn.alert_text("opus-integrator", "pane is gone")
        probe.assert_called_once_with("opus-integrator", strict=True)
        self.assertIn("armed inbox beacon", text)
        self.assertIn("@mention or DM", text)
        self.assertNotIn("idle until someone types", text)

    def test_no_beacon_names_pane_input_as_a_fallback_not_a_verdict(self):
        with mock.patch.object(seats, "beacon_procs", return_value=([], None)):
            text = resumeturn.alert_text("opus-integrator", "pane is gone")
        self.assertIn("No live inbox beacon was observed", text)
        self.assertIn("pane input is the fallback", text)
        self.assertNotIn("one keypress wakes it", text)

    def test_an_unreadable_beacon_probe_stays_UNKNOWN(self):
        with mock.patch.object(seats, "beacon_procs",
                               return_value=([], "process table unreadable")):
            text = resumeturn.alert_text("opus-integrator", "pane is gone")
        self.assertIn("beacon state is UNKNOWN", text)
        self.assertIn("process table unreadable", text)
        self.assertNotIn("idle until someone types", text)


class NamelessPaneTest(ResumeTurnBase):
    """A pane with NO HELM_CHAT_NAME anywhere whose argv carries --resume
    <sid> — the live specimen measured 2026-07-29 (a real pid:
    grep -c <sid> /proc/pid/environ == 0, the sid in argv, the pane fully
    addressable via its ORCA_PANE_KEY). The pre-fix guard answered "this
    session declares no seat name — helm cannot address its pane": a true
    statement with a false conclusion, told to a human as a verdict.

    Every case pins `claude_processes` — an unpinned nameless test would scan
    the LIVE host's process table (the lesson test_resumeturn_adopted.py's
    DeliveryTest already paid for)."""

    def _proc(self, pid=14632, pane_key="94880e82:55095a54", resume_sid=SID,
              seat_name=None):
        return {"pid": pid, "seat": seat_name, "pane_key": pane_key,
                "worktree_id": None, "resume_sid": resume_sid}

    def test_the_nameless_pane_is_resolved_and_resumed(self):
        """THE FIX. Fails against pre-fix code, which never looked."""
        os.environ.pop("HELM_CHAT_NAME")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([self._proc()], [])):
            res = self.run_hook()
        self.assertEqual(res["action"], "spawned", res)
        argv = self.spawned[0]
        self.assertNotIn("--seat", argv,
                         "no seat name was proven, so none may ride the argv")
        self.assertEqual(argv[argv.index("--pids") + 1], "14632")
        self.assertEqual(argv[argv.index("--session") + 1], SID)

    def test_two_panes_on_one_sid_alert_rather_than_guess(self):
        os.environ.pop("HELM_CHAT_NAME")
        procs = [self._proc(), self._proc(pid=999)]
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=(procs, [])):
            res = self.run_hook()
        self.assertEqual(res["action"], "alert")
        self.assertEqual([a for a in self.spawned if "--deliver" in a], [])
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 1, self.spawned)
        self.assertIn("never guess", wakes[0]["reason"])

    def test_an_unreadable_environ_alerts_as_unknown_never_as_not_found(self):
        """CANNOT LOOK != absent: the matching pid's environ (where the pane
        address lives) could not be read, and the alert must say that instead
        of claiming the pane has no address."""
        os.environ.pop("HELM_CHAT_NAME")
        blind = self._proc(pane_key=None)   # environ unreadable => no keys
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([blind], [14632])):
            res = self.run_hook()
        self.assertEqual(res["action"], "alert")
        self.assertEqual([a for a in self.spawned if "--deliver" in a], [])
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 1, self.spawned)
        self.assertIn("could not be read", wakes[0]["reason"])
        self.assertNotIn("no address", wakes[0]["reason"])

    def test_the_alert_caption_uses_the_roster_name_cosmetically(self):
        """The roster reverse-lookup labels the alert so a human knows WHICH
        pane to poke — and does nothing else (no identity, no addressing)."""
        os.environ.pop("HELM_CHAT_NAME")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], [])), \
                mock.patch.object(seats, "seat_for_session",
                                  return_value="acme-platform-claude"):
            res = self.run_hook()
        self.assertEqual(res["action"], "alert")
        wakes = self.wake_children()
        self.assertIn("acme-platform-claude", wakes[0]["display"])
        self.assertFalse(wakes[0]["seat"],
                         "a cosmetic caption must never become DM authority")


class NamelessPaneRosterReportTest(ResumeTurnBase):
    """THE SENTENCE THE OWNER READ, and what was wrong with it.

    A project pane opened by hand (`clientproj-claude`) declares no
    HELM_CHAT_NAME, and a pane launched FRESH leaves no `--resume <sid>` in its
    argv, so the process scan is blind. The refusal then said "this session
    declares no seat name (HELM_CHAT_NAME) and its pane could not be resolved
    from process evidence either" — true, and read as "helm knows nothing about
    this session", while helm's OWN SessionStart join hook had already named
    that pane through `auto_name` (project + family) and written the row.

    THE CURE IS A REPORT, NOT AN ADOPTION, and the two pins in
    `OwnNameStrictnessTest` and `NamelessPaneTest` below/above say why: the
    roster name must not become `seat_name`, must not reach the child argv, and
    must not become DM authority. Those arms stay green here, deliberately —
    this class is the OTHER half: helm must SAY the fact it holds.

    A real temp HELM_HOME roster, written by the shipped writer, is the
    fixture; `claude_processes` is pinned (an unpinned nameless arm scans the
    live host)."""

    SEAT = "acme-platform-claude"

    def _blind(self):
        return mock.patch.object(orcaadopt, "claude_processes",
                                 return_value=([], []))

    def test_the_refusal_NAMES_the_seat_the_roster_binds(self):
        os.environ.pop("HELM_CHAT_NAME")
        seats.write_roster(self.SEAT, session=SID, cwd=self.cwd)
        self.assertEqual(seats.seats_for_session(SID), [self.SEAT],
                         "control: the shipped roster must bind this session")
        with self._blind():
            res = self.run_hook()
        self.assertEqual(res["action"], "alert", res)
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 1, self.spawned)
        reason = wakes[0]["reason"]
        self.assertIn(self.SEAT, reason,
                      "helm held this seat's name and did not say it")
        self.assertIn("REPORTED, not adopted", reason)
        self.assertIn("--seat", reason, "the refusal owes the way out")

    def test_the_report_is_NOT_identity_argv_or_DM_authority(self):  # noqa: VACUOUS_ASSERTION — the absences ARE the pinned contract; the control is the sibling arm above, which proves the same fixture DOES put the seat name in the alert reason
        """The pins, restated against THIS fixture: the same call that now
        names the seat must still mint no identity and address no DM."""
        os.environ.pop("HELM_CHAT_NAME")
        seats.write_roster(self.SEAT, session=SID, cwd=self.cwd)
        with self._blind():
            res = self.run_hook()
        self.assertEqual(res["action"], "alert", res)
        self.assertIsNone(seats.own_name(),
                          "reporting the roster minted an identity")
        self.assertFalse(self.wake_children()[0]["seat"],
                         "a report became DM authority")
        for argv in self.spawned:
            self.assertNotIn("--seat", argv)
            self.assertNotIn(self.SEAT, argv)

    def test_an_UNBOUND_session_says_ZERO_not_silence(self):  # noqa: VACUOUS_ASSERTION — the empty seats_for_session is the FIXTURE check; the arm's own assertions are assertIn on the count and the repair
        os.environ.pop("HELM_CHAT_NAME")
        self.assertEqual(seats.seats_for_session(SID), [],
                         "fixture: nothing may bind this session")
        with self._blind():
            res = self.run_hook()
        self.assertEqual(res["action"], "alert", res)
        reason = self.wake_children()[0]["reason"]
        self.assertIn("0 matches", reason)
        self.assertIn("helm chat join", reason)

    def test_an_UNREADABLE_roster_says_UNKNOWN_not_ZERO(self):
        """THE ONE STATE WHOSE REPAIR ADVICE WAS WRONG. A roster helm cannot
        PARSE answered `{}` through the fail-open reader, so this report counted
        zero rows and told the owner "the roster has never seen this session;
        run `helm chat join`" — a WRITE into the file that is the broken thing.
        UNKNOWN and zero take opposite actions, so they are different states and
        this branch is keyed on the state constant, not on message text.

        CONTROL BLAST RADIUS: `test_an_UNBOUND_session_says_ZERO_not_silence`
        above is the other half and is unchanged — the same call on a MISSING
        roster must still say 0 and must still offer the join. A cure that
        answered UNKNOWN for everything reddens that arm, not this one."""
        os.environ.pop("HELM_CHAT_NAME")
        pk.atomic_write(seats.roster_path(), "{ this is not json")
        self.assertEqual(seats.roster_checked(), ({}, True),
                         "fixture: the tri-state door must call this a FAILED "
                         "probe, or this arm is about nothing")
        with self._blind():
            res = self.run_hook()
        self.assertEqual(res["action"], "alert", res)
        reason = self.wake_children()[0]["reason"]
        self.assertIn("UNKNOWN", reason)
        self.assertNotIn("0 matches", reason,
                         "a failed read was reported as a real zero")
        self.assertNotIn("helm chat join", reason,
                         "the repair offered was a WRITE into the file helm "
                         "had just failed to parse")

    def test_an_AMBIGUOUS_binding_says_the_COUNT_and_the_repair(self):
        """Two rows holding one sid, written as a roster FILE: the shipped
        writer heals that state (it clears the sid from the other row), and the
        refusal is still owed — `seat_for_session` would resolve it BY BINDING
        AGE, which is a coin flip dressed as an answer."""
        os.environ.pop("HELM_CHAT_NAME")
        pk.atomic_write(seats.roster_path(), json.dumps({
            self.SEAT: {"session": SID, "sessions": [SID], "cwd": self.cwd},
            "other-claude": {"session": "s-moved", "sessions": ["s-moved", SID],
                             "cwd": self.cwd}}))
        self.assertEqual(sorted(seats.seats_for_session(SID)),
                         [self.SEAT, "other-claude"],
                         "control: the shipped resolver must see both rows")
        with self._blind():
            res = self.run_hook()
        self.assertEqual(res["action"], "alert", res)
        reason = self.wake_children()[0]["reason"]
        self.assertIn("2 roster rows", reason)
        self.assertIn("disown", reason)


class OwnNameStrictnessTest(ResumeTurnBase):
    """own_name() answers "may I ACT AS seat X" and is DELIBERATELY strict —
    a name any other process supplied would be impersonation. The nameless
    resume answers a DIFFERENT question ("which pane do I type this session's
    own handoff into") and must never be the excuse to loosen this. These
    pins make a quiet loosening fail a named test."""

    def test_own_name_is_env_only_and_never_consults_the_roster(self):
        os.environ.pop("HELM_CHAT_NAME")
        with mock.patch.object(seats, "roster",
                               side_effect=AssertionError("roster read")), \
                mock.patch.object(seats, "seat_for_session",
                                  side_effect=AssertionError("roster read")):
            self.assertIsNone(seats.own_name())

    def test_a_hostile_declared_name_is_no_name(self):
        os.environ["HELM_CHAT_NAME"] = "evil\x1b]0;pwn\x07"
        self.assertIsNone(seats.own_name())

    def test_the_nameless_resume_never_adopts_the_roster_name(self):
        """The crux. The roster KNOWS which seat held this sid, and the resume
        still must not act as that seat: the name may caption an alert, but
        the child argv — the identity the delivery leg runs under — carries
        no --seat and no roster name."""
        os.environ.pop("HELM_CHAT_NAME")
        proc = {"pid": 14632, "seat": None, "pane_key": "94880e82:55095a54",
                "worktree_id": None, "resume_sid": SID}
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([proc], [])), \
                mock.patch.object(seats, "seat_for_session",
                                  return_value="acme-platform-claude"):
            res = self.run_hook()
        self.assertEqual(res["action"], "spawned", res)
        self.assertIsNone(seats.own_name(), "resolving the pane must not "
                          "have minted an identity")
        argv = self.spawned[0]
        self.assertNotIn("--seat", argv)
        self.assertNotIn("acme-platform-claude", argv)


class ResumeTextTest(ResumeTurnBase):
    def test_the_directive_is_the_seats_own_fresh_handoff(self):
        self.journal(next_line="land the landreq delivery leg")
        self.run_hook()
        text = self.child_text()
        self.assertIn("land the landreq delivery leg", text)
        self.assertIn("Resuming after compaction", text)

    def test_a_STALE_handoff_is_never_handed_over_as_this_windows_directive(self):
        """The live 2026-07-28 miss, from the consuming side: a 37h-old handoff
        satisfied the PreCompact gate, so without the event floor the resume leg
        would restart the seat on a day-old directive."""
        self.journal(next_line="yesterdays plan", age_s=37 * 3600)
        self.run_hook()
        text = self.child_text()
        self.assertNotIn("yesterdays plan", text)
        self.assertIn("Re-ground first", text)

    def test_no_handoff_at_all_falls_back_to_the_reground_line(self):
        self.run_hook()
        self.assertEqual(self.child_text(), resumeturn.GENERIC)

    def test_ANOTHER_SEATS_handoff_is_never_handed_over_as_your_own(self):
        """The live 2026-07-31 near-miss. The journal shelf is per-PROJECT and
        every seat on the project writes to it, so `latest_entry`'s fallback —
        newest fresh entry, sid being a preference — was a lottery among the
        seats that compacted inside one floor window. A seat woke to another
        seat's NEXT, phrased "Your own handoff … Do not wait for a human",
        instructing it to land two lanes THAT SEAT still held the leases on,
        one under an open FIX verdict.

        Pinned on the DIRECTIVE, not on the absence of a complaint: the other
        seat's plan must not appear in the text at all."""
        self.journal(sid="f0ad7476-3178-4c38-93e2-f8720f223f26",
                     seat="helm-claude-2",
                     next_line="OI lands both my lanes, then release the leases")
        self.run_hook()
        text = self.child_text()
        self.assertNotIn("release the leases", text)
        self.assertNotIn("Your own handoff", text)
        self.assertIn("not by you", text)
        self.assertIn("name other seats", text)
        self.assertIn("do not release, land or claim anything it names", text)

    def test_an_UNREADABLE_shelf_says_UNKNOWN_and_never_no_handoff(self):
        """codex's FIX: the seat must not be told "No handoff was written" when
        the truth is that one could not be read — the entry may have been its
        own. Refuse the directive, REPORT the read failure."""
        p = self.journal(next_line="mine", seat="codex")

        def read():
            with mock.patch.object(inject, "project_for_cwd",
                                   return_value="proj"), \
                    mock.patch.object(handoff, "compaction_floor",
                                      return_value=0):
                return resumeturn.resume_text(os.getcwd(), SID)

        # POSITIVE CONTROL FIRST: readable, this exact entry DOES resume.
        text, path = read()
        self.assertIn("mine", text)
        self.assertIn("handoff", path)
        os.chmod(p, 0)
        self.addCleanup(lambda: os.path.exists(p) and os.chmod(p, 0o644))
        if os.access(p, os.R_OK):
            self.skipTest("cannot make a file unreadable as this user")
        text, path = read()
        self.assertIsNone(path)
        self.assertNotIn("No handoff was written", text)
        self.assertIn("is unknown", text)
        self.assertIn("could not be read", text)
        self.assertIn("Report the unreadable shelf", text)

    def test_an_UNATTRIBUTABLE_fresh_handoff_is_refused_with_its_own_reason(self):
        """A stamp-less entry (a pre-seat-key entry, or one written by a
        session that declares no name) proves nothing either way. It is refused
        for the same reason — the sentence built from it asserts ownership —
        but the reader is told WHICH refusal it hit, because "nobody could
        prove it" and "it is someone else's" are different facts."""
        self.journal(sid="99999999-9999-9999-9999-999999999999",
                     next_line="somebody elses plan")
        self.run_hook()
        text = self.child_text()
        self.assertNotIn("somebody elses plan", text)
        self.assertIn("proven yours", text)

    def test_a_FORKED_session_id_still_finds_its_own_handoff_by_seat(self):
        """The case the sid-as-preference fallback existed to protect: older CC
        forks a new session id across the compaction boundary, so the entry
        this very seat wrote minutes ago carries an id that no longer matches.
        The seat key survives the fork, so the protection is kept WITHOUT the
        shared-shelf lottery — this is the test that would fail if someone
        'simplified' the fix to a strict sid equality check."""
        self.journal(sid="11111111-2222-3333-4444-555555555555",
                     seat="codex",            # == HELM_CHAT_NAME in setUp
                     next_line="finish the delivery leg")
        self.run_hook()
        text = self.child_text()
        self.assertIn("finish the delivery leg", text)
        self.assertIn("yours by seat codex", text)

    def test_the_injected_line_NAMES_the_identity_it_matched(self):
        """OI's addition when the fix was scoped: the frontmatter check that
        caught the near-miss becomes part of the printed sentence, so the next
        context-less reader gets the verification inline instead of needing the
        discipline to run it."""
        self.journal(next_line="land the landreq delivery leg")   # sid matches
        self.run_hook()
        text = self.child_text()
        self.assertIn("Resuming after compaction", text)
        self.assertIn("yours by session %s" % SID[:8], text)

    def test_a_seat_that_declares_no_name_gets_no_ones_handoff(self):
        """own_name() is empty, so NO entry can be proven foreign (foreign_seat
        fails open) and none can be proven this caller's by seat either. The
        sid is then the only proof left — a nameless process must not inherit
        the shelf's newest entry the way the old fallback handed it over.

        Called at the resume_text seam rather than through the hook: a nameless
        session takes the pane-resolution path, whose outcome is a different
        law with its own tests above."""
        self.journal(sid="88888888-8888-8888-8888-888888888888",
                     seat="helm-claude-2", next_line="not yours either")

        def read():
            with mock.patch.object(inject, "project_for_cwd",
                                   return_value="proj"), \
                    mock.patch.object(handoff, "compaction_floor",
                                      return_value=0):
                return resumeturn.resume_text(os.getcwd(), SID)

        os.environ.pop("HELM_CHAT_NAME")
        text, path = read()
        self.assertIsNone(path)
        self.assertNotIn("not yours either", text)
        self.assertIn("proven yours", text)
        # POSITIVE CONTROL on the same two observables. Without it a broken
        # fixture — an unwritten entry, a floor that excludes everything — is
        # indistinguishable from the refusal under test, and the test would
        # pass for the wrong reason. Declaring the author's own name makes the
        # SAME call return the SAME entry.
        os.environ["HELM_CHAT_NAME"] = "helm-claude-2"
        text, path = read()               # SAME observables, rebound
        self.assertIn("88888888", path)
        self.assertIn("not yours either", text)
        self.assertIn("yours by seat helm-claude-2", text)

    def test_the_anonymous_line_asserts_nothing_it_cannot_support(self):
        """task/1688, measured live on hc2 2026-08-27. The sibling arm above
        already knew the MECHANISM (foreign_seat fails open for a nameless
        reader); what nobody checked was the SENTENCE built on it. The
        UNCLAIMED text says "a handoff WAS written for this compaction, but not
        by you", "the entry on the shelf is someone else's plan" and "there is
        nothing here to continue FROM" — three claims an anonymous process
        cannot support, printed to the one reader who has just lost the context
        that would let it notice. On the live incident an entry carrying that
        seat's OWN session id was on the shelf while it read those words."""
        self.journal(sid="88888888-8888-8888-8888-888888888888",
                     seat="seat-b", next_line="not yours either")

        def read():
            with mock.patch.object(inject, "project_for_cwd",
                                   return_value="proj"), \
                    mock.patch.object(handoff, "compaction_floor",
                                      return_value=0):
                return resumeturn.resume_text(os.getcwd(), SID)

        os.environ.pop("HELM_CHAT_NAME")
        text, path = read()
        self.assertIsNone(path)
        self.assertNotIn("someone else", text)
        self.assertNotIn("nothing here to continue FROM", text)
        for expected in ("declares no seat identity", "HELM_CHAT_NAME",
                         "helm handoff check"):
            self.assertIn(expected, text)
        # POSITIVE CONTROL, same shape the sibling arm uses: declaring a name
        # must swing the SAME call back to the foreign wording, or this arm is
        # measuring a broken fixture rather than the reader's anonymity.
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        text = read()[0]
        self.assertIn("name other seats", text)
        self.assertNotIn("declares no seat identity", text)

    def test_the_reground_line_routes_obligations_through_the_ledger_fold(self):
        """The recovered-seat trap (three live instances in 90 minutes,
        2026-08-01): a compacted context makes stale history read as an
        invitation — one seat rebuilt a superseded lane, another assembled a
        LAND READY from a cancelled row + an ungated approve + a vanished tip.
        The re-ground line is the FIRST text a compacted seat acts on, so it
        must say where live obligations come from (the open-row fold) and that
        a row's status outranks the chat about it. Pinned on CONTENT, not by
        equality with the constant — an equality check stays green through any
        regression of the constant itself."""
        self.run_hook()
        text = self.child_text()
        self.assertIn("helm dispatch list --open", text)
        self.assertIn("cancelled or superseded row is DEAD", text)
        self.assertIn("check its status", text)
        # codex's adversarial finding on the first cut: "ONLY open rows"
        # with no escape inverts an unreadable fold into "no live work" —
        # the opposite silent failure. Unreadable is UNKNOWN, never empty.
        self.assertIn("UNKNOWN, never empty", text)
        self.assertIn("unreadable", text)

    def test_the_injected_line_is_laundered_and_single_line(self):
        """It crosses the adapter seam into a live terminal: a directive
        carrying ESC or a newline must not reshape the pane or split into two
        keystroke bursts."""
        self.journal(next_line="ship \x1b[31mit\x1b[0m now")
        self.run_hook()
        text = self.child_text()
        self.assertNotIn("\x1b", text)          # the ESC is gone…
        self.assertNotIn("\n", text)
        self.assertIn("ship [31mit[0m now", text)   # …the prose is intact


class LoopGuardTest(ResumeTurnBase):
    def test_a_double_firing_hook_injects_exactly_once(self):
        first = self.run_hook()
        second = self.run_hook()
        self.assertEqual(first["action"], "spawned")
        self.assertEqual(second["action"], "debounce")
        self.assertEqual(len(self.spawned), 1)

    def test_a_compaction_right_on_top_of_a_resume_alerts_instead_of_spiraling(self):
        """compact -> resume -> compact within the spiral window means the
        resume itself is feeding the context. Injecting again is the spiral;
        this is the by-construction refusal."""
        os.environ["HELM_RESUME_TURN_DEBOUNCE_S"] = "1"
        self.run_hook()
        deliver = lambda: [a for a in self.spawned if "--deliver" in a]
        self.assertEqual(len(deliver()), 1)
        self._age_state(300)                       # 5 minutes later
        res = self.run_hook()
        self.assertEqual(res["action"], "spiral")
        self.assertEqual(len(deliver()), 1, "the spiral must not inject")
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 1, "the spiral must still alert loudly")
        self.assertFalse(wakes[0]["seat"],
                         "spiral carries no DM authority in its artifact")

    def test_the_rolling_cap_stops_a_slow_spiral_and_re_arms_with_time(self):
        os.environ["HELM_RESUME_TURN_DEBOUNCE_S"] = "1"
        os.environ["HELM_RESUME_TURN_SPIRAL_S"] = "10"
        deliver = lambda: [a for a in self.spawned if "--deliver" in a]
        for _ in range(resumeturn.MAX_RESUMES):
            self.run_hook()
            self._age_state(60)
        self.assertEqual(len(deliver()), resumeturn.MAX_RESUMES)
        res = self.run_hook()
        self.assertEqual(res["action"], "capped")
        self.assertEqual(len(deliver()), resumeturn.MAX_RESUMES)
        self._age_state(resumeturn.WINDOW_S + 60)   # the window rolls past
        self.assertEqual(self.run_hook()["action"], "spawned")
        self.assertEqual(len(deliver()), resumeturn.MAX_RESUMES + 1)

    def _age_state(self, seconds):
        """Push every recorded resume `seconds` into the past."""
        p = resumeturn.state_path()
        st = pk.read_json(p, {}) or {}
        for e in st.values():
            e["at"] = [t - seconds for t in e.get("at", [])]
            e["last_at"] = (e.get("last_at") or time.time()) - seconds
        pk.write_json(p, st)


class CliTest(ResumeTurnBase):
    def test_deliver_reads_the_text_file_once_and_removes_it(self):
        """A leftover text file must never re-inject on a later run."""
        tp = os.path.join(self.tmp, "resume.txt")
        pk.atomic_write(tp, "CONTINUE THE WORK")
        ad = FakeAdapter()
        with mock.patch("helm.harness.detect", return_value=ad):
            rc = resumeturn.cmd_resume_turn(
                ["--deliver", "--seat", "codex", "--session", SID,
                 "--text-file", tp, "--delay", "0"])
        self.assertEqual(rc, 0)
        self.assertEqual(ad.sent,
                         [("h1", "CONTINUE THE WORK", False), ("h1", "", True)])
        self.assertFalse(os.path.exists(tp))

    def test_status_surfaces_what_the_leg_last_did(self):
        self.run_hook()
        lines = "\n".join(resumeturn.report_lines())
        self.assertIn("resume-turn", lines)
        self.assertIn("codex", lines)

    def test_junk_args_refuse_before_anything_fires(self):
        rc = resumeturn.cmd_resume_turn(["--frobnicate"])
        self.assertEqual(rc, 2)

    def test_a_nameless_deliver_rides_pids_and_reaches_the_sid_path(self):  # noqa: VACUOUS_ASSERTION — positive controls pin rc 0, exact args, pid guard, adapter, and both provenance callbacks; file absence is the one-shot cleanup contract
        tp = os.path.join(self.tmp, "resume.txt")
        pk.atomic_write(tp, "CONTINUE THE WORK")
        with mock.patch.object(orcaadopt, "send_to_sid_pane",
                               return_value=("resumed", "ok")) as sidp:
            rc = resumeturn.cmd_resume_turn(
                ["--deliver", "--session", SID, "--text-file", tp,
                 "--delay", "0", "--pids", "14632"])
        self.assertEqual(rc, 0)
        sidp.assert_called_once()
        args, kwargs = sidp.call_args
        self.assertEqual(args, (SID, "CONTINUE THE WORK"))
        self.assertEqual(kwargs["expect_pids"], [14632])
        self.assertIsNone(kwargs["adapter"])
        self.assertTrue(callable(kwargs["on_typed"]))
        self.assertTrue(callable(kwargs["on_submit"]))
        self.assertFalse(os.path.exists(tp))

    def test_a_deliver_with_neither_seat_nor_pids_refuses(self):
        """Without a name AND without a proven holder the child has nothing
        to re-prove a pane by — rc 2 before any file is consumed."""
        tp = os.path.join(self.tmp, "resume.txt")
        pk.atomic_write(tp, "CONTINUE THE WORK")
        rc = resumeturn.cmd_resume_turn(
            ["--deliver", "--session", SID, "--text-file", tp, "--delay", "0"])
        self.assertEqual(rc, 2)
        self.assertTrue(os.path.exists(tp), "a refused deliver must not "
                        "consume the one-shot text file")

    def test_the_repair_childs_forked_argv_reaches_the_child(self):  # noqa: VACUOUS_ASSERTION — the child is asserted called once with the exact record key, attempt and room, and rc asserted 0; the consumed text file is the one-shot contract
        """THE ARGV THE REPAIR PARENT FORKS IS ONE ITS CHILD'S PARSER ACCEPTS.
        `_child_argv` carries the attempt and the owed room across the fork;
        the child's own tail guard must know both flags, or every repair
        child exits 2 before it reads its text file."""
        tp = os.path.join(self.tmp, "resume.txt")
        pk.atomic_write(tp, "CATCH UP")
        key = "deaf:codex:%s" % SID
        argv = resumeturn._child_argv("codex", SID, tp, 0, record_key=key,
                                      attempt="1789400000.0#1",
                                      owed_room="foreign-x")
        self.assertEqual(argv[1:4], ["seat", "resume-turn", "--deliver"])
        self.assertIn("--attempt", argv)
        self.assertIn("--owed-room", argv)
        with mock.patch.object(resumeturn, "child",
                               return_value=("resumed", "ok")) as ch:
            rc = resumeturn.cmd_resume_turn(argv[3:])
        self.assertEqual(ch.call_count, 1,
                         "the repair child's own parser refused the argv its "
                         "parent forked (rc %s): %s" % (rc, argv[3:]))
        kwargs = ch.call_args.kwargs
        self.assertEqual((kwargs["record_key"], kwargs["attempt"],
                          kwargs["owed_room"]),
                         (key, "1789400000.0#1", "foreign-x"))
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(tp))



class SeenStateClearedOnCompactTest(unittest.TestCase):
    """A COMPACTION ERASES THE CONTEXT AND KEEPS THE SESSION ID.

    Measured on the fire-ledger 2026-08-04: session cf8ce076 carries 306 turns
    across five days and several compactions under ONE id. So the injector's
    per-session suppression outlives the thing it suppresses against, and
    nothing else in the pipeline can see the boundary — this hook is the only
    leg that learns a compaction happened.

    ISOLATED, BECAUSE IT WAS NOT. This class extended unittest.TestCase with no
    setUp, so `_seen_path` resolved against the REAL home and every run wrote
    into the LIVE fleet's injection state. Measured 2026-08-04: with HELM_HOME
    unset it resolves to <operator home>/_global/.state/inject-seen, so every
    test below planted and deleted files the running fleet reads.

    THE DANGER IS THE REACH, NOT THE RESIDUE. Left-behind files are only the
    visible symptom, and that symptom is currently zero — by accident, because
    once every SessionStart source began resetting, the hook deleted each
    planted file on its way out. What remains is worse: these tests drive
    forget_session, which DELETES suppression state BY SESSION ID against
    whatever home is in scope. A fixture sid that ever collided with a live
    seat's session would silently reset that seat's injection, and the seat
    could not tell. Same class as the chat-dir leak this module's siblings
    already guard: a suite that writes to what the fleet reads is not a suite,
    it is a second agent."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seenclear-")
        self._env = {k: os.environ.get(k) for k in ("HELM_HOME", "HELM_CHAT_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _payload(self, sid, src="compact"):
        return {"source": src, "session_id": sid, "cwd": os.getcwd()}

    def test_the_fixture_cannot_reach_the_live_fleet_state(self):
        """THE GUARD ON THE ISOLATION ITSELF. Every test below plants a file at
        _seen_path and asserts what the hook does to it; if the env ever stops
        being redirected, they all still PASS while operating on the real
        fleet's injection state. Nothing in their assertions would notice."""
        from helm.inject import _ledger
        path = _ledger._seen_path("seen-clear-probe-1")
        self.assertTrue(path.startswith(self.tmp),
                        "seen-state path escaped the test home: %s" % path)
        self.assertNotIn("/.helm/_global", path)

    def test_a_compact_drops_the_session_seen_state(self):
        from helm.inject import _ledger
        sid = "seen-clear-probe-1"
        path = _ledger._seen_path(sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write('{"v":1,"turn":9,"fired":{"x":[9,1.0]}}')
        # POSITIVE CONTROL, unconditional: the file really is there, so the
        # absence below is the hook acting and not a path that never existed.
        self.assertTrue(os.path.exists(path))
        resumeturn.hook(self._payload(sid))
        self.assertFalse(os.path.exists(path),
                         "a compacted seat must re-fire its pinned lane")

    def test_no_source_leaves_the_suppression_standing(self):
        """WAS test_a_non_compact_source_leaves_it_alone, and the rename tracks
        the contract twice over. `startup` used to preserve suppression; then
        only `resume` did; now NOTHING does. The predicate is "did the context
        go away", expressed as a deny-list whose exception set is EMPTY, so a
        source nobody enumerated resets. Adding one back needs proof of context
        EQUALITY, not proof that a transcript exists."""
        from helm.inject import _ledger
        # UNCONDITIONAL CONTROL: planting really writes where the hook reads,
        # so an empty loop cannot satisfy the survival assertions by running
        # none of them.
        probe = _ledger._seen_path("seen-clear-probe-control")
        os.makedirs(os.path.dirname(probe), exist_ok=True)
        with open(probe, "w") as f:
            f.write('{"v":1,"turn":3,"fired":{}}')
        self.assertTrue(os.path.exists(probe))
        for src, survives in (("resume", False), ("startup", False),
                              ("clear", False), ("whats-this-then", False)):
            with self.subTest(source=src):
                sid = "seen-clear-probe-2-" + (src or "empty")
                path = _ledger._seen_path(sid)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write('{"v":1,"turn":3,"fired":{}}')
                self.assertTrue(os.path.exists(path))
                resumeturn.hook(self._payload(sid, src=src))
                self.assertEqual(os.path.exists(path), survives,
                                 "source=%s survival mismatch" % src)

    def test_a_dry_run_never_mutates(self):
        from helm.inject import _ledger
        sid = "seen-clear-probe-3"
        path = _ledger._seen_path(sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write('{"v":1,"turn":3,"fired":{}}')
        self.assertTrue(os.path.exists(path))
        resumeturn.hook(self._payload(sid), dry=True)
        self.assertTrue(os.path.exists(path), "dry must not touch disk")

class EpochProvenanceTest(ResumeTurnBase):
    """WHY THE EPOCH RESET, recorded where the reset is already visible.

    The injector's per-session counter restarts at 1 after every
    `forget_session`, so the fire ledger has always been able to say THAT a
    context boundary happened and never WHY. Reading one night's boundaries
    out of it therefore meant cross-referencing the ledger against the resume
    state and the hook's own stderr — three files, and an instrument that had
    to be retracted — to answer a question two fields settle.

    THE THIRD VALUE IS THE POINT. "No PreCompact record was found" and "the
    record could not be read" are different facts: only the first is evidence
    about the compaction, and a field that collapses them rebuilds, one layer
    down, exactly the ambiguity it was added to remove."""

    def plant_record(self, **fields):
        """A PreCompact record under this seat's key, written the way the
        producer writes it. Direct rather than through the PreCompact hook:
        the arms below are about what the SessionStart leg can READ, and the
        producer's own contract is pinned by NativeAutocompactionTest."""
        rec = {"session": SID, "trigger": "auto", "agent": None,
               "at": time.time()}
        rec.update(fields)

        def change(entry):
            entry["precompact"] = rec
            return None, True
        resumeturn._mutate_entry("codex", change)
        return rec

    def test_a_record_that_is_there_vouches_and_its_absence_does_not(self):
        """The two ordinary answers, and they must be told apart by the READ
        rather than by a default: the control below plants a real record and
        gets `vouched` from the same call that answers `unvouched` with none."""
        from helm.inject import _ledger
        self.assertEqual(
            resumeturn._epoch_provenance("clear", SID, None),
            {"source": "clear", "vouch": _ledger.EPOCH_UNVOUCHED},
            "an empty store holds no record, and that is a READ result")
        # UNCONDITIONAL POSITIVE CONTROL on the SAME observable: with a record
        # planted, this call answers `vouched`, so the `unvouched` above is
        # the absence of a record and not a predicate that never says yes.
        self.plant_record()
        self.assertEqual(
            resumeturn._epoch_provenance("compact", SID, None),
            {"source": "compact", "vouch": _ledger.EPOCH_VOUCHED})

    def test_an_unreadable_store_is_unknown_and_never_unvouched(self):
        """THE ARM THE WHOLE FIELD EXISTS FOR. A store that cannot be read
        says NOTHING about whether a record was there, and reporting that as
        `unvouched` would print a claim the reader never made."""
        from helm.inject import _ledger
        # CONTROL, unconditional and on the SAME observable: the readable
        # store answers `vouched` here, so `unknown` below is the unreadable
        # store and not a call that cannot reach the record at all.
        self.plant_record()
        self.assertEqual(
            resumeturn._epoch_provenance("compact", SID, None)["vouch"],
            _ledger.EPOCH_VOUCHED)
        with open(resumeturn.state_path(), "w", encoding="utf-8") as f:
            f.write("{not json at all")
        self.assertIsInstance(resumeturn._peek("codex"), resumeturn.Unreadable)
        self.assertEqual(
            resumeturn._epoch_provenance("compact", SID, None),
            {"source": "compact", "vouch": _ledger.EPOCH_UNKNOWN})

    def test_a_consumed_or_foreign_record_vouches_for_nothing(self):  # noqa: VACUOUS_ASSERTION — the plain record AFTER the loop is an unconditional positive control on the SAME observable (`_epoch_provenance(...)["vouch"]`), asserting EPOCH_VOUCHED, so every `unvouched` in the loop is a clause and not a predicate that never says yes
        """One PreCompact vouches for one SessionStart of one thread. A
        consumed record has already been spent, and a record naming another
        session or another thread describes another boundary."""
        from helm.inject import _ledger
        for fields, why in (({"consumed": True}, "already spent"),
                            ({"session": "another-session"}, "another session"),
                            ({"agent": "sub-1"}, "another thread")):
            with self.subTest(why=why):
                self.plant_record(**fields)
                self.assertEqual(
                    resumeturn._epoch_provenance("compact", SID, None)["vouch"],
                    _ledger.EPOCH_UNVOUCHED, why)
        # UNCONDITIONAL CONTROL on the SAME observable, after the loop: the
        # plain record still vouches, so the loop's `unvouched` answers are
        # its clauses and not a fixture that stopped planting.
        self.plant_record()
        self.assertEqual(
            resumeturn._epoch_provenance("compact", SID, None)["vouch"],
            _ledger.EPOCH_VOUCHED)

    def test_a_payload_with_no_source_is_unknown_and_not_a_source(self):
        """An absent source is recorded as the module's own spelling for one
        it cannot name — the same one the skip detail and the alert print —
        never as a source, and never silently omitted."""
        self.assertEqual(resumeturn._epoch_provenance("", SID, None)["source"],
                         "?")
        # CONTROL: a payload that DOES name its source is carried verbatim.
        self.assertEqual(resumeturn._epoch_provenance("clear", SID, None)["source"],
                         "clear")

    def test_the_hook_records_the_provenance_of_the_boundary_it_drops(self):  # noqa: VACUOUS_ASSERTION — the populated `_seen_load(SID)["fired"]` assertion before the hook is an unconditional positive control on the same state, so the absence afterwards is the hook acting on a file that provably existed
        """THE WIRING, from the payload to the state the next turn reads. The
        arms above test the function; this proves the hook calls it with the
        source it actually received and that the answer survives the drop."""
        from helm.inject import _ledger
        seen = inject._seen_path(SID)
        os.makedirs(os.path.dirname(seen), exist_ok=True)
        pk.write_json(seen, {"v": 1, "ts": pk.now_ts(), "turn": 7,
                             "fired": {"jit-a": [1, 0.8]}})
        # CONTROL: the suppression is really there, so the fresh read below
        # is the hook acting and the provenance rides a real boundary.
        self.assertEqual(_ledger._seen_load(SID)["fired"], {"jit-a": [1, 0.8]})
        self.run_hook(self.payload(source="clear"))
        self.assertFalse(os.path.exists(seen),
                         "the drop is still provable by the file's ABSENCE")
        state = _ledger._seen_load(SID)
        self.assertEqual(state["fired"], {}, "and the suppression is gone")
        self.assertEqual(state["epoch"],
                         {"source": "clear", "vouch": _ledger.EPOCH_UNVOUCHED})

    def test_a_dry_run_records_no_provenance(self):
        """A dry run mutates nothing, and a marker is a mutation."""
        from helm.inject import _ledger
        # CONTROL on the SAME observable: the wet hook writes one.
        self.run_hook(self.payload(source="clear"))
        self.assertIsNotNone(_ledger._seen_load(SID)["epoch"])
        os.remove(_ledger._epoch_path(SID))
        resumeturn.hook(self.payload(source="clear"), dry=True)
        self.assertIsNone(_ledger._seen_load(SID)["epoch"])


class HookWiringTest(ResumeTurnBase):
    def test_the_spec_is_installed_on_homes_AND_seats(self):
        from helm import hooks
        spec = next(s for s in hooks.SPECS if s["name"] == "resume-turn")
        self.assertEqual(spec["event"], "SessionStart")
        self.assertEqual(spec["args"], "seat resume-turn --hook-json")
        self.assertIn(spec, hooks.SEAT_SPECS)
        cmd = hooks.spec_command(spec)
        self.assertIn("seat resume-turn --hook-json", cmd)
        # NEVER BLOCKS, read from the wrapper's own KIND operand: only `gate`
        # has an arm that propagates rc 2, and the arms themselves live in the
        # shipped bin/helm-hook rather than in this string.
        self.assertEqual(shlex.split(cmd)[1], "lane")
        self.assertTrue(hooks._fail_open(cmd))                # ...never gates

    def test_the_verb_is_reachable_through_seat(self):
        with mock.patch.object(resumeturn, "cmd_resume_turn",
                               return_value=0) as m:
            self.assertEqual(seat.cmd_seat(["resume-turn", "--status"]), 0)
        m.assert_called_once_with(["--status"])


class SidechainCompactionTest(ResumeTurnBase):
    """task/2542 door (2): a subagent's own compaction is not the seat's.

    THE FAILURE MODE (task/2542, measured on a live seat): a background
    subagent auto-compacts and this hook answers "spawned — Resuming after
    compaction ..." inside the sidechain, handing the subagent the seat's own
    directive and arming a child to type into the seat's pane. The subagent
    then takes the seat's beacon. A subagent compaction fires SessionStart:compact (Claude Code
    2.1.270 guards both call sites only against delegated-observation agents).

    THE FIELD IS UNMEASURED FOR THIS EVENT: the same bytes build the
    SessionStart payload without agent_id. This door is keyed on the
    documented field, and without it the output must be today's bytes.

    THE PANE-SEND SPY IS `spawn_child`. The hook never types into a pane in
    process: every pane input and every wake it causes crosses that one fork
    (the `--deliver` child types, the `--alert-fd` child wakes), and a DM wake
    goes through chat.post. Both are recorded by the base fixture's real
    seams, not rebuilt here."""

    def compaction(self, **extra):
        return dict(self.payload(transcript=self.transcript_path()), **extra)

    def test_a_subagent_compaction_spawns_nothing_and_prints_nothing(self):  # noqa: VACUOUS_ASSERTION — the empty stdout, spawn and post lists are the door; the stderr line is its non-empty positive, and the byte-identical twin below fills the same spies through the same runner
        # task/2980 lane 7: resume-turn speaks to the main thread only. The
        # child's line goes to stderr, where the seat's operator reads it.
        self.journal(next_line="land the landreq delivery leg")
        rc, out = self.run_installed(self.compaction(
            agent_id="a1b2", agent_type="general-purpose"))
        self.assertEqual(rc, 0)
        self.assertIn("you are a subagent inside seat 'codex'", self.last_stderr)
        self.assertEqual(out, "")               # the child reads nothing
        self.assertEqual(self.spawned, [])      # no pane input, no wake child
        self.assertEqual(self.posts, [])        # no room alert, no DM wake

    def test_the_same_compaction_on_the_main_thread_is_byte_identical(self):
        """THE CONTROL, through the same stdin, stdout and spies."""
        self.journal(next_line="land the landreq delivery leg")
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("--deliver", self.spawned[0])
        text = self.child_text()
        self.assertIn("land the landreq delivery leg", text)
        self.assertEqual(out, "helm seat resume-turn: spawned — %s\n" % text)

    def test_a_subagent_compaction_leaves_the_seats_injection_state_alone(self):
        """Every OTHER context loss drops the injector's per-session
        suppression, because the session lost the context. A subagent's
        compaction lost the subagent's context, and UserPromptSubmit never
        injects into a subagent, so resetting the shared session file would
        only re-deliver premises into a main thread that still holds them."""
        seen = inject._seen_path(SID)
        os.makedirs(os.path.dirname(seen), exist_ok=True)
        with open(seen, "w", encoding="utf-8") as f:
            f.write('{"v":1,"turn":3,"fired":{}}')
        self.run_installed(self.compaction(agent_id="a1b2"))
        self.assertTrue(os.path.exists(seen))
        # MUST-HIT TWIN: the main thread's compaction removes the same file,
        # so the survival above is the sidechain door and not a path the hook
        # never reads.
        self.run_installed(self.compaction())
        self.assertFalse(os.path.exists(seen))


class SubagentCompactionWithoutAgentIdTest(ResumeTurnBase):
    """task/2926: the lead's handoff NEXT reached an Agent-tool subagent.

    THE PAYLOAD IS THE LEAD'S. Claude Code 2.1.280 builds a subagent's
    SessionStart(compact) input with the lead's session_id and main
    transcript_path and no agent_id, so the task/2542 door above never
    fires for it (measured: a project lead's subagent compacted and its own
    transcript recorded the lead's "says NEXT" line as SessionStart:compact
    hook output). These fixtures are that
    payload in every field the harness sends, and the thread is told apart
    only by the session's transcripts on disk."""

    NEXT = "merge #1070 and deploy prod"

    def compaction(self):
        return self.payload(transcript=self.transcript_path(
            '{"type":"user","message":{"content":"x"}}'))

    def subagent(self, name="agent-a1.jsonl", age_s=0):
        d = os.path.join(self.tmp, SID, "subagents")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"type":"user","isSidechain":true}\n')
        if age_s:
            t = time.time() - age_s
            os.utime(p, (t, t))
        return p

    def precompact_then_main_turn(self, payload):
        """The subagent's PreCompact records the main transcript's position
        (its payload names the lead's transcript too), then the lead keeps
        working: a turn lands in the main transcript before this hook."""
        resumeturn.note_precompact(SID, "auto", None,
                                   payload["transcript_path"])
        with open(payload["transcript_path"], "a", encoding="utf-8") as f:
            f.write('{"type":"assistant","message":{"content":"lead"}}\n')

    def test_a_proven_subagent_compaction_hears_no_next_and_arms_nothing(self):  # noqa: VACUOUS_ASSERTION — the empty spawn and post lists are the door; the lead arm below fills the same spies through the same runner
        self.journal(next_line=self.NEXT)
        payload = self.compaction()
        self.subagent()
        self.precompact_then_main_turn(payload)
        rc, out = self.run_installed(payload)
        self.assertEqual(rc, 0)
        # A PROVEN child reads nothing (task/2980 lane 7): the withheld NEXT
        # and the reason go to stderr only.
        self.assertEqual(out, "")
        self.assertNotIn(self.NEXT, self.last_stderr)
        self.assertIn("lead handoff suppressed", self.last_stderr)
        self.assertIn("main transcript took a turn", self.last_stderr)
        self.assertEqual(self.spawned, [])     # the seat's pane is untouched
        self.assertEqual(self.posts, [])       # no alert, no DM

    def test_a_seatless_notice_reads_as_english(self):
        # Measured in two subagent transcripts: "not proven to be seat this
        # seat's main conversation", from a compaction with no seat name.
        said = resumeturn.lead_suppressed_text(None, "why")
        self.assertIn("not proven to be this seat's main conversation", said)
        self.assertIn("If you are a subagent, follow your own brief", said)
        self.assertNotIn("seat this seat", said)
        self.assertIn("seat codex's main conversation",
                      resumeturn.lead_suppressed_text("codex", "why"))

    def test_the_lead_with_no_live_subagent_still_hears_its_next(self):
        self.journal(next_line=self.NEXT)
        self.subagent(age_s=resumeturn.precompact_window_s() + 60)
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertIn("says NEXT: " + self.NEXT, out)
        self.assertNotIn("suppressed", out)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn(self.NEXT, self.child_text())

    def test_an_unattributable_compaction_fails_safe_and_says_why(self):
        """A live subagent and a quiet main transcript: either thread could
        have compacted. The printed line withholds the NEXT and names why;
        the pane leg, which reaches only the seat's main conversation, still
        carries it, so the lead's own resume is never silently dropped."""
        self.journal(next_line=self.NEXT)
        self.subagent()
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertNotIn(self.NEXT, out)
        self.assertIn("lead handoff suppressed", out)
        self.assertIn("1 subagent(s) of this session wrote inside the "
                      "compaction window (agent-a1.jsonl)", out)
        self.assertIn("`helm handoff check` names your own NEXT", out)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn(self.NEXT, self.child_text())

    def test_a_payload_without_a_transcript_fails_safe(self):
        self.journal(next_line=self.NEXT)
        res = self.run_hook(self.payload())       # transcript_path ""
        self.assertEqual(res["action"], "spawned")
        self.assertNotIn(self.NEXT, res["detail"])
        self.assertIn("names no transcript", res["detail"])
        self.assertIn(self.NEXT, res["text"])


class NativeAutocompactionTest(ResumeTurnBase):
    """task/2568: a native autocompaction posts no resume alert.

    THE DEFECT (owner report): 240 "RESUME-TURN: seat X compacted and
    could NOT be auto-resumed" rows in room helm (~85 KB), every one about a
    seat Claude Code's native autocompaction had already resumed by itself.
    The SessionStart payload says `source == "compact"` for a native
    autocompaction and a deliberate /compact alike, and the hook read only
    that field. The PreCompact payload one hook earlier carries the
    distinction (`trigger: "auto"` / `"manual"`), `helm handoff check
    --hook-json` records it, and the resume leg reads it back.

    EVERY ARM DRIVES BOTH SHIPPED HOOKS with real payload shapes: the record
    is written by `handoff check --hook-json` on stdin (never a hand-written
    entry), and the decision is read through `seat resume-turn --hook-json`
    on stdin. The spies are the module's real doors: `spawn_child` (every
    pane input and every wake crosses it) and `chat.post` (the DM lane).

    THE TRANSCRIPT IS A REAL FILE, never a mocked stat: both payloads carry
    the same `transcript_path`, the producer records its position, and the
    arms grow, replace, shrink or remove that file the way the harness or
    the world would, then read what the consumer makes of it (module
    docstring, clause 5)."""

    # THE PRODUCER'S BOUND, SCALED WITH THE HOLDS THAT TEST IT. A refused
    # PreCompact really waits out PRECOMPACT_WAIT_S on a real flock, so each
    # refusal arm costs the whole bound. The arms run on a 0.4s bound, and
    # arm (b)'s released hold is a quarter of it, the 1:4 the shipped 2.0s
    # bound has to a 0.5s stamp. The shipped bound is not waited out here,
    # so the refusal helper asserts it still fits under the hook's timeout.
    PRECOMPACT_WAIT_S = 0.4

    def setUp(self):
        super().setUp()
        self.shipped_wait_s = resumeturn.PRECOMPACT_WAIT_S
        bound = mock.patch.object(resumeturn, "PRECOMPACT_WAIT_S",
                                  self.PRECOMPACT_WAIT_S)
        bound.start()
        self.addCleanup(bound.stop)

    @property
    def transcript(self):
        """The fixture transcript's PATH — an accessor that never creates
        or truncates, so a payload can name a file that was deliberately
        removed."""
        return os.path.join(self.tmp, "transcript.jsonl")

    def transcript_file(self):
        """The fixture transcript, seeded once with a turn record so the
        recorded position is never zero; later calls return the path
        untouched."""
        if not os.path.exists(self.transcript):
            self.append_transcript(
                {"type": "user", "timestamp": self.stamp(120),
                 "message": {"role": "user", "content": "the first turn"}})
        return self.transcript

    def stamp(self, ago_s=0.0):
        """A harness-shaped UTC timestamp, `ago_s` seconds ago."""
        return time.strftime("%Y-%m-%dT%H:%M:%S",
                             time.gmtime(time.time() - ago_s)) + ".000Z"

    def append_transcript(self, *records):
        """Append records to the fixture transcript exactly as the harness
        does — one JSON object per line — and return the new size. A
        `bytes` record is appended raw."""
        with open(self.transcript, "ab") as f:
            for rec in records:
                f.write(rec if isinstance(rec, bytes)
                        else json.dumps(rec).encode("utf-8") + b"\n")
        return os.path.getsize(self.transcript)

    def harness_metadata(self):
        """The record shapes measured between a PreCompact and its boundary
        (resumeturn's module docstring): session metadata and a queued
        message, about a kilobyte, the measured median's order."""
        return ({"type": "last-prompt", "lastPrompt": "x" * 200},
                {"type": "mode", "mode": "default"},
                {"type": "permission-mode", "permissionMode": "default"},
                {"type": "queue-operation", "operation": "enqueue",
                 "timestamp": self.stamp(), "content": "y" * 400},
                {"type": "bridge-session", "bridgeSession": {}})

    def compaction_tail(self, summary_bytes=16 * 1024):
        """What a COMPLETED compaction leaves on disk: the boundary record,
        then the summary, sized inside the measured 11.5-30.9 KB."""
        return ({"type": "system", "subtype": "compact_boundary",
                 "timestamp": self.stamp(60),
                 "compactMetadata": {"trigger": "auto", "durationMs": 120000}},
                {"type": "user", "isCompactSummary": True,
                 "timestamp": self.stamp(60),
                 "message": {"role": "user", "content": "s" * summary_bytes}})

    def run_precompact(self, trigger, sid=SID, agent_id=None,
                       transcript=None):
        """The PreCompact hook as hooks.py installs it, payload on stdin ->
        (rc, its stderr). `agent_id` rides the payload the way Claude Code
        sets it inside a subagent; None leaves the key absent, as on the
        main thread. `transcript` defaults to the seeded fixture file; ""
        is the payload that names none."""
        payload = {"session_id": sid, "cwd": self.cwd,
                   "transcript_path": (self.transcript_file()
                                       if transcript is None else transcript),
                   "hook_event_name": "PreCompact", "trigger": trigger}
        if agent_id is not None:
            payload["agent_id"] = agent_id
        err = io.StringIO()
        with mock.patch.object(handoff, "_git", return_value=None), \
                mock.patch.object(inject, "project_for_cwd",
                                  return_value="proj"), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                mock.patch("sys.stdout", io.StringIO()), \
                mock.patch("sys.stderr", err):
            rc = handoff.cmd_handoff(["check", "--hook-json"])
        return rc, err.getvalue()

    def precompact(self, trigger, sid=SID, agent_id=None):
        """`run_precompact` -> the record it wrote (asserted: the producer
        must have run for the arm to mean anything)."""
        rc, _err = self.run_precompact(trigger, sid=sid, agent_id=agent_id)
        self.assertEqual(rc, 0)
        rec = resumeturn._peek("codex")["precompact"]   # MUST-HIT
        self.assertEqual((rec["session"], rec["trigger"], rec["agent"]),
                         (sid, trigger, agent_id))
        st = os.stat(self.transcript)
        self.assertEqual((rec["transcript_len"], rec["transcript_ino"],
                          rec["transcript_dev"]),
                         (st.st_size, st.st_ino, st.st_dev))   # arm (a)
        self.assertGreater(rec["transcript_len"], 0)          # the seed turn
        return rec

    def assert_declined(self, out, needle, said=None):
        """The loud path over an auto record whose binding could not be
        proved: the leg fired exactly as the deliberate control asserts it,
        the record was NOT consumed, and exactly ONE stderr line says which
        proof was missing. Control for every caller: arm (c) and
        `test_the_same_compaction_grown_by_harness_metadata_still_vouches`,
        the same record with the binding intact."""
        self.assertIn("spawned", out)          # the leg fired: positive on out
        self.assert_resumed_as_before(out, said)
        self.assertNotIn("consumed", self.record())
        lines = [l for l in self.last_stderr.splitlines()
                 if "the PreCompact record does not vouch" in l]
        self.assertEqual(len(lines), 1, self.last_stderr)
        self.assertIn(needle, lines[0])
        self.assertIn("treated as deliberate", lines[0])

    def record(self):
        return resumeturn._peek("codex")["precompact"]

    @contextlib.contextmanager
    def state_lock_held(self):
        """THE REAL FAILURE DOOR of every hook-side write: `_mutate_entry`
        takes `fcntl.flock(LOCK_EX | LOCK_NB)` on `state_path() + ".lock"`,
        and a second open file description holding LOCK_EX on that path
        makes it raise BlockingIOError (flock conflicts across open file
        descriptions, in one process as across two). Nothing is mocked:
        the producer and the consumer meet the same refusal a contending
        child's blocking stamp would hand them."""
        path = resumeturn.state_path() + ".lock"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def backdate(self, age_s):
        """Move the record's stamp back by `age_s` — only its clock, the
        record itself stays the producer's."""
        def change(entry):
            entry["precompact"]["at"] = time.time() - age_s
            return None, True
        resumeturn._mutate_entry("codex", change)

    def events(self):
        try:
            with open(pk.events_path(), encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def assert_resumed_as_before(self, out, said=None):
        """The deliberate-compaction leg, exactly as the unchanged control
        `SidechainCompactionTest.test_the_same_compaction_on_the_main_thread_is_byte_identical`
        asserts it: one --deliver child, the seat's own NEXT, the one
        stdout line."""
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("--deliver", self.spawned[0])
        text = self.child_text()
        self.assertIn("land the landreq delivery leg", text)
        self.assertEqual(out, "helm seat resume-turn: spawned — %s\n"
                         % (text if said is None else said))
        self.assertEqual(resumeturn._peek("codex")["mode"], "spawned")

    def compaction(self):
        """The SessionStart payload naming the SAME transcript path the
        PreCompact named — the path only, so an arm that removed the file
        drives a payload pointing at nothing."""
        return self.payload(transcript=self.transcript)

    def test_a_native_autocompaction_injects_nothing_alerts_nobody_and_records_why(self):  # noqa: VACUOUS_ASSERTION — the empty spawn, wake and post lists ARE the door; the arms below fill the same spies through the same runner
        """(c) PreCompact trigger=auto, then SessionStart(compact): the
        harness is continuing the turn itself. No pane input, no wake child,
        no room row, no DM, nothing on the seat's own stdout — and the
        decision is auditable: the state entry says native-auto, spends no
        resume slot, and the ledger carries one seat.resume-turn event."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")                # nothing into the seat's context
        self.assertEqual(self.spawned, [])       # no --deliver, no wake child
        self.assertEqual(self.wake_children(), [])
        self.assertEqual(self.posts, [])         # no room alert, no DM
        entry = resumeturn._peek("codex")
        self.assertEqual(entry["mode"], "native-auto")
        self.assertIn("trigger=auto", entry["detail"])
        self.assertEqual(entry["at"], [])        # no slot spent: nothing injected
        ev = [e for e in self.events() if e["verb"] == "seat.resume-turn"]
        self.assertEqual(len(ev), 1, ev)
        self.assertEqual(ev[0]["target"], "codex")
        self.assertIn("native autocompaction", ev[0]["summary"])

    def test_the_native_decision_is_one_stderr_line(self):  # noqa: VACUOUS_ASSERTION — the one stderr line is the positive; the empty spawn list is the door it replaces
        """The diagnostic the design owes: one line on stderr, none on
        stdout, so an operator tailing the hook sees the decision and the
        seat's context does not."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        err = io.StringIO()
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.record_spawn), \
                mock.patch("sys.stdin",
                           io.StringIO(json.dumps(self.compaction()))), \
                mock.patch("sys.stdout", io.StringIO()), \
                mock.patch("sys.stderr", err):
            self.assertEqual(resumeturn.cmd_resume_turn(["--hook-json"]), 0)
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith(
            "helm seat resume-turn: native-auto — PreCompact said trigger=auto"),
            lines[0])
        self.assertEqual(self.spawned, [])

    def test_a_manual_record_resumes_exactly_as_before(self):
        """(d) A typed or helm-injected /compact says trigger=manual: the
        turn ended, the seat is parked, and the leg fires as the control
        asserts it — one --deliver child carrying the seat's own NEXT."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("manual")
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertIn("spawned", out)          # the leg fired: positive on out
        self.assert_resumed_as_before(out)

    def test_no_record_is_the_loud_path(self):
        """(e) Absence of the record is not proof of a native compaction:
        the PreCompact hook may be uninstalled, timed out, or skipped its
        nonblocking write. The control arm in SidechainCompactionTest covers
        the no-record spawned path unchanged; this one pins that the store
        holds NO precompact record before the leg fires, so the arm above it
        is the record's doing and not the fixture's."""
        self.journal(next_line="land the landreq delivery leg")
        self.assertIsNone(resumeturn._peek("codex"))     # fixture: no record
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertIn("spawned", out)          # the leg fired: positive on out
        self.assert_resumed_as_before(out)

    def test_a_stale_auto_record_is_the_loud_path(self):
        """(f) An auto record older than `precompact_window_s` (SPIRAL_S,
        900s: past the longest measured compaction, under the hours between
        two) describes an EARLIER compaction, so it vouches for nothing."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        self.backdate(resumeturn.precompact_window_s() + 1)
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertIn("spawned", out)          # the leg fired: positive on out
        self.assert_resumed_as_before(out)

    def test_a_fresh_auto_record_inside_the_window_still_vouches(self):  # noqa: VACUOUS_ASSERTION — the native-auto state entry is the positive; empty stdout and spawn list are the door
        """The boundary from the other side, and why the window is SPIRAL_S
        and not DEBOUNCE_S: a compaction measured at up to 190s of wall clock
        must still find its own record. 190s is past DEBOUNCE_S (120s)."""
        self.assertGreater(resumeturn.precompact_window_s(), 190)
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        self.backdate(190)
        rc, out = self.run_installed(self.compaction())
        self.assertEqual((rc, out, self.spawned), (0, "", []))
        self.assertEqual(resumeturn._peek("codex")["mode"], "native-auto")

    def test_an_auto_record_for_another_session_does_not_vouch(self):
        """The negative on an otherwise-valid record: same key (this seat),
        right trigger, fresh — for a DIFFERENT session id. The record vouches
        only for the session it was written for."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto", sid="other-session-0000-4000-8000-000000000002")
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertIn("spawned", out)          # the leg fired: positive on out
        self.assert_resumed_as_before(out)

    def test_a_dry_run_reports_native_auto_and_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the returned action is the positive; dry writes nothing by the module's own law
        """A dry run READS the record and consumes nothing: the same record
        still vouches for the real SessionStart that follows."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        res = resumeturn.hook(self.compaction(), dry=True)
        self.assertEqual(res["action"], "native-auto")
        self.assertNotIn("mode", resumeturn._peek("codex"))
        self.assertNotIn("consumed", self.record())
        self.assertEqual(self.events(), [])

    # -- F1: one PreCompact vouches for exactly one SessionStart ------------

    def test_a_consumed_auto_record_vouches_for_no_second_SessionStart(self):
        """(F1, the consumer) The SessionStart that stays silent on an auto
        record marks it consumed in the same write. A SECOND SessionStart of
        the same session inside the window -- a deliberate /compact whose
        own PreCompact never produced a record -- finds the evidence spent
        and takes the loud path. Control: the first SessionStart, silent
        exactly as arm (c) asserts."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        self.assertNotIn("consumed", rec)
        rc, out = self.run_installed(self.compaction())
        self.assertEqual((rc, out, self.spawned), (0, "", []))   # the control
        consumed = self.record()["consumed"]                     # MUST-HIT
        self.assertGreaterEqual(consumed, rec["at"])
        self.assertEqual(resumeturn._peek("codex")["mode"], "native-auto")
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertIn("spawned", out)          # the leg fired: positive on out
        self.assert_resumed_as_before(out)

    def test_a_record_the_consumer_cannot_consume_does_not_vouch(self):
        """(F1, the consumer's own failure door) The consuming write is
        nonblocking; a lock it cannot take means a record it cannot spend,
        and a record it cannot spend is one it may not act on. The record
        survives unconsumed for the SessionStart that CAN consume it.
        Control: arm (c), the same record with the lock free."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        with self.state_lock_held():
            rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertIn("spawned", out)          # the leg fired: positive on out
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("--deliver", self.spawned[0])
        self.assertIn("land the landreq delivery leg", self.child_text())
        self.assertNotIn("consumed", self.record())

    @contextlib.contextmanager
    def state_lock_held_for(self, seconds):
        """`state_lock_held`, released by a timer thread after `seconds`:
        the contention a deliverer child's blocking stamp really is -- held,
        then gone -- so the producer's bounded wait has something to
        outlast. Real flock, real release from the same open file
        description; nothing mocked."""
        import threading
        path = resumeturn.state_path() + ".lock"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            timer = threading.Timer(seconds, fcntl.flock,
                                    (lock.fileno(), fcntl.LOCK_UN))
            timer.start()
            try:
                yield
            finally:
                timer.cancel()
                timer.join()
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def state_files(self):
        """Every path under the state directory, relative: the producer's
        failure must leave NOTHING beside the store."""
        root = os.path.dirname(resumeturn.state_path())
        return sorted(os.path.relpath(os.path.join(d, f), root)
                      for d, _dirs, files in os.walk(root) for f in files)

    def precompact_hook_timeout(self):
        """The PreCompact hook's own deadline, read from the spec hooks.py
        installs, never transcribed."""
        return next(sp["timeout"] for sp in hooks.SPECS
                    if sp["name"] == "handoff-precompact")

    def refused_manual_precompact(self):
        """A deliberate /compact whose PreCompact write is REFUSED past the
        producer's bound at the real door (`state_lock_held`) -> its stderr.
        Asserted on the way: the hook still exits 0 (fail-open), the bound
        was respected (the wait ran to PRECOMPACT_WAIT_S and ended under
        the hook's own timeout), and the store holds nothing it did not
        hold before -- the record standing before it, untouched, and no
        side file beside the store."""
        rec, files = self.record(), self.state_files()
        started = time.monotonic()
        with self.state_lock_held():
            rc, err = self.run_precompact("manual")
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(elapsed, resumeturn.PRECOMPACT_WAIT_S)
        self.assertLess(elapsed, self.precompact_hook_timeout())
        self.assertLess(self.shipped_wait_s, self.precompact_hook_timeout(),
                        "the shipped bound no longer fits under the hook")
        self.assertTrue(err.startswith(
            "helm handoff check: PreCompact trigger not recorded after"), err)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertEqual(self.record(), rec)             # MUST-HIT: stale
        self.assertEqual(self.state_files(), files)      # nothing lockless
        return err

    def test_the_producer_waits_out_a_short_hold_and_writes(self):
        """(b) The state lock held for a quarter of the producer's bound -- a
        deliverer child's stamp in flight -- and released by a timer: the
        producer's bounded wait outlasts it and the manual record LANDS, so
        the SessionStart that follows is loud. Positive on the elapsed time
        (the wait happened) and on the record (the write happened).
        Control: arm (c) below, the same hold never released."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        hold = resumeturn.PRECOMPACT_WAIT_S / 4
        started = time.monotonic()
        with self.state_lock_held_for(hold):
            rc, err = self.run_precompact("manual")
        elapsed = time.monotonic() - started
        self.assertEqual((rc, err), (0, ""))
        self.assertGreaterEqual(elapsed, 0.8 * hold)      # it waited
        self.assertLess(elapsed, resumeturn.PRECOMPACT_WAIT_S)
        self.assertEqual(self.record()["trigger"], "manual")   # MUST-HIT
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertIn("spawned", out)          # the leg fired: positive on out
        self.assert_resumed_as_before(out)

    def test_the_producer_past_the_bound_says_so_and_writes_nothing(self):
        """(c) The lock held for longer than PRECOMPACT_WAIT_S: the producer
        gives up inside its bound (measured under the hook's timeout, read
        from hooks.SPECS), prints its one line, and writes nothing -- not
        the record, and nothing lockless beside the store. The record
        standing before it stays. Control: arm (b), the same hold released
        inside the bound."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        err = self.refused_manual_precompact()
        self.assertIn("after %.1fs" % resumeturn.PRECOMPACT_WAIT_S, err)
        self.assertIn("BlockingIOError", err)             # the lock's refusal

    def test_RESIDUAL_a_stale_auto_record_read_after_its_compaction_ended_is_loud(self):  # noqa: VACUOUS_ASSERTION — the unconsumed record and the empty spawn list are the door; the positives are the spawned child, the mode and the one stderr line asserted through assert_declined, and the same record binds in the metadata-growth arm
        """THE ROUND-3 RESIDUAL, driven exactly as the reviewer stated it,
        and now recovering. Three refusals in one window, in this order:
        the consumer cannot take the lock at the auto SessionStart (loud,
        and its own recovery stamp is refused too, so no decision is
        recorded); no later decision lands on the entry (the deliverer
        child is a spy here, so its stamps never come); the producer
        cannot take the lock within its bound at a later manual PreCompact,
        so the stale auto record stands. Between that auto compaction and
        the manual one the harness finished the auto compaction: its
        boundary record and its summary were appended to the transcript
        (a summary's worth of bytes, sized as measured). The next
        SessionStart reads the stale record — unconsumed, same session and
        thread, inside the window, newer than no decision — and the
        position binding (clause 5) refuses it: a compaction boundary lies
        after the recorded position, so the record's compaction already
        ended and this one is not it. LOUD, where round 3 was silent over a
        parked seat. Control: arm (c), and the metadata-growth arm, where
        the same record binds."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        with self.state_lock_held():
            rc, out = self.run_installed(self.compaction())
        self.assertIn("spawned", out)                    # consumer refused: loud
        self.assertNotIn("consumed", self.record())
        self.assertNotIn("last_at", resumeturn._peek("codex"))   # no decision
        del self.spawned[:]
        self.refused_manual_precompact()
        self.assertEqual(self.record(), rec)             # the stale auto stands
        grown = self.append_transcript(*self.compaction_tail()) \
            - rec["transcript_len"]
        self.assertGreater(grown, 11 * 1024)             # a summary's worth
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assert_declined(out, "a compaction boundary was appended")
        self.assertEqual(resumeturn._peek("codex")["mode"], "spawned")

    def test_a_record_older_than_the_entrys_last_decision_declines(self):  # noqa: VACUOUS_ASSERTION — the debounce action, the resumed mode and the inline no-last_at control are the positives; the unconsumed record and empty spawn list are the door
        """(f) Clause 4 through the shipped hooks, with the deliverer
        child's REAL accounting landing between the two refusals: the
        child's doors are `_launch_charge` (attempt None, the unnamed
        compaction leg, which records "spawned" with count_it=True and so
        SPENDS a resume slot) and its final `_record(..., count_it=False)`,
        both called here exactly as `child` calls them. The round-3 arm
        resumed with `_record` at its default count_it=False, and the
        reason it could not replay the real accounting is now stated: the
        real charge spends a slot, and a slot spent inside DEBOUNCE_S makes
        the next SessionStart a DEBOUNCE ("same compaction episode"), not
        a spawn. That IS the real outcome — the child typed the directive
        seconds ago, the seat is not parked, and a debounce injects nothing
        and consumes nothing — so this arm asserts it through the
        in-process hook, whose dict names the action, and asserts clause 4
        itself through the predicate: the same record, stamped before
        `last_at`, declines with the transcript bound, and vouches again
        the moment `last_at` is taken off the entry (the inline control).
        The stale record is never consumed and the entry never says
        native-auto."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        with self.state_lock_held():
            rc, out = self.run_installed(self.compaction())
        self.assertIn("spawned", out)                    # consumer refused: loud
        self.assertNotIn("last_at", resumeturn._peek("codex"))
        text = self.child_text()
        self.assertEqual(resumeturn._launch_charge("codex", SID, text, None,
                                                   "codex"), "")   # MUST-HIT
        resumeturn._record("codex", SID, "resumed", "the child's",
                           count_it=False)
        entry = resumeturn._peek("codex")
        self.assertGreater(entry["last_at"], rec["at"])           # MUST-HIT
        self.assertEqual(len(entry["at"]), 1)         # the real charge's slot
        del self.spawned[:]
        self.refused_manual_precompact()
        self.assertEqual(self.record(), rec)             # still unconsumed
        now, tp = time.time(), self.transcript_file()
        control = dict(entry)
        del control["last_at"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIn("trigger=auto", resumeturn.native_autocompaction(
                control, SID, None, now, tp))                    # bound: control
            self.assertEqual(resumeturn.native_autocompaction(
                entry, SID, None, now, tp), "")                  # clause 4 alone
        res = self.run_hook(self.compaction())
        self.assertEqual(res["action"], "debounce")
        self.assertIn("same compaction episode", res["detail"])
        self.assertNotIn("consumed", self.record())
        self.assertEqual(resumeturn._peek("codex")["mode"], "resumed")
        self.assertEqual(self.spawned, [])

    MISSING = object()
    INVALID_SHAPES = (
        # (name, field, value) -- MISSING removes the field; a field named
        # "precompact" replaces the record itself, "last_at" sits on the entry.
        ("record null", "precompact", None),
        ("record a list", "precompact", [{"trigger": "auto"}]),
        ("record a string", "precompact", "auto"),
        ("at missing", "at", MISSING),
        ("at null", "at", None),
        ("at NaN", "at", float("nan")),
        ("at +inf", "at", float("inf")),
        ("at -inf", "at", float("-inf")),
        ("at a string", "at", "0"),
        ("at a bool", "at", True),
        ("trigger missing", "trigger", MISSING),
        ("trigger null", "trigger", None),
        ("trigger a list", "trigger", ["auto"]),
        ("session missing", "session", MISSING),
        ("session null", "session", None),
        ("session an int", "session", 3),
        ("agent missing", "agent", MISSING),
        ("agent an int", "agent", 0),
        ("agent a list", "agent", []),
        ("last_at null", "last_at", None),
        ("last_at NaN", "last_at", float("nan")),
        ("last_at a string", "last_at", "0"),
        # the position (clause 5): absent, null, mistyped or negative, each
        # of the three fields
        ("transcript_len missing", "transcript_len", MISSING),
        ("transcript_len null", "transcript_len", None),
        ("transcript_len a string", "transcript_len", "0"),
        ("transcript_len a bool", "transcript_len", True),
        ("transcript_len a float", "transcript_len", 1.5),
        ("transcript_len negative", "transcript_len", -1),
        ("transcript_ino missing", "transcript_ino", MISSING),
        ("transcript_ino null", "transcript_ino", None),
        ("transcript_dev missing", "transcript_dev", MISSING),
        ("transcript_dev a string", "transcript_dev", "1"),
    )

    def corrupt(self, field, value):
        """The producer's real record, then ONE field changed in the store
        the way only something other than the producer could -> nothing.
        Written through the store's own lock so the shipped hook reads it."""
        def change(entry):
            target = entry if field in ("precompact", "last_at") else entry["precompact"]
            if value is self.MISSING:
                del target[field]
            else:
                target[field] = value
            return None, True
        resumeturn._mutate_entry("codex", change)

    def test_every_present_invalid_field_declines_while_the_valid_record_vouches(self):  # noqa: VACUOUS_ASSERTION — the inline control on the same predicate vouches first; each shape's empty answer is the door
        """(e) The reviewer's present-invalid class applied to the record
        itself. Control first: the producer's own record vouches through
        the predicate. Then each shape in INVALID_SHAPES, one field at a
        time on that same record, declines -- absence and null are two
        shapes each, because `agent` is null on the main thread and must be
        told apart from absent. The `at NaN` shape is also driven through
        the shipped SessionStart hook, because NaN is the value a plain
        `<=` waves through: the leg is loud and the record is not
        consumed."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        now, tp = rec["at"] + 1, self.transcript_file()
        valid = resumeturn._peek("codex")
        self.assertIn("trigger=auto", resumeturn.native_autocompaction(
            valid, SID, None, now, tp))                            # control
        for name, field, value in self.INVALID_SHAPES:
            with self.subTest(name):
                self.corrupt(field, value)
                entry = resumeturn._peek("codex")
                self.assertNotEqual(entry, valid)                  # it landed
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(resumeturn.native_autocompaction(
                        entry, SID, None, now, tp), "", name)
                # restore the producer's record for the next shape
                def restore(e, rec=rec):
                    e.pop("last_at", None)
                    e["precompact"] = dict(rec)
                    return None, True
                resumeturn._mutate_entry("codex", restore)
        self.assertEqual(resumeturn._peek("codex"), valid)
        self.corrupt("at", float("nan"))
        self.assertNotEqual(self.record()["at"], self.record()["at"])  # NaN landed
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertIn("spawned", out)          # the leg fired: positive on out
        self.assert_resumed_as_before(out)
        self.assertNotIn("consumed", self.record())

    # -- F2: the record vouches for the thread that wrote it ----------------

    def test_a_subagent_auto_record_does_not_vouch_for_the_main_thread(self):
        """(F2) A background subagent shares the seat's name, session and
        environ, so its PreCompact lands under the SAME key and overwrites
        the main thread's manual record. Its agent_id rides the record, and
        the main SessionStart (no agent_id) reads a record another thread
        wrote: the loud path. MUST-HIT: the child's write landed (trigger
        auto, agent set), so the loud path is the agent door and not a
        missing record. Control: arm (c), main auto -> main SessionStart."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("manual")
        rec = self.precompact("auto", agent_id="a1b2")
        self.assertEqual((rec["trigger"], rec["agent"]), ("auto", "a1b2"))
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertIn("spawned", out)          # the leg fired: positive on out
        self.assert_resumed_as_before(out)
        self.assertNotIn("consumed", self.record())

    def test_a_main_auto_record_does_not_vouch_for_a_subagent(self):
        """(F2, the other direction) A subagent's SessionStart is refused by
        the sidechain guard before any record is read (SidechainCompactionTest),
        so the predicate is the door to measure: the main thread's auto
        record answers a subagent's identity with nothing, and the main
        thread's own with the reason (the inline control)."""
        rec = self.precompact("auto")
        entry = resumeturn._peek("codex")
        now, tp = rec["at"] + 1, self.transcript_file()
        self.assertIn("trigger=auto", resumeturn.native_autocompaction(
            entry, SID, None, now, tp))
        self.assertEqual(
            resumeturn.native_autocompaction(entry, SID, "a1b2", now, tp), "")

    # -- clause 5: the record is bound to a transcript position -------------

    def test_the_same_compaction_grown_by_harness_metadata_still_vouches(self):  # noqa: VACUOUS_ASSERTION — the native-auto state entry with its consumed stamp and the "transcript stands" detail are the positives; empty stdout and spawn list are the door
        """(b) Between this compaction's PreCompact and its SessionStart the
        harness appends only session metadata and queued messages (the
        measured record shapes, about a kilobyte): the transcript is the
        same file, grown by a little, with no boundary and no turn after
        the recorded position — the record BINDS and the leg is silent.
        Positive on the growth (the file did grow) and on the detail that
        names the binding. Control for every declining arm below."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        grown = self.append_transcript(*self.harness_metadata()) \
            - rec["transcript_len"]
        self.assertGreater(grown, 500)                   # MUST-HIT: it grew
        self.assertLess(grown, 4096)
        rc, out = self.run_installed(self.compaction())
        self.assertEqual((rc, out, self.spawned), (0, "", []))
        entry = resumeturn._peek("codex")
        self.assertEqual(entry["mode"], "native-auto")
        self.assertIn("the transcript stands where it left it", entry["detail"])
        self.assertIn("consumed", self.record())

    def test_a_replaced_transcript_declines(self):  # noqa: VACUOUS_ASSERTION — the spawned child, the mode and the one stderr line through assert_declined are the positives; the unconsumed record is the door, and the metadata-growth arm binds the same record
        """(d) Same path, another file: the transcript was rewritten under
        the seat (a restore, a copy), so the inode at that path is not the
        one PreCompact saw and nothing about its length can be trusted.
        The replacement is created BESIDE the original and moved over it,
        so the new inode is allocated while the old one still exists and
        cannot be a reuse of it. Loud; the line names the inode."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        twin = self.transcript + ".new"
        shutil.copyfile(self.transcript, twin)
        self.assertNotEqual(os.stat(twin).st_ino, rec["transcript_ino"])
        os.replace(twin, self.transcript)
        self.assertEqual(os.path.getsize(self.transcript), rec["transcript_len"])
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assert_declined(out, "inode differs")

    def test_an_unreadable_transcript_declines(self):  # noqa: VACUOUS_ASSERTION — positives through assert_declined; the unconsumed record is the door; control is the metadata-growth arm
        """(d) The SessionStart payload names a path that cannot be read
        (the file is gone): the binding cannot be proved. Loud; the line
        names the read failure. The payload is the same one arm (c) drives,
        so the path — not a fixture difference — is what changed."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        os.remove(self.transcript)
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assert_declined(out, "the transcript cannot be read")

    def test_a_record_without_a_transcript_position_declines(self):  # noqa: VACUOUS_ASSERTION — positives through assert_declined; the removed field is asserted absent as the MUST-HIT of the corruption, the unconsumed record is the door
        """(d) A record the producer could not position — here the field
        is removed from the producer's own record through the store's lock,
        as only something other than the producer could — never vouches,
        whatever the file looks like. Loud; the line names the position."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        self.corrupt("transcript_len", self.MISSING)
        self.assertNotIn("transcript_len", self.record())          # MUST-HIT
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assert_declined(out, "no transcript position")

    def test_a_PreCompact_payload_without_a_transcript_path_records_no_position_and_the_leg_is_loud(self):  # noqa: VACUOUS_ASSERTION — the trigger recorded and the one producer stderr line are the positives; the absent position and unconsumed record are the door; control is precompact()
        """(d, the producer's side) A PreCompact payload naming no
        transcript: the record is still written (the trigger is evidence
        the resume leg reports), WITHOUT a position, and the producer says
        so in one stderr line; the SessionStart that follows declines it.
        Control: `precompact`, whose payload names the file and whose
        record carries the position."""
        self.journal(next_line="land the landreq delivery leg")
        rc, err = self.run_precompact("auto", transcript="")
        self.assertEqual(rc, 0)
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertIn("transcript position not recorded", lines[0])
        self.assertIn("carries no transcript_path", lines[0])
        rec = self.record()
        self.assertEqual(rec["trigger"], "auto")                  # MUST-HIT
        self.assertNotIn("transcript_len", rec)
        self.transcript_file()          # the file exists for the SessionStart
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assert_declined(out, "no transcript position")

    def test_a_SessionStart_payload_without_a_transcript_path_declines(self):  # noqa: VACUOUS_ASSERTION — positives through assert_declined; the unconsumed record is the door; control is the metadata-growth arm
        """(d) The consumer's half missing: a SessionStart payload naming no
        transcript cannot prove the binding. Loud; the line names the
        payload."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        rc, out = self.run_installed(self.payload())    # transcript_path ""
        self.assertEqual(rc, 0)
        # WITHOUT A TRANSCRIPT THE THREAD CANNOT BE ATTRIBUTED (task/2926):
        # the pane leg still carries the seat's NEXT, and the printed line,
        # which the compacting thread reads, is the suppression notice.
        said = resumeturn.lead_suppressed_text(
            "codex", "the payload names no transcript, so this session's "
                     "subagents cannot be read")
        self.assertNotIn("land the landreq delivery leg", said)
        self.assert_declined(out, "SessionStart payload carries no transcript_path",
                             said)

    def test_a_shrunk_transcript_declines(self):  # noqa: VACUOUS_ASSERTION — positives through assert_declined; the unconsumed record is the door; control is the metadata-growth arm
        """(d) The file is shorter than the recorded position: whatever
        happened to it, the position no longer describes it. Loud; the line
        names the shrink."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        os.truncate(self.transcript, rec["transcript_len"] - 1)
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assert_declined(out, "shrank below the recorded position")

    def test_growth_past_the_bound_declines(self):  # noqa: VACUOUS_ASSERTION — the measured over-cap growth and the positives through assert_declined; the unconsumed record is the door
        """(d) The region past the recorded position is larger than
        TRANSCRIPT_GROWTH_CAP: the consumer reads no further, the binding
        is unprovable, and unprovable declines. One queued-message record
        one byte over the cap. Loud; the line names the bound."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        grown = self.append_transcript(
            {"type": "queue-operation", "operation": "enqueue",
             "content": "z" * resumeturn.TRANSCRIPT_GROWTH_CAP}) \
            - rec["transcript_len"]
        self.assertGreater(grown, resumeturn.TRANSCRIPT_GROWTH_CAP)   # MUST-HIT
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assert_declined(out, "over the %d-byte bound"
                             % resumeturn.TRANSCRIPT_GROWTH_CAP)

    def test_a_turn_appended_after_the_record_declines(self):  # noqa: VACUOUS_ASSERTION — positives through assert_declined; the unconsumed record is the door; control is the metadata-growth arm, the same metadata without the turn
        """(d) No boundary after the position, but a turn: the record's
        compaction never ended (the summary failed) and the session moved
        on, so a later compaction reading it is not the one it describes.
        Measured: zero turn records ever land between a PreCompact and its
        own boundary, so a turn in the region is never this compaction's
        own late flush. Loud; the line names the turn."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        self.append_transcript(
            *self.harness_metadata(),
            {"type": "assistant", "timestamp": self.stamp(),
             "message": {"role": "assistant", "content": "a turn"}})
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assert_declined(out, "a turn record (type assistant) was appended")

    def test_an_unparseable_record_after_the_position_declines(self):  # noqa: VACUOUS_ASSERTION — positives through assert_declined; the unconsumed record is the door; control is the metadata-growth arm, the same metadata without the raw line
        """(d) A line in the region that is not a JSON object cannot be
        classified, and a region the consumer cannot classify is
        unprovable. Loud; the line names it."""
        self.journal(next_line="land the landreq delivery leg")
        self.precompact("auto")
        self.append_transcript(*self.harness_metadata(), b"{not a record\n")
        rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assert_declined(out, "an unparseable record was appended")

    def test_observation_exceptions_alert_and_regular_metadata_still_vouches(self):
        """An observation failure declines native-auto, not the whole hook.

        NUL is real valid-JSON input to the real opener. The other arms inject
        unexpected open/read/close exceptions only at the binder's call site.
        A mismatched synthetic spawn register forces the ordinary alert door;
        its real pipe payload, not just rc 0, proves recovery was armed.
        """
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        self.spawn(session="other-session")
        cases = (("nul", None), ("before_open", RuntimeError("open failed")),
                 ("before_read", RuntimeError("read failed")),
                 ("after_close", OSError("close failed")))
        seen = 0
        for stage, error in cases:
            with self.subTest(stage=stage):
                self.spawned.clear()
                self.wake_payloads.clear()
                rec = self.precompact("auto")
                payload = self.compaction()
                if stage == "nul":
                    payload["transcript_path"] += "\x00"
                    boundary = contextlib.nullcontext()
                    needle = "embedded null"
                else:
                    fail = mock.Mock(side_effect=error)
                    boundary = self.transcript_mutated_mid_read(**{stage: fail})
                    needle = str(error)
                with boundary as opens, \
                        mock.patch.object(orcaadopt, "proc_start", return_value=None):
                    rc, out = self.run_installed(payload)
                if stage != "nul":
                    fail.assert_called_once_with()
                    self.assertEqual(len(opens), 1)
                self.assertEqual(rc, 0)
                self.assertIn("alert — spawn register names session", out)
                self.assertEqual(len(self.spawned), 1)
                self.assertIn("--alert-fd", self.spawned[0])
                wakes = self.wake_children()
                self.assertEqual(len(wakes), 1)
                self.assertEqual((wakes[0]["seat"], wakes[0]["session"],
                                  wakes[0]["record_key"], wakes[0]["mode"]),
                                 ("codex", SID, "codex", "alert"))
                self.assertIn("spawn register names session", wakes[0]["reason"])
                self.assertEqual(self.record(), rec)  # no consumption or rewrite
                self.assertEqual(self.last_stderr.count(
                    "the PreCompact record could not be consumed"), 1)
                self.assertIn(needle, self.last_stderr)
                self.assertIn("treated as deliberate", self.last_stderr)
                seen += 1
        self.assertEqual(seen, 4)

        # Opposite pole through the same CLI: a real metadata-only interval
        # is still accepted, rather than making every observation decline.
        self.spawn()
        self.spawned.clear()
        self.wake_payloads.clear()
        rec = self.precompact("auto")
        grown = self.append_transcript(*self.harness_metadata()) - rec["transcript_len"]
        self.assertGreater(grown, 500)
        rc, out = self.run_installed(self.compaction())
        self.assertEqual((rc, out, self.spawned, self.wake_children()),
                         (0, "", [], []))
        self.assertEqual(resumeturn._peek("codex")["mode"], "native-auto")
        self.assertIn("consumed", self.record())
        self.assertIn("the transcript stands", self.last_stderr)

    def test_a_nul_transcript_path_under_a_matching_register_is_still_loud(self):  # noqa: VACUOUS_ASSERTION — the alert line on stdout, the one --alert-fd child and its wake payload are the positives; the --deliver control after the NUL stage fires the leg on the same register
        """CL97 (review on the observation-boundary arm above): the NUL
        stage there lands on the
        alert door only because its register names ANOTHER session. Under
        the production shape -- the register names THIS session, exactly as
        the metadata-growth control's does -- the declined native-auto falls
        through to `resume_text`, whose `handoff.last_compaction` opens the
        same NUL path, and a ValueError there is not an OSError: it escaped
        `hook` to `cmd_resume_turn`'s last-resort catch, which printed it and
        returned 0 with no child and no wake -- the silent seat this lane
        exists to end. The effect owed is the ordinary alert: one wake child
        whose reason carries the error, the record untouched.

        THE NUL STAGE RUNS FIRST, on fresh state: an alert stamps no resume
        (the wake child would, and it is not run here), so the CONTROL that
        follows -- same register, same fixture, a declined record over a
        readable path -- still fires the --deliver leg, which proves the
        register MATCHES. The other order debounced the subject: a control
        that resumes first leaves a stamp that silences the next decision
        (`_decide` -> "debounce"), and that silence is not this defect."""
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        payload = self.compaction()
        payload["transcript_path"] += "\x00"
        with mock.patch.object(orcaadopt, "proc_start", return_value=None):
            rc, out = self.run_installed(payload)
        self.assertEqual(rc, 0)
        self.assertIn("helm seat resume-turn: alert — ", out, self.last_stderr)
        self.assertIn("embedded null", out)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("--alert-fd", self.spawned[0])
        self.assertNotIn("--deliver", self.spawned[0])
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 1)
        self.assertEqual((wakes[0]["seat"], wakes[0]["session"],
                          wakes[0]["record_key"], wakes[0]["mode"]),
                         ("codex", SID, "codex", "alert"))
        self.assertIn("embedded null", wakes[0]["reason"])
        self.assertEqual(self.record(), rec)  # no consumption or rewrite
        self.assertEqual(self.last_stderr.count(
            "the PreCompact record could not be consumed"), 1)
        self.assertIn("embedded null", self.last_stderr)
        self.assertIn("treated as deliberate", self.last_stderr)

        self.spawned.clear()
        self.wake_payloads.clear()
        self.precompact("auto")
        self.append_transcript(*self.compaction_tail())
        with mock.patch.object(orcaadopt, "proc_start", return_value=None):
            rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assert_declined(out, "a compaction boundary was appended")

    # -- F1/F2: ONE regular descriptor, the whole interval, no lock held ----

    @contextlib.contextmanager
    def transcript_mutated_mid_read(self, before_open=None, before_read=None,
                                    after_close=None):
        """THE REAL SEAM between the consumer's observation and its read.

        `pk.open_regular` is the ONE opener the binder uses, so a wrapper
        around it runs `before_open` immediately before the real open (an
        interleaving writer then lands on the path the descriptor is about
        to be taken from) and `before_read` between the fstat that sizes
        the interval and the read that fills it (an interleaving writer
        then lands UNDER a descriptor already measured). NO STAT ANSWER IS
        INVENTED and `os.stat` is not mocked: the real opener opens the
        real file and the real fstat measures the real descriptor; the
        wrapper decides only WHEN the other writer runs.

        IT INSTRUMENTS ONE CALL SITE, not one path. `pk.open_regular` is the
        tree's only regular-file reader, so the store reads and
        `handoff.spoke_since` -- which reads the same transcript later in
        the same hook -- come through it too, and counting those made "the
        binder opened once" read as two. The wrapper therefore acts only on
        the call whose immediate caller is `_observe_transcript`, the
        binder's own frame, and passes every other call straight to the real
        opener. The yielded list counts the binder's opens, so an arm can
        assert the seam ran."""
        import sys
        real, opens, fired = pk.open_regular, [], []

        class Reader:
            """The real file object with its read hooked. A BufferedReader
            carries no attributes of its own, so the binder's four calls
            are delegated rather than patched onto it."""

            def __init__(self, f):
                self.f = f

            def fileno(self):
                return self.f.fileno()

            def seek(self, *a):
                return self.f.seek(*a)

            def read(self, *a):
                if before_read and not fired:
                    fired.append(True)
                    before_read()
                return self.f.read(*a)

            def close(self):
                self.f.close()
                if after_close:
                    after_close()   # inject failure only after releasing the fd

        def opener(path, *a, **kw):
            binder = sys._getframe(1).f_code.co_name == "_observe_transcript"
            if path != self.transcript or not binder:
                return real(path, *a, **kw)
            opens.append(path)
            if before_open and len(opens) == 1:
                before_open()
            return Reader(real(path, *a, **kw))

        with mock.patch.object(pk, "open_regular", opener):
            yield opens

    def test_a_transcript_replaced_between_the_observation_and_the_read_declines(self):  # noqa: VACUOUS_ASSERTION — the counted open, the replacement measured at the recorded length and the one stderr line through assert_declined are the positives; the unconsumed record is the door
        """(F1) The reviewer's falsifier for the pathname/descriptor split.

        The standing record's own compaction has ENDED — its boundary and
        summary lie past the recorded position, so the honest answer is the
        loud path — and a prefix-only regular file of exactly the recorded
        length is moved over the path in the instant between the
        observation and the read. A binder that measures a PATHNAME and
        then opens that path again reads an EMPTY region out of the
        replacement, and an empty region holds no boundary and no turn, so
        the stale auto record vouches and is consumed over a seat nobody
        resumed. THE PROPERTY: identity comes from `os.fstat` of the ONE
        descriptor the bytes come from, so the replacement is refused and
        the line names the inode. Control: the metadata-growth positive,
        the same record with nothing moved under it."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        self.append_transcript(*self.compaction_tail())   # this record is STALE
        twin = self.transcript + ".prefix"
        with open(twin, "wb") as f:
            f.write(b"p" * rec["transcript_len"])
        self.assertNotEqual(os.stat(twin).st_ino, rec["transcript_ino"])
        with self.transcript_mutated_mid_read(
                before_open=lambda: os.replace(twin, self.transcript)) as opens:
            rc, out = self.run_installed(self.compaction())
        self.assertEqual(len(opens), 1)                   # MUST-HIT: the seam ran
        self.assertEqual(os.path.getsize(self.transcript),
                         rec["transcript_len"])   # the vacuous-accept shape
        self.assertEqual(rc, 0)
        self.assert_declined(out, "inode differs")

    def test_a_transcript_truncated_to_the_position_under_the_read_declines(self):  # noqa: VACUOUS_ASSERTION — the counted open, the surviving inode and the one stderr line through assert_declined are the positives; the unconsumed record is the door
        """(F1) The same vacuous acceptance by the other route. The file is
        the one the producer saw and the size the interval was computed
        from was real, and then the SAME inode is truncated back to the
        recorded position between the fstat and the read: the bytes asked
        for are gone by the time they are asked for, and what was truncated
        away is this record's own boundary and summary — the evidence that
        declines it. A region shorter than the interval is a reason, never
        a clean region. Control: the metadata-growth positive, the same
        read with nothing truncated."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        grown = self.append_transcript(*self.compaction_tail()) \
            - rec["transcript_len"]
        self.assertGreater(grown, 0)                      # MUST-HIT: a region
        with self.transcript_mutated_mid_read(
                before_read=lambda: os.truncate(
                    self.transcript, rec["transcript_len"])) as opens:
            rc, out = self.run_installed(self.compaction())
        self.assertEqual(len(opens), 1)                   # MUST-HIT: the seam ran
        self.assertEqual(os.stat(self.transcript).st_ino,
                         rec["transcript_ino"])    # the same file, truncated
        self.assertEqual(rc, 0)
        self.assert_declined(out, "returned 0 of the %d bytes" % grown)

    def test_a_short_read_of_the_region_declines(self):  # noqa: VACUOUS_ASSERTION — the counted open, the measured half-region and the one stderr line through assert_declined are the positives; the unconsumed record is the door
        """(F1) A region that arrives PARTIALLY, which is the shape a torn
        read leaves: the file is truncated mid-region between the fstat and
        the read, so the read returns a prefix of the interval. A prefix
        can be parsed and can hold no boundary and no turn while the bytes
        that do are missing, so the length read must equal the interval the
        descriptor's own size named. Control: the metadata-growth positive,
        whose read returns the whole interval."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        grown = self.append_transcript(*self.compaction_tail()) \
            - rec["transcript_len"]
        half = grown // 2
        self.assertGreater(half, 0)                       # MUST-HIT: a prefix
        with self.transcript_mutated_mid_read(
                before_read=lambda: os.truncate(
                    self.transcript, rec["transcript_len"] + half)) as opens:
            rc, out = self.run_installed(self.compaction())
        self.assertEqual(len(opens), 1)                   # MUST-HIT: the seam ran
        self.assertEqual(rc, 0)
        self.assert_declined(out, "returned %d of the %d bytes" % (half, grown))

    def test_a_fifo_at_the_transcript_path_declines_at_once_and_records_no_position(self):  # noqa: VACUOUS_ASSERTION — the bounded elapsed time, the retaken lock, the recorded trigger and the one stderr line through assert_declined are the positives; the unconsumed record is the door
        """(F2) A FIFO where the transcript belongs, with no writer: a
        blocking open of it never returns. A blocking open inside the
        store's transaction holds the state lock until the hook's own
        timeout kills it, and every other writer — the fallback leg that
        timeout exists to reach included — waits behind it. THE PROPERTY,
        both halves: the consumer opens O_NONBLOCK and fstats before any
        read (`pk.open_regular`), so it declines at once, consumes nothing
        and leaves the lock free, and the producer refuses to position a
        record over a non-regular file at all. EXTERNALLY BOUNDED: the
        decision runs in a joined thread, so a blocking open fails an
        assertion instead of hanging the suite."""
        import threading
        import stat as stat_mod
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        os.remove(self.transcript)
        os.mkfifo(self.transcript)
        self.assertTrue(stat_mod.S_ISFIFO(os.stat(self.transcript).st_mode))
        self.assertEqual(self.record()["transcript_ino"],
                         rec["transcript_ino"])   # MUST-HIT: still positioned
        done = {}

        def run():
            started = time.monotonic()
            done["result"] = self.run_installed(self.compaction())
            done["elapsed"] = time.monotonic() - started

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(30)
        self.assertFalse(th.is_alive(), "the consumer blocked on the FIFO open")
        rc, out = done["result"]
        self.assertEqual(rc, 0)
        self.assertLess(done["elapsed"], 1.0)    # at once, not at a timeout
        self.assert_declined(out, "not a regular file")
        lock_path = resumeturn.state_path() + ".lock"
        with open(lock_path, "a") as lock:       # MUST-HIT: nothing holds it
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        rc, err = self.run_precompact("auto")    # the producer's half
        self.assertEqual(rc, 0)
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertIn("transcript position not recorded", lines[0])
        self.assertIn("not a regular file", lines[0])
        fresh = self.record()
        self.assertEqual(fresh["trigger"], "auto")            # MUST-HIT: written
        self.assertNotIn("transcript_len", fresh)

    def test_the_transcript_is_read_before_the_state_lock_is_taken(self):  # noqa: VACUOUS_ASSERTION — the counted open under the held lock, the spawned child and its text are the positives; the unconsumed record is the door
        """(F2) The ordering itself, and the reason a FIFO can no longer
        wedge the store. With the state lock really held by a second open
        file description, the consumer STILL opens and reads the transcript
        — the seam counts the open — and only the decide-and-consume write
        meets the refusal, which is the loud path. Before the cure that
        open sat inside the refused write, so a held lock meant the
        transcript was never read at all and an unreadable one meant the
        lock was held for as long as the read took. Control: the same
        payload with the lock free is the metadata-growth positive, which
        reads the same region and consumes it."""
        self.journal(next_line="land the landreq delivery leg")
        rec = self.precompact("auto")
        grown = self.append_transcript(*self.harness_metadata()) \
            - rec["transcript_len"]
        self.assertGreater(grown, 500)              # MUST-HIT: a region to read
        with self.transcript_mutated_mid_read() as opens:
            with self.state_lock_held():
                rc, out = self.run_installed(self.compaction())
        self.assertEqual(rc, 0)
        self.assertEqual(len(opens), 1)   # MUST-HIT: read WHILE the lock was held
        self.assertIn("spawned", out)     # the consume refused: the loud path
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("--deliver", self.spawned[0])
        self.assertIn("land the landreq delivery leg", self.child_text())
        self.assertNotIn("consumed", self.record())


class AlertWakeDmTest(ResumeTurnBase):
    """The room alert describes a wedged seat; the wake is the actuator.

    65 alerts in one chat window said "will wake on its next @mention or
    DM" while nothing sent that DM (2026-08-08 overnight burn). The meld
    with codex converged on the whole-object shape these arms pin: the HOOK
    only materializes one trusted reason artifact and detached-spawns; the
    WAKE-CHILD owns proof (identity AND routability AND session binding),
    the transcript-episode debounce, resolver-routed delivery, bounded
    independent legs, and the honest actuation claim (queued-to-armed-route
    is the ceiling; queued bytes with no beacon are not actuation). Hook
    arms assert the spawn and the artifact; child arms drive wake_alert
    directly; composition arms run the spawned payloads end-to-end.
    """

    def recorded(self, calls):
        return mock.patch("helm.chat.post",
                          side_effect=lambda text, **kw:
                          calls.append((text, kw)))

    def armed_beacon(self, armed=True):
        return mock.patch.object(seats, "beacon_procs",
                                 return_value=([123] if armed else [], None))

    def dms(self, calls):
        # chat.post receives dm= as the CANONICAL recipient object, not the
        # raw token — normalize before comparing.
        return [(t, str(kw["dm"])) for t, kw in calls if kw.get("dm")]

    def rooms(self, calls):
        return [t for t, kw in calls if not kw.get("dm")]

    def chat_hits(self, needle):
        """Every chat-store file containing `needle` — the artifact, not a
        recorder. Returns (paths, dm_paths)."""
        root = os.environ["HELM_CHAT_DIR"]
        hits = []
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        if needle in fh.read():
                            hits.append(p)
                except OSError:
                    pass
        return hits, [p for p in hits if os.sep + "dm" in p]

    def wake_payload(self, seat="codex", session=SID, reason="wake up",
                     episode="ep1", display=None):
        return {"display": display or seat, "reason": reason,
                "seat": seat, "session": session, "episode": episode}

    def transcript(self, body="turn one"):
        p = os.path.join(self.tmp, "transcript.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p

    # ---- hook side: arm the wake, never perform it -----------------------

    def test_a_register_mismatch_arms_exactly_one_wake_child(self):
        """The 65-count class: the hook's whole contract is now ONE spawned
        wake-child carrying a hook-authored artifact that binds seat AND
        session together (meld finding 6)."""
        why = "spawn register names session aaaa but this compaction is bbbb"
        with mock.patch.object(inject, "project_for_cwd", return_value="p"), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.record_spawn), \
                mock.patch.object(resumeturn, "_registered",
                                  return_value=(why, [])):
            out = resumeturn.hook(self.payload(
                transcript=self.transcript()))
        self.assertEqual(out["action"], "alert")
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 1)
        self.assertEqual((wakes[0]["seat"], wakes[0]["session"]),
                         ("codex", SID))
        self.assertIn("register", wakes[0]["reason"])
        self.assertTrue(wakes[0]["episode"], "the hook stamps the episode")
        # composition: the payload actually wakes through the real seams
        calls = []
        self.run_wakes(calls)
        self.assertEqual([d for _, d in self.dms(calls)], ["codex"])
        self.assertEqual(len(self.rooms(calls)), 1)

    def test_capped_and_suppression_paths_carry_no_dm_authority(self):
        """spiral/capped means STOP poking the seat, and the suppression
        path runs inside the seat's own hook (awake by construction). Their
        artifacts carry an empty seat, so no child can mint a wake from
        them — the authority is absent, not merely unused."""
        from helm.inject import _ledger
        with mock.patch.object(inject, "project_for_cwd", return_value="p"), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.record_spawn), \
                mock.patch.object(_ledger, "forget_session",
                                  return_value=False), \
                mock.patch.object(resumeturn, "_decide",
                                  return_value=("capped", "5 in window")):
            out = resumeturn.hook(self.payload())
        self.assertEqual(out["action"], "capped")
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 2)     # suppression row + capped row
        self.assertTrue(all(not w["seat"] for w in wakes), wakes)
        calls = []
        self.run_wakes(calls)
        self.assertEqual(self.dms(calls), [])
        self.assertEqual(len(self.rooms(calls)), 2)

    def test_the_hook_never_touches_chat_or_dm_lanes_in_process(self):
        """The founding invariant, applied to the alert path: the hook
        writes one artifact and spawns. Any in-process chat call is the
        wedge class returning."""
        calls = []
        why = "spawn register names session aaaa but this compaction is bbbb"
        with mock.patch.object(inject, "project_for_cwd", return_value="p"), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.record_spawn), \
                mock.patch.object(resumeturn, "_registered",
                                  return_value=(why, [])), \
                self.recorded(calls):
            resumeturn.hook(self.payload())
        self.assertEqual(calls, [],
                         "the hook must not perform chat I/O in-process")

    def test_a_dead_state_store_still_arms_the_wake(self):
        """The artifact falls back to the tempdir when the state dir is
        dead; the wake is armed either way and the hook does not raise."""
        state_dir = os.path.dirname(resumeturn.state_path())
        os.makedirs(state_dir, exist_ok=True)
        os.chmod(state_dir, 0o500)
        try:
            why = ("spawn register names session aaaa but this compaction "
                   "is bbbb")
            with mock.patch.object(inject, "project_for_cwd",
                                      return_value="p"), \
                    mock.patch.object(resumeturn, "spawn_child",
                                      side_effect=self.record_spawn), \
                    mock.patch.object(resumeturn, "_registered",
                                      return_value=(why, [])):
                out = resumeturn.hook(self.payload())
        finally:
            # restore BEFORE tearDown's rmtree — addCleanup runs after it
            os.chmod(state_dir, 0o700)
        self.assertEqual(out["action"], "alert")
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 1)
        self.assertEqual(wakes[0]["seat"], "codex")

    def test_a_failed_spawn_loses_loudly_and_never_blocks(self):
        """Meld correction 3: there is NO inline fallback — an unbounded
        flock inline is exactly the wedge. The loss is narrated on guarded
        stderr plus one O_APPEND breadcrumb, and no chat I/O happens."""
        calls = []
        with mock.patch.object(tempfile, "tempdir", self.tmp), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=OSError("fork refused")), \
                self.recorded(calls):
            ok = resumeturn._alert("codex", "lost wake", dm_seat="codex",
                                   dm_session=SID)
        self.assertFalse(ok)
        self.assertEqual(calls, [])
        log = os.path.join(self.tmp, "helm-wake-lost.log")
        with open(log, encoding="utf-8") as f:
            self.assertIn("lost wake", f.read())

    def test_record_failure_returns_none_and_never_raises(self):
        state_dir = os.path.dirname(resumeturn.state_path())
        os.makedirs(state_dir, exist_ok=True)
        os.chmod(state_dir, 0o500)
        try:
            self.assertIsNone(
                resumeturn._record("k", SID, "alert", "d", count_it=True))
        finally:
            os.chmod(state_dir, 0o700)

    # ---- the episode debounce (meld corrections 1 + 5) -------------------

    def test_a_replayed_payload_same_episode_wakes_once(self):
        """A replayed delivery of the SAME compaction repeats the episode
        (untouched transcript) and must not wake twice; every room row
        still narrates."""
        tp = self.transcript()
        why = "spawn register names session aaaa but this compaction is bbbb"
        with mock.patch.object(inject, "project_for_cwd", return_value="p"), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.record_spawn), \
                mock.patch.object(resumeturn, "_registered",
                                  return_value=(why, [])):
            resumeturn.hook(self.payload(transcript=tp))
            resumeturn.hook(self.payload(transcript=tp))
        calls = []
        self.run_wakes(calls)
        self.assertEqual(len(self.dms(calls)), 1, calls)
        self.assertEqual(len(self.rooms(calls)), 2)

    def test_a_genuine_second_compaction_wakes_again(self):
        """One session CAN compact twice inside the window (codex, meld):
        each compaction rewrites the transcript, the episode changes, and
        the second wake is due. This is the arm that kills a seat+sid key."""
        tp = self.transcript("after the first compaction")
        why = "spawn register names session aaaa but this compaction is bbbb"
        with mock.patch.object(inject, "project_for_cwd", return_value="p"), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.record_spawn), \
                mock.patch.object(resumeturn, "_registered",
                                  return_value=(why, [])):
            resumeturn.hook(self.payload(transcript=tp))
            with open(tp, "w", encoding="utf-8") as f:
                f.write("rewritten by the second compaction — longer body")
            resumeturn.hook(self.payload(transcript=tp))
        calls = []
        self.run_wakes(calls)
        self.assertEqual(len(self.dms(calls)), 2, calls)

    # ---- the wake-child: proof, routability, honest claim ----------------

    def test_wake_alert_dms_the_proved_seat(self):
        calls = []
        with self.armed_beacon(), self.recorded(calls):
            ok = resumeturn.wake_alert(self.wake_payload())
        self.assertTrue(ok)
        self.assertEqual([d for _, d in self.dms(calls)], ["codex"])
        self.assertIn("wake up", self.dms(calls)[0][0])
        self.assertEqual(len(self.rooms(calls)), 1)

    def test_an_unproved_candidate_stays_room_only(self):
        """Caller text is not evidence: an arbitrary token earns no lane."""
        calls = []
        with self.armed_beacon(), self.recorded(calls):
            resumeturn.wake_alert(self.wake_payload(seat="victim-seat"))
        self.assertEqual(self.dms(calls), [])
        self.assertEqual(len(self.rooms(calls)), 1)

    def test_env_or_pipe_alone_cannot_mint_a_wake(self):
        """A pipe is transport, not authorship (the reviewer's forged-FIFO
        probe minted a DM): with the register unable to bind the session
        and no holder evidence, the wake refuses — descriptor type and env
        equality grant nothing."""
        calls = []
        with self.armed_beacon(), self.recorded(calls):
            resumeturn.wake_alert(
                self.wake_payload(session="unrelated-session"))
        self.assertEqual(self.dms(calls), [])

    def test_holder_kernel_evidence_routes_when_the_register_cannot(self):
        """The 65-class fires precisely when the register cannot bind the
        session; the seat's own live process — pid:starttime re-proved via
        /proc with an environ claiming the seat — is the evidence that
        remains."""
        holder = self.plant_holder("codex")
        calls = []
        with self.armed_beacon(), self.recorded(calls):
            resumeturn.wake_alert(dict(
                self.wake_payload(session="unrelated-session"),
                holder=holder))
        self.assertEqual([d for _, d in self.dms(calls)], ["codex"])

    def test_a_holder_claiming_another_seat_is_refused(self):
        holder = self.plant_holder("some-other-seat")
        calls = []
        with self.armed_beacon(), self.recorded(calls):
            resumeturn.wake_alert(dict(
                self.wake_payload(session="unrelated-session"),
                holder=holder))
        self.assertEqual(self.dms(calls), [])

    def test_an_oversize_reason_is_clipped_not_wedged(self):
        """The reviewer's 1MiB reason blocked inside os.write and the child
        never spawned. The payload is clipped to the PIPE_BUF atomic bound
        and the wake still arms."""
        with mock.patch.object(resumeturn, "spawn_child",
                               side_effect=self.record_spawn):
            ok = resumeturn._alert("codex", "X" * (1024 * 1024),
                                   dm_seat="codex", dm_session=SID)
        self.assertTrue(ok)
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 1)
        self.assertLessEqual(len(wakes[0]["reason"]), 1500)

    def test_the_hook_never_takes_a_blocking_state_lock(self):
        """A held LOCK_EX wedged SessionStart even on the successful resume
        path (measured by the reviewer). The hook's only state touch is the
        NONBLOCKING stamp (_record_nb — LOCK_NB, skip-on-contention); the
        blocking _record belongs to the deliverer and the wake-child. Both
        halves pinned: zero blocking calls, and the nonblocking stamp does
        land on the happy path (the double-fire arm depends on it)."""
        recs, stamps = [], []
        real_nb = resumeturn._record_nb
        with mock.patch.object(inject, "project_for_cwd", return_value="p"), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.record_spawn), \
                mock.patch.object(resumeturn, "_record",
                                  side_effect=lambda *a, **k:
                                  recs.append(a)), \
                mock.patch.object(resumeturn, "_record_nb",
                                  side_effect=lambda *a, **k:
                                  (stamps.append(a), real_nb(*a, **k))[1]):
            out = resumeturn.hook(self.payload())
        self.assertEqual(out["action"], "spawned")
        self.assertEqual(recs, [], "no blocking lock in the hook, ever")
        self.assertEqual(len(stamps), 1, "the nonblocking stamp must land")

    def test_a_renamed_seat_is_refused_by_the_roster(self):
        """Meld finding 1: rename_seat leaves the live process's env stale
        while the roster and lane move — identity without routability. A
        positively-ABSENT roster entry refuses; we never auto-reroute on an
        alias suggestion."""
        calls = []
        with self.armed_beacon(), self.recorded(calls), \
                mock.patch.object(seats, "recipient_capability",
                                  return_value={"membership": "ABSENT",
                                                "evidence": "populated"}):
            resumeturn.wake_alert(self.wake_payload(seat="codex"))
        self.assertEqual(self.dms(calls), [])
        self.assertEqual(len(self.rooms(calls)), 1)

    def test_unknown_roster_evidence_never_refuses(self):
        calls = []
        with self.armed_beacon(), self.recorded(calls), \
                mock.patch.object(seats, "recipient_capability",
                                  return_value={"membership": "UNKNOWN",
                                                "evidence": "read-failed"}):
            resumeturn.wake_alert(self.wake_payload(seat="codex"))
        self.assertEqual([d for _, d in self.dms(calls)], ["codex"])

    def test_queued_bytes_without_a_beacon_are_not_actuation(self):
        """Meld correction 4: a DM row plus a measured armed beacon is
        "queued to an armed route"; with no live beacon the dm leg claims
        nothing, so a dead room makes the whole wake honestly False."""
        def room_dead(text, **kw):
            if not kw.get("dm"):
                raise OSError("room node down")
        with self.armed_beacon(armed=False), \
                mock.patch("helm.chat.post", side_effect=room_dead):
            ok = resumeturn.wake_alert(self.wake_payload())
        self.assertFalse(ok)

    def test_an_armed_route_is_a_claimable_outcome(self):
        def room_dead(text, **kw):
            if not kw.get("dm"):
                raise OSError("room node down")
        with self.armed_beacon(armed=True), \
                mock.patch("helm.chat.post", side_effect=room_dead):
            ok = resumeturn.wake_alert(self.wake_payload())
        self.assertTrue(ok)

    def test_a_resolver_refusal_is_loud_and_mints_no_lane(self):
        """The stale-token case: the resolver's refusal must be honored —
        no dm/<stale>.jsonl minted — because chat.post(dm=raw) would have
        faithfully recreated the abandoned lane: recorded delivery, no
        wake."""
        from helm import seats_delivery
        with self.armed_beacon(), \
                mock.patch.object(seats_delivery, "resolve_recipient",
                                  return_value=(None,
                                                "known alternate of codex2")):
            ok = resumeturn.wake_alert(
                self.wake_payload(reason="renamed seat wake"))
        self.assertTrue(ok)          # the room row still landed
        hits, dm_hits = self.chat_hits("renamed seat wake")
        self.assertTrue(hits, "the room row must exist in the real store")
        self.assertEqual(dm_hits, [], "a refused token must mint NO lane")

    def test_the_wake_lands_in_the_real_lane_end_to_end(self):
        """No mocks below wake_alert except the beacon measurement: the
        wake must be findable in the actual chat store, in a dm lane —
        the artifact, not a recorder (a mocked seam is how the routing
        blocker hid)."""
        with self.armed_beacon():
            ok = resumeturn.wake_alert(
                self.wake_payload(reason="end to end wake"))
        self.assertTrue(ok)
        _hits, dm_hits = self.chat_hits("end to end wake")
        self.assertTrue(dm_hits,
                        "the wake must land in a real dm lane file")

    def test_a_seat_actually_named_resume_turn_still_wakes(self):
        """The DM sender is resume-turn/wake — "/" cannot appear in a
        joinable seat token, so a seat legitimately named resume-turn can
        never collide with the sender and its wake delivers."""
        os.environ["HELM_CHAT_NAME"] = "resume-turn"
        holder = self.plant_holder("resume-turn")
        with self.armed_beacon():
            ok = resumeturn.wake_alert(dict(
                self.wake_payload(seat="resume-turn",
                                  reason="collision wake"),
                holder=holder))
        self.assertTrue(ok)
        _hits, dm_hits = self.chat_hits("collision wake")
        self.assertTrue(dm_hits)

    def test_the_legs_are_independent_under_a_stalled_dm(self):  # noqa: VACUOUS_ASSERTION — the recorded room row positively controls the bounded stalled-DM result
        """Meld correction 2: moving the work to the child removes the
        hook-budget coupling but not lane coupling — a stalled DM leg must
        not starve the room row, and the child returns at its bound.

        The ratios are the arm: the DM stalls for up to twice the join bound
        and the child must be back by 1.75 of it, so a child that waited for
        the DM cannot pass and one that returned at its bound cannot fail.

        THE STALLED LEG ENDS INSIDE THIS ARM. wake_alert abandons it at the
        join bound, still running, and its refusal line prints when the
        stall ends. Left alone, that line lands in whichever later test is
        capturing stderr at the time, so the arm releases the stall and
        joins the leg before it returns."""
        import threading
        calls, legs = [], []
        join = 0.5
        release = threading.Event()

        def slow_dm(to, text, **kw):
            legs.append(threading.current_thread())
            release.wait(2 * join)
            return None, "too slow to matter"

        started = time.time()
        with mock.patch.dict(os.environ,
                             {"HELM_RESUME_TURN_WAKE_JOIN_S": str(join)}), \
                mock.patch.object(seats, "dm", side_effect=slow_dm), \
                self.recorded(calls):
            ok = resumeturn.wake_alert(self.wake_payload())
        elapsed = time.time() - started
        release.set()
        for leg in legs:
            leg.join(5)
        self.assertEqual(len(legs), 1, "fixture: the DM leg never ran")
        self.assertLess(elapsed, 1.75 * join,
                        "the child must return at its join bound")
        self.assertTrue(ok, "the room row must land despite the stall")
        self.assertEqual(len(self.rooms(calls)), 1)

    def test_the_alert_capability_is_a_pipe_and_one_shot(self):
        """The wake capability is an inherited FIFO: a regular file at the
        flag is refused (the documented door cannot be pointed at crafted
        caller text), a drained/replayed pipe reads EOF and exits quietly,
        and one payload yields exactly one wake."""
        ran = []
        with mock.patch.object(resumeturn, "wake_alert",
                               side_effect=lambda payload:
                               ran.append(payload) or True):
            p = os.path.join(self.tmp, "crafted.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self.wake_payload(), f)
            fd = os.open(p, os.O_RDONLY)
            self.assertEqual(
                resumeturn.cmd_resume_turn(["--alert", "--alert-fd",
                                            str(fd)]), 1,
                "a regular file must be refused — not an inherited pipe")
            r, w = os.pipe()
            os.write(w, json.dumps(self.wake_payload()).encode("utf-8"))
            os.close(w)
            self.assertEqual(
                resumeturn.cmd_resume_turn(["--alert", "--alert-fd",
                                            str(r)]), 0)
            r2, w2 = os.pipe()
            os.close(w2)
            self.assertEqual(
                resumeturn.cmd_resume_turn(["--alert", "--alert-fd",
                                            str(r2)]), 0,
                "a drained pipe is a replay: quiet success, no wake")
        self.assertEqual(len(ran), 1, "one payload, one wake")

    def test_an_equal_length_rewrite_is_a_new_episode(self):
        """(mtime, size) alone collides under a restored mtime with an
        equal-length rewrite; dev/ino/ctime join the identity and ctime
        cannot be set from userspace."""
        p = os.path.join(self.tmp, "t.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("AAAA")
        st = os.stat(p)
        e1 = resumeturn._episode(p)
        q = os.path.join(self.tmp, "t2.jsonl")
        with open(q, "w", encoding="utf-8") as f:
            f.write("BBBB")                    # same length
        os.replace(q, p)                       # new inode at the same path
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))   # restored mtime
        e2 = resumeturn._episode(p)
        self.assertNotEqual(e1, e2)

    def test_the_child_records_the_bookkeeping_the_hook_no_longer_takes(self):
        """The record key rides the payload; the CHILD takes the state
        lock. The hook side of the register-why path holds no lock at
        all."""
        calls = []
        with self.armed_beacon(), self.recorded(calls):
            resumeturn.wake_alert(dict(self.wake_payload(),
                                       record_key="codex", mode="alert"))
        entry = resumeturn._peek("codex")
        self.assertEqual(entry["mode"], "alert")
        self.assertEqual(entry["detail"], "wake up")


class WithdrawalTest(unittest.TestCase):
    """The pane copy is WITHDRAWN once the hook's own channel is proven.

    A compaction is announced twice — the SessionStart hook prints the
    directive and CC injects hook stdout as context, and separately this
    child types it into the pane — and neither leg knew the other existed.
    These arms are about the second leg declining.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-withdraw-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.transcript = os.path.join(self.tmp, "t.jsonl")
        os.environ["HELM_RESUME_TURN_WITHDRAW_S"] = "0"
        # tearDown-STYLE, NOT addCleanup, AND THE REASON IS THE GUARD.
        # tests/test_env_hygiene scans for a CALL of os.environ.pop;
        # in `addCleanup(os.environ.pop, KEY, None)` the pop is a
        # REFERENCE, so the scan cannot see it and accuses this module
        # of leaking a variable it does restore. The scanner blindness
        # is real and filed separately; this lane takes the shape the
        # guard can read rather than widening the guard mid-train.
        self.addCleanup(self._drop_withdraw_env)

    def _drop_withdraw_env(self):
        os.environ.pop("HELM_RESUME_TURN_WITHDRAW_S", None)

    def write(self, *lines):
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def rec(self, kind, at):
        return json.dumps({"type": kind, "timestamp": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))})

    def run_child(self, since):
        """child() with the ONE seam that types into a pane recorded."""
        calls = []
        with mock.patch.object(resumeturn, "deliver",
                               side_effect=lambda *a, **k:
                               (calls.append((a, k)),
                                _mode_for_delivered())[1]), \
                mock.patch.object(resumeturn, "_record"), \
                mock.patch.object(resumeturn, "_wire_text",
                                  return_value="wire"):
            mode, detail = resumeturn.child(
                "seat-under-test", SID, "the directive", 0,
                transcript=self.transcript, since=since)
        return mode, detail, calls

    def test_absence_of_evidence_costs_no_time(self):
        """THE ARM NOBODY HAD, BECAUSE NOBODY THOUGHT ABSENCE COULD COST TIME.

        `spoke_since` answers None instantly when there is nothing to read, so
        polling for it spends the WHOLE deadline on structurally guaranteed
        answers. Every caller predating the withdrawal passes no transcript,
        which is how a 30s block reached paths that used to return promptly.
        The deadline here is set HIGH on purpose: a regression cannot pass
        this arm quickly, it can only hang it.
        """
        for why, tr, since in (("neither", None, None),
                               ("empty path", "", time.time()),
                               ("no since", self.transcript, None)):
            with self.subTest(missing=why):
                began = time.time()
                got = resumeturn._await_or_withdraw(tr, since, 0, deadline=30.0)
                self.assertEqual(got, (False, None))
                self.assertLess(time.time() - began, 1.0)
        # THE UNCONDITIONAL POSITIVE CONTROL, on the SAME observable and in
        # the same arm: a source that could still speak MUST spend its budget.
        # Without it, `return False, None` at the top of the function satisfies
        # every assertion above while silently deleting the withdrawal.
        # The deadline is short because spending it IS the measurement: an
        # early return takes microseconds, far under 0.8 of any deadline.
        began = time.time()
        got = resumeturn._await_or_withdraw(
            os.path.join(self.tmp, "not-yet.jsonl"), time.time(), 0,
            deadline=0.2)
        self.assertEqual(got, (False, None))
        self.assertGreater(time.time() - began, 0.16)

    def test_a_seat_that_spoke_after_the_compaction_gets_no_pane_copy(self):
        """A PAIR ON ONE TRANSCRIPT, ONE `since` APART. The only difference
        between the two halves is whether the assistant record falls after
        the moment the hook fired, so nothing but the withdrawal predicate
        can explain the difference in outcome."""
        now = time.time()
        self.write(self.rec("assistant", now))
        mode, detail, calls = self.run_child(now - 60)
        self.assertEqual(mode, "withdrawn", detail)
        self.assertEqual(calls, [], "the pane was typed into anyway")
        self.assertIn("withdrawn", detail)
        # THE POSITIVE CONTROL, and it is the whole arm: without it a
        # `deliver` that never runs for an unrelated reason reads as a
        # successful withdrawal. Same file, same child, `since` moved past
        # the record.
        mode, _d, calls = self.run_child(now + 60)
        self.assertEqual(mode, "resumed")
        self.assertEqual(len(calls), 1, "the control did not type at all")

    def test_the_hooks_own_output_can_never_prove_the_seat_woke(self):
        """THE VACUITY THIS CURE IS ONE LINE AWAY FROM. The hook's stdout
        lands in the transcript too — as `attachment` and `user` records —
        so a check that accepted any post-hook record would be satisfied by
        the hook proving itself, on a seat that never woke, every time."""
        now = time.time()
        self.write(self.rec("attachment", now + 1),
                   self.rec("user", now + 2),
                   self.rec("system", now + 3))
        mode, detail, calls = self.run_child(now)
        self.assertEqual(mode, "resumed", detail)
        self.assertEqual(len(calls), 1,
                         "hook-authored records suppressed the delivery")

    def test_an_unreadable_transcript_delivers_rather_than_withdraws(self):  # noqa: VACUOUS_ASSERTION — every assertion here is POSITIVE (a delivery happened); there is no absence claim. The subTest loop varies only WHICH unreadable input is fed, and each iteration asserts len(calls) == 1
        """UNKNOWN IS NOT PROOF. Every way of failing to establish that the
        seat woke must end with the directive reaching the pane; the seat
        this leg exists to rescue is exactly the one whose evidence is
        missing."""
        for why, path in (("absent", os.path.join(self.tmp, "gone.jsonl")),
                          ("empty path", ""), ("None", None)):
            with self.subTest(transcript=why):
                calls = []
                with mock.patch.object(
                        resumeturn, "deliver",
                        side_effect=lambda *a, **k:
                        (calls.append(a), _mode_for_delivered())[1]), \
                        mock.patch.object(resumeturn, "_record"), \
                        mock.patch.object(resumeturn, "_wire_text",
                                          return_value="wire"):
                    mode, _d = resumeturn.child(
                        "seat-under-test", SID, "the directive", 0,
                        transcript=path, since=time.time() - 60)
                self.assertEqual(mode, "resumed")
                self.assertEqual(len(calls), 1)

    def test_a_since_the_caller_could_not_supply_never_withdraws(self):
        """A child spawned by an older hook carries no `--since`. It must
        behave exactly as it did before this cure existed rather than
        withdrawing on a comparison against None."""
        now = time.time()
        self.write(self.rec("assistant", now))
        calls = []
        with mock.patch.object(resumeturn, "deliver",
                               side_effect=lambda *a, **k:
                               (calls.append(a),
                                _mode_for_delivered())[1]), \
                mock.patch.object(resumeturn, "_record"), \
                mock.patch.object(resumeturn, "_wire_text",
                                  return_value="wire"):
            mode, _d = resumeturn.child("seat-under-test", SID, "d", 0,
                                        transcript=self.transcript,
                                        since=None)
        self.assertEqual(mode, "resumed")
        self.assertEqual(len(calls), 1)


class SpokeSinceTest(unittest.TestCase):
    """handoff.spoke_since — the tri-state the withdrawal reads."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-spoke-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.p = os.path.join(self.tmp, "t.jsonl")

    def stamp(self, at):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))

    def test_a_buried_compaction_boundary_cannot_make_this_answer_no(self):  # noqa: VACUOUS_ASSERTION — the control IS unconditional and on the same observable — the same file with `since` moved past the record must answer False; the rung reads assertIs(..., False) as an absence claim, but False here is a VERDICT, not an absence
        """THE DRAFT THIS REPLACED WOULD HAVE BEEN INERT IN PRODUCTION. It
        searched the bounded tail for a compact_boundary; measured, ZERO of
        86 real transcripts on this host carry one inside 512KB, because a
        session that compacts and then works buries it. Reading `since` from
        the CALLER removes the dependency entirely — a transcript with no
        boundary at all still answers True."""
        now = time.time()
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant",
                                "timestamp": self.stamp(now)}) + "\n")
        self.assertIs(handoff.spoke_since(self.p, now - 60), True)
        # THE CONTROL THE RUNG ASKED FOR, on the same observable: move `since`
        # past the record and the same file must answer False, so True above
        # is a verdict rather than this reader's only reachable answer.
        self.assertIs(handoff.spoke_since(self.p, now + 60), False)

    def test_an_unstamped_assistant_record_is_unknown_not_false(self):  # noqa: VACUOUS_ASSERTION — the control IS unconditional and on the same observable — the same record re-written WITH a timestamp must answer True, so the None above cannot be this reader's only reachable answer
        """An instrument that could not read the clock reports that it could
        not, so the caller keeps delivering.

        A PAIR ON ONE OBSERVABLE, ONE FIELD APART. `None` alone would pass on
        a reader that had simply failed to find the record at all, so the
        control writes the SAME record WITH a timestamp and requires a real
        verdict out of it. Only the missing clock can explain the difference.
        """
        now = time.time()
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant"}) + "\n")
        self.assertIsNone(handoff.spoke_since(self.p, now))
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant",
                                "timestamp": self.stamp(now)}) + "\n")
        self.assertIs(handoff.spoke_since(self.p, now - 60), True)

    def test_a_zone_offset_carries_its_meaning(self):
        """ACCEPTED AND UNDERSTOOD MUST BE THE SAME SET.

        The hand-rolled grammar this replaces ACCEPTED zone offsets and
        fractional seconds and then discarded them, so `12:00+05:00` was read
        as `12:00Z` — five hours wrong, in the direction that makes an old
        record look current and WITHDRAWS a delivery. A review answered the
        scope question NO for exactly this (7ac39e0f704e): widening what the
        reader accepts without widening what it can interpret is the same
        defect as reading the payload, one layer down.

        A record four hours old written in +05:00 must read as four hours
        old. The control is the SAME offset on a CURRENT record, so False
        cannot come from offsets being rejected wholesale.
        """
        import datetime as _dt
        utc = _dt.datetime.now(_dt.timezone.utc)
        since = (utc - _dt.timedelta(minutes=1)).timestamp()

        def at(when, hours):
            zoned = when.astimezone(_dt.timezone(_dt.timedelta(hours=hours)))
            text = zoned.strftime("%Y-%m-%dT%H:%M:%S%z")
            return text[:-2] + ":" + text[-2:]

        old = utc - _dt.timedelta(hours=4)
        for label, value, want in (
                ("+05:00 four hours ago", at(old, 5), False),
                ("-08:00 four hours ago", at(old, -8), False),
                ("+05:00 now", at(utc, 5), True),
                ("fractional Z now",
                 utc.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z", True),
                ("fractional Z four hours ago",
                 old.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z", False),
                ("naive is read as UTC", utc.strftime("%Y-%m-%dT%H:%M:%S"),
                 True),
        ):
            with self.subTest(stamp=label):
                with open(self.p, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "assistant",
                                        "timestamp": value}) + "\n")
                self.assertIs(handoff.spoke_since(self.p, since), want)

    def test_a_malformed_top_level_timestamp_is_skipped_never_raised(self):
        """CLAUSE 3 OF THE AGREED INVARIANT: a record whose top level lacks a
        directly parseable timestamp is NOT EVIDENCE — skipped, never matched,
        never raising.

        Two shapes, both measured in review (d4ac3c08ad12) against a cure
        that re-ran the LINE regex over a string built around the extracted
        value. That is a parser fed an input the function CONSTRUCTED from
        untrusted data, so a value carrying its own quotes escaped into faux
        JSON and the injected stamp won; and a value shaped like a date but
        impossible raised out of a helper whose contract is to answer None.

        EACH CASE MUST BE SKIPPED, NOT MERELY UNREAD, so an older genuine
        record sits beneath: a skip lets it decide (False), while a reader
        that simply gave up would answer None. The control is that same file
        with the malformed row made valid and CURRENT, which must answer True.
        """
        now = time.time()
        older = {"type": "assistant", "timestamp": self.stamp(now - 7200)}
        for label, stamp in (
                ("injected", 'junk", "timestamp": "2099-01-01T00:00:00Z'),
                ("impossible", "2026-99-99T99:99:99Z"),
                ("empty", ""),
        ):
            with self.subTest(timestamp=label):
                with open(self.p, "w", encoding="utf-8") as f:
                    f.write(json.dumps(older) + "\n")
                    f.write(json.dumps({"type": "assistant",
                                        "timestamp": stamp}) + "\n")
                self.assertIs(handoff.spoke_since(self.p, now - 60), False)
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(json.dumps(older) + "\n")
            f.write(json.dumps({"type": "assistant",
                                "timestamp": self.stamp(now)}) + "\n")
        self.assertIs(handoff.spoke_since(self.p, now - 60), True)

    def test_the_reader_never_consults_the_raw_line(self):
        """CLAUSES 1 AND 4, PINNED AS STRUCTURE RATHER THAN BEHAVIOUR.

        Every behavioural arm above can be satisfied by a reader that still
        greps the payload and merely happens to agree on these inputs. The
        invariant is about HOW the answer is reached, so this reads the AST:
        exactly one json.loads in the reader path, and no line-level regex
        surviving anywhere in it. A future edit that reintroduces a substring
        check reddens here even if every fixture above still passes.
        """
        import ast as _ast
        src = open(handoff.__file__, encoding="utf-8").read()
        fns = {n.name: n for n in _ast.walk(_ast.parse(src))
               if isinstance(n, _ast.FunctionDef)}
        for name in ("_spoken_at", "spoke_since"):
            self.assertIn(name, fns, "the reader path moved; re-pin this arm")
        names = []
        for name in ("_spoken_at", "spoke_since"):
            for n in _ast.walk(fns[name]):
                if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute):
                    base = getattr(n.func.value, "id", "")
                    names.append("%s.%s" % (base, n.func.attr))
        self.assertEqual(names.count("json.loads"), 1, names)
        self.assertNotIn("_ASSISTANT.search", names)
        self.assertNotIn("_TS.search", names)

    def test_a_payload_type_marker_cannot_make_a_user_row_speak(self):
        """SELECTION IS AS SUPPLIED AS THE TIMESTAMP WAS, and fixing only the
        timestamp left the same defect standing on the other leg.

        `"type": "assistant"` appears in PAYLOADS too — an echoed record, a
        tool result, a quoted transcript — so a regex over the raw line picks
        `user` rows as if the seat had spoken, and they then answer with a
        perfectly real top-level timestamp. A review found this on the SECOND
        round (348e1dc4aa81), after the timestamp half was already cured.

        THE ARM PROVES SKIPPED, NOT MERELY UNREAD. A file holding only the
        decoy would answer None either way, which a broken reader also does.
        So an OLDER genuine assistant record sits beneath it: if the decoy is
        skipped the old one decides and the answer is False, and the control
        flips ONLY the decoy's top-level type to make the same row decide.
        """
        now = time.time()
        decoy = {"message": {"content": [{"type": "tool_result",
                                          "echo": {"type": "assistant"}}]}}
        older = {"type": "assistant", "timestamp": self.stamp(now - 7200)}
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(json.dumps(older) + "\n")
            f.write(json.dumps(dict(decoy, type="user",
                                    timestamp=self.stamp(now))) + "\n")
        self.assertIs(handoff.spoke_since(self.p, now - 60), False)
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(json.dumps(older) + "\n")
            f.write(json.dumps(dict(decoy, type="assistant",
                                    timestamp=self.stamp(now))) + "\n")
        self.assertIs(handoff.spoke_since(self.p, now - 60), True)

    def test_a_payload_timestamp_cannot_speak_for_the_record(self):
        """A SUPPLIED INPUT MUST NOT BE ABLE TO ANSWER THE GUARD'S QUESTION.

        A transcript line is a nested JSON document and its PAYLOAD routinely
        carries a `timestamp` key — a tool_use input, a quoted body, an echoed
        record — which can appear in the bytes BEFORE the record's own. A
        reader that regex-scans the line takes whichever comes first, so a
        two-hour-old `assistant` record can claim to be current, the child
        withdraws, and a seat that never woke gets nothing. It fails OPEN, on
        the one input the record supplies rather than the reader derives.

        Found on review (dispatch 0e4addc128eb) and reproduced
        before curing. THE CONTROL IS THE SAME PAYLOAD SHAPE one field apart:
        an identical nested 2099 stamp on a record that IS current must still
        answer True, so False above is the top-level read winning rather than
        the nested value being ignored wholesale.
        """
        now = time.time()
        payload = {"content": [{"type": "tool_use",
                                "input": {"timestamp": "2099-01-01T00:00:00Z"}}]}
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "message": payload,
                                "timestamp": self.stamp(now - 7200)}) + "\n")
        self.assertIs(handoff.spoke_since(self.p, now - 60), False)
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "message": payload,
                                "timestamp": self.stamp(now)}) + "\n")
        self.assertIs(handoff.spoke_since(self.p, now - 60), True)

    def test_an_unreadable_record_is_unknown_not_a_guess(self):
        """A line this reader cannot parse yields None, so the caller keeps
        delivering. The control is the SAME file made parsable."""
        now = time.time()
        with open(self.p, "w", encoding="utf-8") as f:
            f.write('{"type":"assistant" THIS IS NOT JSON\n')
        self.assertIsNone(handoff.spoke_since(self.p, now - 60))
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant",
                                "timestamp": self.stamp(now)}) + "\n")
        self.assertIs(handoff.spoke_since(self.p, now - 60), True)

    def test_the_newest_assistant_record_decides(self):
        """A PAIR ON ONE FILE. An old record and a new one both present: the
        answer must track the NEWEST, or a long-lived session that spoke
        hours ago would look awake forever."""
        now = time.time()
        with open(self.p, "w", encoding="utf-8") as f:
            for at in (now - 7200, now - 1):
                f.write(json.dumps({"type": "assistant",
                                    "timestamp": self.stamp(at)}) + "\n")
        self.assertIs(handoff.spoke_since(self.p, now - 60), True)
        self.assertIs(handoff.spoke_since(self.p, now + 60), False)


# ---------------------------------------------------------------------------
# task/2134 — a refusal that took NO READ claims nothing about the pane
# ---------------------------------------------------------------------------

# HALF OF THE INSTRUMENT. THE OTHER HALF IS DIFFERENTIAL EXECUTION BELOW, AND
# NEITHER SUBSUMES THE OTHER — they catch OPPOSITE failures.
#
# THIS half catches an UNFOUNDED ASSERTION: a door that read nothing and states
# a present fact about the pane anyway. Such a sentence is usually a CONSTANT,
# so it does not vary with the pane and the differential half cannot see it.
#
# THE OTHER half catches LEAKAGE: a refusal whose text DEPENDS on content the
# door never read, in any wording at all. That half is unevadable by phrasing;
# this one is not, and its limit is stated here rather than hidden: THIS LIST IS
# ENUMERATED, NOT DERIVED, so it covers the phrasings below and is blind to the
# next one. A ninth shape is always available. What makes that blindness
# non-fatal is that a novel phrasing which interpolates ANYTHING is caught by
# the differential arm, and a novel phrasing that interpolates nothing is a
# constant this list can be extended to name.
#
# WHAT COUNTS: a PRESENT-TENSE verb or qualifier whose subject is the pane or
# its composer. `holds` is one; `held` is NOT — "has not remained held across
# the required persistence interval" is a statement about the RECORD's own
# history, which a door that read nothing is entitled to make. That one letter
# is a real discrimination and the unpersisted-record arm asserts the near-miss
# is still produced, so a detector matching the noun family blindly fails.
# A CONTENTS CLAIM IS NAMED BY ITS OBJECT, NOT BY ITS VERB. `holds`, `carries`
# and `shows`
# take many subjects in this codebase: a PROCESS carries an ORCA_PANE_KEY, a
# ROSTER holds a session, a REPLY carries a pane_id, a COMPACTION carries no
# session id. A verb-only pattern fired on all of them. What makes a sentence a
# claim about the pane's PRESENT CONTENTS is that the thing held IS composer
# content — text, a draft, input, a prompt, chrome, a paste chip.
#
# THE TEMPERED MIDDLE IS LOAD-BEARING: no non-content noun may sit between the
# verb and the content word, or "holds the pane this directive is addressed to"
# matches by reaching across a clause to a noun that is not the verb's object.
_CLAIM_CONTENT = (r"text|draft|input|prompt|chrome|paste\s+chip|instruction|"
                  r"directive|what\s+Helm\s+typed|unsent|contents")
_CLAIM_VERB = (r"holds|holding|has|have|having|carries|carrying|shows|showing|"
               r"contains|containing")
_CLAIM_NOT_THE_OBJECT = (r"pane|process|processes|session|roster|seat|key|id|"
                         r"repo|context")
_CONTENTS_CLAIM = re.compile(
    r"(?:%s)\b(?:(?!\b(?:%s)\b)[^.;]){0,40}?\b(?:%s)\b"
    % (_CLAIM_VERB, _CLAIM_NOT_THE_OBJECT, _CLAIM_CONTENT)
    + r"|\b(?:%s)\b[^.;]{0,30}?\b(?:sitting|remains|remaining)\s+in\b"
    % _CLAIM_CONTENT
    + r"|\bthere\s+is\s+(?:%s)\b[^.;]{0,20}?\bin\b" % _CLAIM_CONTENT
    + r"|\bcomposer\b[^.;]{0,30}?\bis\s+not\s+empty\b"
    + r"|\bcurrent(?:ly)?\s+composer\b"
    + r"|\bcurrently\s+(?:%s)\b" % _CLAIM_VERB,
    re.I)

# THE MODULES THAT PRODUCE REFUSALS ABOUT A PANE. The corpus arm below reads
# their string literals, so this list IS the scan's population and a module
# added here widens it.
_REFUSAL_MODULES = ("helm/composers.py", "helm/resumeturn.py",
                    "helm/orcaadopt.py", "helm/harness.py")

# EVERY PRODUCTION LITERAL THE DETECTOR MAY FIRE ON, each one a sentence written
# AFTER a real read. Derived by running the scan, then read one by one: this is
# the enumeration the six hand-picked doors could never be.
_READ_PATH_LITERALS = (
    "composer holds only placeholder chrome",
    "no current composer could be located in the tail",
    "holds a Helm RETRIEVAL PROMPT for directive",
    "never showed an identifiable composer holding Helm's text",
    "STILL holds the text in its composer",
    "composer holds text that is not what Helm typed",
    "composer shows a collapsed paste chip",
    "composer holds a pre-existing human draft",
    "the full current composer could not be identified exactly",
    "current composer does not exactly equal Helm's recorded text",
    "the composer no longer holds the text",
    # `choose_in_modal` reads the pane into `tail` as its first act and every
    # sentence below that read describes what the read returned. This one says
    # a dialog is not showing; the detector is right that it claims contents,
    # and the claim is one the door measured.
    "is not showing a dialog that owns input at delivery time",
    # The SAME door's wall sentence, and it is a contents claim on purpose: the
    # quoted wall LINE and both line spans it prints are fields of the parse of
    # the tail that first read returned, so the sentence reports measurements
    # rather than asserting a screen. It quotes the pane's own words and never
    # the recognizer that matched them. It says nothing about which of them
    # arrived first, because position does not carry that.
    "carries quota-wall text",
)


class CountingReads(FakeAdapter):
    """A FakeAdapter that records every read, and can hold a chosen body.

    The wording is the symptom; the invariant is that NO READ HAPPENED. An arm
    that inspected only the sentence would go green against a door that read
    the pane and merely declined to mention it — a different defect with the
    same output.
    """

    def __init__(self, panes=({"handle": "h1", "title": "codex",
                               "worktree": "", "last_output_at": 1},),
                 body=None):
        super().__init__(panes)
        self.reads = []
        self.body = body

    def read(self, handle, limit=3000, timeout=60):
        self.reads.append(handle)
        if self.body is None:
            return super().read(handle, limit=limit, timeout=timeout)
        return "\n".join(("─" * 40, "❯\xa0" + self.body, "─" * 40,
                          "  opus-5 | ~/dev/example/repo"))


class ARefusalWithoutAReadClaimsNothingTest(ResumeTurnBase):
    """task/2134 — TRUE AT EVERY DOOR AND, until this class, PINNED BY NO ARM.

    A refusal taken WITHOUT a live pane read must not assert anything about
    what the pane currently holds. A door that took no read and says "pane X
    holds Helm's injection" is claiming a present fact about something nobody
    looked at.

    A LITERAL ON ONE DOOR CANNOT HOLD THIS PROPERTY. An assertion on one
    refusal's exact words dies with that refusal's other contracts, and a
    door added later inherits no pin at all. So this is a property OVER the
    doors: each no-read refusal is DRIVEN through production, its read count
    asserted at zero, its keystroke count asserted at zero, and its detail
    checked for a present-tense contents claim — with the read-path mismatch
    refusal as an unconditional positive control, so "claims nothing" is a
    discrimination rather than a fact about an under-powered detector.
    """

    TEXT = "GO NOW"

    def assertClaimsNothing(self, detail, ad, door):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is test_a_refusal_that_did_read_does_carry_a_contents_claim, which drives the read-path mismatch door through the SAME detector and asserts a claim IS found; without that sibling every caller of this helper would be vacuous, which is exactly why it is written as one arm and not as a comment
        """The property, stated once so six doors cannot drift apart."""
        self.assertEqual(ad.reads, [],
                         "%s took a pane read (%r), so this arm is no longer "
                         "about a no-read refusal" % (door, ad.reads))
        self.assertEqual(ad.sent, [], "%s spent a keystroke" % door)
        claim = _CONTENTS_CLAIM.search(detail)
        self.assertIsNone(
            claim, "%s took no read, and its refusal asserts %r about the "
                   "pane's PRESENT contents: %s"
                   % (door, claim.group(0) if claim else "", detail))

    def record(self, observed_ago=None):
        """A recorded injection, optionally already observed held."""
        generation = resumeturn._record_injection(
            "codex", SID, "h1", self.TEXT)
        self.assertTrue(generation, "the fixture recorded no injection")
        if observed_ago is not None:
            self.assertIsNotNone(
                resumeturn._observe_injection(
                    "codex", "h1", self.TEXT, generation,
                    now=time.time() - observed_ago),
                "the fixture failed to observe the injection held, so every "
                "arm using it would refuse for the wrong reason")
        return generation

    # ---- the doors that take no read -------------------------------------

    def test_a_pane_with_no_record_refuses_without_a_read_or_a_claim(self):
        ad = CountingReads()
        state, detail = composers.submit("h1", adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("no unique Helm injection record", detail,
                      "a different door answered; this arm proves nothing "
                      "about the no-record refusal")
        self.assertClaimsNothing(detail, ad, "composers.submit no-record")

    def test_an_unpersisted_record_refuses_without_a_read_or_a_claim(self):
        self.record()
        ad = CountingReads()
        with mock.patch.object(resumeturn, "recovery_persist_s",
                               return_value=30):
            state, detail = composers.submit("h1", adapter=ad, now=100)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("has not remained held", detail,
                      "a different door answered")
        # THE NEAR-MISS THAT MAKES THE DETECTOR A DERIVATION. This sentence
        # contains `held`, one letter from the `holds` the detector fires on,
        # and it is legitimate here because its subject is the RECORD's
        # history, not the pane's present contents. If this door stops
        # producing the near-miss, the detector is no longer being tested
        # against the case that could break it.
        self.assertIn("held", detail)
        self.assertClaimsNothing(detail, ad, "composers.submit unpersisted")

    def test_a_census_reservation_failure_refuses_without_a_read_or_a_claim(self):
        self.record()
        ad = CountingReads()
        with mock.patch.object(resumeturn, "_claim_injection_observation",
                               return_value=None):
            rows, err = composers.scan(adapter=ad)
        self.assertIsNone(err)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["state"], composers.CANNOT_TELL)
        self.assertClaimsNothing(rows[0]["why"], ad, "census reservation")

    def test_an_ineligible_recovery_claim_refuses_without_a_read_or_a_claim(self):
        self.record(observed_ago=1)
        injection = resumeturn.recorded_injections()["h1"]
        self.assertTrue(resumeturn._claim_injection_recovery(injection),
                        "the fixture failed to take the generation's claim, "
                        "so the call below would refuse for another reason")
        ad = CountingReads()
        state, detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("already recovering", detail)
        self.assertClaimsNothing(detail, ad, "recover_injection ineligible")

    def test_a_changed_pane_identity_refuses_without_a_read_or_a_claim(self):
        self.record(observed_ago=1)
        injection = resumeturn.recorded_injections()["h1"]
        ad = CountingReads()

        def changed(_row, _adapter, action, **_kw):
            return action(ad, "h2", "re-proved a replacement pane"), None

        with mock.patch("helm.autocompact._pane_action", side_effect=changed):
            state, detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("pane identity changed", detail)
        self.assertClaimsNothing(detail, ad, "recover_injection handle change")

    def test_a_changed_process_identity_refuses_without_a_read_or_a_claim(self):
        generation = resumeturn._record_injection(
            "codex", SID, "h1", self.TEXT, adapter="fake", pids=["4242:99"])
        self.assertIsNotNone(resumeturn._observe_injection(
            "codex", "h1", self.TEXT, generation, now=time.time() - 1))
        injection = resumeturn.recorded_injections()["h1"]
        ad = CountingReads()
        with mock.patch.object(orcaadopt, "authorized_handle",
                               return_value=(None, "process birth changed")):
            state, detail = resumeturn.recover_injection(injection, adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("birth changed", detail)
        self.assertClaimsNothing(detail, ad, "recover_injection pid change")

    # ---- the control that makes all six a discrimination -----------------

    def test_a_refusal_that_did_read_does_carry_a_contents_claim(self):  # noqa: VACUOUS_ASSERTION — the flagged empty observable is ad.sent; its unconditional positive control is the assertTrue(ad.reads) two lines above it, asserted on the SAME call through the same adapter, which is what proves this door was reached and acted rather than skipped. The arm's own subject is a PRESENCE assertion (the detector must fire), so it is itself the control the six sibling absence arms depend on
        """THE UNCONDITIONAL POSITIVE CONTROL.

        Every arm above asserts an ABSENCE through this detector. If the
        detector could not see a contents claim at all they would pass over a
        door that borrowed the read-path's sentence wholesale. So drive the
        one refusal that IS entitled to describe the composer — the exact-read
        mismatch at harness.retry_held_submission — and require the detector
        to fire on it.
        """
        self.record(observed_ago=10)
        ad = CountingReads(body=self.TEXT + " and this is my unfinished draft")
        state, detail = composers.submit("h1", adapter=ad)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertTrue(ad.reads,
                        "the control never reached a read-path door, so it "
                        "controls nothing: %s" % detail)
        self.assertEqual(ad.sent, [], "a human draft must spend zero Enters")
        claim = _CONTENTS_CLAIM.search(detail)
        self.assertIsNotNone(
            claim, "the read-path mismatch refusal carries no present-tense "
                   "contents claim, so every no-read arm in this class is "
                   "vacuous rather than passing: %s" % detail)


# EIGHT PRESENT-TENSE CONTENTS-CLAIM SHAPES, MEASURED TO EVADE A NARROWER
# TOKEN SET. Kept as DATA so the cover is pinned: a trim of _CONTENTS_CLAIM
# reddens the arm below instead of silently narrowing what it can see.
# PRODUCTION SENTENCES A NO-READ DOOR IS ENTITLED TO PRODUCE, each one measured
# firing the VERB-ONLY pattern. They are the reason the object rule exists.
_LEGITIMATE_NON_CLAIMS = (
    "3 live processes name codex but none carries an ORCA_PANE_KEY — helm has "
    "no address to inject into",
    "pid 42 carries no ORCA_PANE_KEY — helm has no address to inject into",
    "this compaction carries no session id, so there is nothing to match a "
    "nameless pane against",
    "herdr: agent_started reply carries no pane_id",
    "the chat roster holds abc… for codex only as HISTORY",
    "the process holding abc is gone",
    "no pane holding abc… can be ruled out",
    "orca holds /x as a 'foo' context, not a git repo",
    "pane h1 has not remained held across the required persistence interval",
)


_EVADING_SHAPES = (
    "pane h1 has your draft",
    "pane h1 is showing unsent input",
    "pane h1 shows a draft",
    "there is text in pane h1",
    "your directive is sitting in pane h1",
    "pane h1 still carries what Helm typed",
    "the composer for h1 is not empty",
    "your instruction remains in pane h1",
)


class ARefusalWithoutAReadCarriesNoRecordContentTest(ResumeTurnBase):
    """task/2134, the OTHER half — DIFFERENTIAL EXECUTION over the RECORD.

    A NO-READ REFUSAL'S TEXT CANNOT DEPEND ON WHAT THE PANE HOLDS. So every
    no-read door is driven TWICE against panes holding DIFFERENT bodies and its
    two refusals must be BYTE-IDENTICAL. Any sentence that carries something
    about present contents differs between the runs whatever words it uses, so
    this half cannot be evaded by phrasing, which is the failure the lexical
    half is measurably open to.

    AND IT IS BLIND TO THE BUG IN THIS ROW'S TITLE, which is why the lexical
    half stays. A door returning the CONSTANT string "pane X currently holds
    text" is byte-identical across both panes and passes here; that claim is
    FALSE rather than leaked, and falsity does not vary with the pane.

    Leakage and unfounded assertion are OPPOSITE failures. Each half sees one.
    """

    TEXT_A = "GO NOW"
    TEXT_B = "RESUME THE LANDREQ DELIVERY LEG AND POST THE RECEIPT"

    def twice(self, door, name):
        """Run `door(recorded_text) -> (detail, adapter)` for two DIFFERENT
        recorded injection texts and require byte-identical refusals.

        THE AXIS IS THE RECORD, NOT THE PANE. A no-read door never sees the
        pane, so varying the PANE body tests nothing the `reads == []`
        assertion has not already settled.
        What such a door CAN reach without reading is Helm's own RECORDED text
        — and "pane X holds <the recorded text>" is precisely this row's bug: a
        present claim about the pane, sourced from the record, with nobody
        having looked. That sentence varies with the record and is caught here
        in ANY wording, including phrasings the lexical half has never seen.
        """
        out = []
        for text in (self.TEXT_A, self.TEXT_B):
            detail, ad = door(text)
            self.assertEqual(ad.reads, [],
                             "%s took a pane read (%r)" % (name, ad.reads))
            self.assertEqual(ad.sent, [], "%s spent a keystroke" % name)
            out.append(detail)
        self.assertEqual(
            out[0], out[1],
            "%s produced DIFFERENT refusals for two different RECORDED texts, "
            "so its sentence carries Helm's recorded content into a claim made "
            "with no read behind it:\n  A: %s\n  B: %s"
            % (name, out[0], out[1]))
        claim = _CONTENTS_CLAIM.search(out[0])
        self.assertIsNone(
            claim, "%s took no read and asserts %r about the pane's PRESENT "
                   "contents: %s"
                   % (name, claim.group(0) if claim else "", out[0]))
        return out[0]

    def record(self, text, observed_ago=None):
        generation = resumeturn._record_injection("codex", SID, "h1", text)
        self.assertTrue(generation, "the fixture recorded no injection")
        if observed_ago is not None:
            self.assertIsNotNone(
                resumeturn._observe_injection(
                    "codex", "h1", text, generation,
                    now=time.time() - observed_ago),
                "the fixture failed to observe the injection held")
        return generation

    # ---- the fixture control, and it validates the HARNESS ----------------

    def test_the_differential_instrument_CATCHES_a_door_that_leaks_the_record(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the assertIsNone proving the leak sentence does NOT trip the lexical half; its unconditional positive control is the assertRaises two lines below, which requires the differential half to FIRE on that same sentence. The pair is the whole point of the arm
        """THE CONTROL A DIFFERENTIAL INSTRUMENT ACTUALLY OWES: prove it FAILS
        on a known-bad door, or every identity assertion in this class is a
        statement about a comparison that never discriminates.

        The door below is SYNTHETIC and claims nothing about production. It
        takes no read, spends no keystroke, and interpolates Helm's RECORDED
        text into its refusal using a phrasing the lexical half does not see —
        "your instruction was %s" carries none of _CONTENTS_CLAIM's tokens. So
        the lexical half passes it and `twice` must still fail, which is the
        whole reason both halves exist.
        """
        def leaking_door(text):
            self.setUp_fresh()
            self.record(text)
            ad = CountingReads()
            injection = resumeturn.recorded_injections()["h1"]
            return ("pane h1 cannot be recovered; your instruction was %s"
                    % injection["text"]), ad

        sample = "pane h1 cannot be recovered; your instruction was %s" % self.TEXT_A
        self.assertIsNone(
            _CONTENTS_CLAIM.search(sample),
            "the leak sentence trips the LEXICAL half, so this control no "
            "longer isolates the differential half: %s" % sample)
        with self.assertRaises(AssertionError) as caught:
            self.twice(leaking_door, "synthetic leaking door")
        self.assertIn("DIFFERENT refusals for two different RECORDED texts",
                      str(caught.exception))

    def setUp_fresh(self):
        """Reset the per-test home so the second leg of a control starts clean."""
        shutil.rmtree(self.d, ignore_errors=True)
        os.makedirs(self.d, exist_ok=True)
        self.spawn()

    # ---- the no-read doors, driven twice ---------------------------------

    def test_no_record_refusal_is_identical_across_two_panes(self):
        def door(_text):
            self.setUp_fresh()          # no record at all: nothing to vary
            ad = CountingReads()
            return composers.submit("h1", adapter=ad)[1], ad
        detail = self.twice(door, "composers.submit no-record")
        self.assertIn("no unique Helm injection record", detail)

    def test_unpersisted_refusal_is_identical_across_two_panes(self):
        def door(text):
            self.setUp_fresh()
            self.record(text)
            ad = CountingReads()
            with mock.patch.object(resumeturn, "recovery_persist_s",
                                   return_value=30):
                return composers.submit("h1", adapter=ad, now=100)[1], ad
        detail = self.twice(door, "composers.submit unpersisted")
        self.assertIn("held", detail)

    def test_census_reservation_refusal_is_identical_across_two_panes(self):  # noqa: VACUOUS_ASSERTION — the unconditional structural assertions are inside the door: rows[0]["state"] == composers.CANNOT_TELL and err is None on BOTH legs, so a door that returned nothing or a different state could not reach the identity check
        def door(text):
            self.setUp_fresh()
            self.record(text)
            ad = CountingReads()
            with mock.patch.object(resumeturn, "_claim_injection_observation",
                                   return_value=None):
                rows, err = composers.scan(adapter=ad)
            self.assertIsNone(err)
            self.assertEqual(rows[0]["state"], composers.CANNOT_TELL)
            return rows[0]["why"], ad
        self.twice(door, "census reservation")

    def test_ineligible_claim_refusal_is_identical_across_two_panes(self):
        def door(text):
            self.setUp_fresh()
            self.record(text, observed_ago=1)
            injection = resumeturn.recorded_injections()["h1"]
            self.assertTrue(resumeturn._claim_injection_recovery(injection),
                            "the fixture failed to take the claim")
            ad = CountingReads()
            return resumeturn.recover_injection(injection, adapter=ad)[1], ad
        detail = self.twice(door, "recover_injection ineligible")
        self.assertIn("already recovering", detail)

    def test_changed_handle_refusal_is_identical_across_two_panes(self):
        def door(text):
            self.setUp_fresh()
            self.record(text, observed_ago=1)
            injection = resumeturn.recorded_injections()["h1"]
            ad = CountingReads()

            def changed(_row, _adapter, action, **_kw):
                return action(ad, "h2", "re-proved a replacement pane"), None

            with mock.patch("helm.autocompact._pane_action",
                            side_effect=changed):
                return resumeturn.recover_injection(
                    injection, adapter=ad)[1], ad
        detail = self.twice(door, "recover_injection handle change")
        self.assertIn("pane identity changed", detail)

    def test_changed_pid_refusal_is_identical_across_two_panes(self):
        def door(text):
            self.setUp_fresh()
            generation = resumeturn._record_injection(
                "codex", SID, "h1", text, adapter="fake", pids=["4242:99"])
            self.assertIsNotNone(resumeturn._observe_injection(
                "codex", "h1", text, generation, now=time.time() - 1))
            injection = resumeturn.recorded_injections()["h1"]
            ad = CountingReads()
            with mock.patch.object(orcaadopt, "authorized_handle",
                                   return_value=(None, "process birth changed")):
                return resumeturn.recover_injection(
                    injection, adapter=ad)[1], ad
        detail = self.twice(door, "recover_injection pid change")
        self.assertIn("birth changed", detail)

    # ---- the widening is pinned ------------------------------------------

    def test_no_production_refusal_outside_the_read_path_trips_the_detector(self):  # noqa: VACUOUS_ASSERTION — the flagged empty observable is the `unexpected == []` assertion, and it carries TWO unconditional positive controls in the same body: the scan must reach more than 100 literals (a broken walker reports broken, never clean), and the expected-hit list must still contain two named read-path sentences (an emptied list would leave nothing to compare against). Both run before the absence assertion
        """THE ENUMERATION THE SIX HAND-PICKED DOORS COULD NEVER BE.

        The other arms drive six doors I chose. This one reads EVERY refusal-
        shaped string literal in the modules that refuse about a pane, runs the
        detector over all of them, and requires every hit to be a sentence
        written AFTER a real read. It is the reason the detector's vocabulary
        stops being a judgement call: widening it is now MEASURED against what
        production actually says, and a token that reaches a no-read refusal
        reddens here instead of shipping.

        IT ALSO FIRES IN THE OTHER DIRECTION. A new no-read refusal that adopts
        composer-claim wording reddens this arm even though nobody added an arm
        for that door — which is exactly the seventh-door gap the rest of this
        class has and cannot close.
        """
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scanned, unexpected = 0, []
        for rel in _REFUSAL_MODULES:
            path = os.path.join(root, rel)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) \
                        or not isinstance(node.value, str):
                    continue
                text = node.value.strip()
                # refusal-SHAPED: one line, a real sentence, not a docstring
                if " " not in text or "\n" in text or len(text) > 400:
                    continue
                scanned += 1
                if not _CONTENTS_CLAIM.search(text):
                    continue
                if any(k in text for k in _READ_PATH_LITERALS):
                    continue
                unexpected.append("%s:%d %s" % (rel, node.lineno, text))
        # MUST-HIT FIRST: a scan that reached nothing would report a clean
        # population while proving only that the walker is broken.
        self.assertGreater(scanned, 100,
                           "the literal scan reached %d strings — it is not "
                           "reading these modules" % scanned)
        for known in ("composer holds a pre-existing human draft",
                      "current composer does not exactly equal"):
            self.assertTrue(
                any(known in lit for lit in _READ_PATH_LITERALS),
                "the expected-hit list lost %r, so this arm would pass by "
                "having nothing to compare against" % known)
        self.assertEqual(
            unexpected, [],
            "these production refusals trip the contents-claim detector and "
            "are not read-path sentences — either the wording claims something "
            "nobody read, or the detector is too wide:\n  %s"
            % "\n  ".join(unexpected))

    def test_a_legitimate_non_claim_stays_silent(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is its sibling test_every_measured_evading_shape_is_covered, which requires the SAME pattern to FIRE on eight shapes; asserting silence here is what proves the object rule discriminates rather than having been narrowed into uselessness
        """THE SENTENCES THAT FORCED THE OBJECT RULE.

        Every one of these is a real production refusal, and every one fired
        the earlier VERB-ONLY pattern: a process carries an ORCA_PANE_KEY, a
        roster holds a session, a reply carries a pane_id, a compaction carries
        no session id. None of them says anything about a composer. A detector
        that cannot tell them from `composer holds a draft` is reading the verb
        and calling it the claim.
        """
        for sentence in _LEGITIMATE_NON_CLAIMS:
            hit = _CONTENTS_CLAIM.search(sentence)
            self.assertIsNone(
                hit, "the detector fires on %r in a sentence whose subject is "
                     "not the composer: %s"
                     % (hit.group(0) if hit else "", sentence))

    def test_every_measured_evading_shape_is_covered(self):  # noqa: VACUOUS_ASSERTION — the arm asserts PRESENCE eight times (the detector must FIRE on each shape); the single absence assertion at the end is its own unconditional positive control, since the eight preceding assertions are on the same pattern in the opposite direction
        """THE COVER IS PINNED BY DATA, NOT BY MEMORY.

        Eight present-tense contents-claim shapes measured to evade a narrower
        token set. Holding them here means a trim of the pattern reddens rather
        than silently narrowing cover, and it shows the next reader that this
        half's vocabulary is ENUMERATED rather than derived — which is its
        known limit and the reason the differential half exists."""
        for shape in _EVADING_SHAPES:
            self.assertIsNotNone(
                _CONTENTS_CLAIM.search(shape),
                "the detector no longer sees %r, a shape measured evading it" % shape)
        # AND THE OTHER DIRECTION, so widening has not made it fire on
        # everything: a RECORD-history sentence a no-read door may legitimately
        # produce must stay silent.
        self.assertIsNone(_CONTENTS_CLAIM.search(
            "pane h1 has not remained held across the required persistence "
            "interval"))


def _granted(why=""):
    """A prepared authorization whose inputs stand."""
    return harness.Grant(True, why, still=lambda: (True, ""))


def _refused(why, kind="drained"):
    """A refused preparation. Its validator says the inputs stand, because a
    door that honours the refusal never consults it -- so a door that ignored
    the refusal would act, and an arm can see that."""
    return harness.Grant(False, why, kind, still=lambda: (True, ""))


class DoorBook(object):
    """A double for `resumeturn._prepare_due` KEYED BY DOOR NAME.

    Each door maps to one Grant or a list of Grants served in order, the last
    repeating; a door the book does not name is granted. `asked` records the
    door of every preparation, so an arm can assert WHICH door was reached
    instead of counting positional answers that a new earlier ask would
    silently shift."""

    def __init__(self, **doors):
        self.doors = {k.replace("_", "-"): (list(v) if isinstance(v, list)
                                            else [v])
                      for k, v in doors.items()}
        self.asked, self.attempts = [], []

    def __call__(self, seat_name, session, room=None, door="", attempt=None,
                 key=None):
        self.asked.append(door)
        self.attempts.append(attempt)
        queue = self.doors.get(door)
        if not queue:
            return _granted()
        return queue.pop(0) if len(queue) > 1 else queue[0]


class AppendingAdapter(FakeAdapter):
    """A composer that KEEPS what is already in it. `send` without Enter
    APPENDS, the way a terminal composer does, so text typed into a human's
    draft is observable as contamination instead of being overwritten by the
    fixture. `human` types as a person would. Enter clears the composer."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.composer = ""
        self.on_read = None
        self.reads = 0

    def human(self, text):
        self.composer += text

    def read(self, handle, limit=3000, timeout=60):
        self.reads += 1
        if self.on_read is not None:
            frame = self.on_read(self.reads)
            if frame is not None:
                return frame
        return "\n".join(("─" * 40, "❯\xa0" + self.composer,
                          "─" * 40, "  opus-5 | ~/dev/example/repo"))

    def send(self, handle, text, enter=True):
        self.sent.append((handle, text, enter))
        if enter:
            self.enter_attempted = True
            self.composer = ""
        else:
            self.typed = text
            self.composer += text


#: A readable frame with terminal chrome and NO prompt line: a repaint.
CHROME_ONLY = "\n".join(("─" * 40, "  opus-5 | ~/dev/example/repo",
                         "  ⏵⏵ bypass permissions on"))


class OneActDoorTest(ResumeTurnBase):
    """PREPARE, then a FRESH graded composer proof, then a NON-BLOCKING
    validation of the preparation, then the act -- at placement and at Enter.
    A preparation may block; nothing that blocks runs between a proof and the
    act it licenses, and a validation that finds an input moved restarts the
    whole preparation and proof."""

    TEXT = "GO NOW"

    def enters(self, ad):
        return [row for row in ad.sent if row[2]]

    def placements(self, ad):
        return [row for row in ad.sent if not row[2]]

    def test_a_human_draft_during_the_placement_preparation_places_nothing(self):
        ad = AppendingAdapter()

        def prepare(door):
            if door == "placement" and not ad.composer:
                # The preparation blocks, and a person starts typing inside it.
                ad.human("half a thought")
            return _granted()

        state, detail = ad.submit("h1", self.TEXT, settle=0, admit=prepare)
        self.assertEqual(self.placements(ad), [],
                         "helm typed into a human draft that appeared during "
                         "the placement preparation")
        self.assertEqual(ad.composer, "half a thought")
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("may be a human draft", detail)
        # THE CONTROL, unconditional, same adapter shape: no draft places and
        # submits.
        ad = AppendingAdapter()
        state, detail = ad.submit("h1", self.TEXT, settle=0,
                                  admit=lambda door: _granted())
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(len(self.placements(ad)), 1)
        self.assertEqual(len(self.enters(ad)), 1)

    def test_a_pause_set_during_the_pre_read_places_nothing(self):
        world = {"paused": False, "prepared": 0}
        ad = AppendingAdapter()

        def pause_lands(n):
            world["paused"] = True          # lands while the pre-read reads
            return None

        ad.on_read = pause_lands

        def prepare(door):
            world["prepared"] += 1
            if world["paused"]:
                return harness.Grant(False, "delivery paused (state UNKNOWN)",
                                     "paused")
            return harness.Grant(True, still=lambda: (
                (False, "delivery to alpha was paused") if world["paused"]
                else (True, "")))

        state, detail = ad.submit("h1", self.TEXT, settle=0, admit=prepare)
        self.assertEqual(self.placements(ad), [],
                         "helm typed after the pause landed inside the pre-read")
        self.assertEqual(state, harness.NOT_DELIVERED, detail)
        self.assertIn("nothing typed", detail)
        self.assertEqual(getattr(detail, "kind", None), "paused")
        self.assertEqual(world["prepared"], 2,
                         "the moved validation did not restart the preparation")

    def test_a_stable_world_places_and_submits(self):
        ad = AppendingAdapter()
        book = DoorBook()
        state, detail = ad.submit("h1", self.TEXT, settle=0,
                                  admit=lambda door: book("a", "s", door=door))
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(book.asked, ["placement", "enter"])
        self.assertEqual(len(self.placements(ad)), 1)
        self.assertEqual(len(self.enters(ad)), 1)

    def test_an_enter_refused_at_its_door_is_withheld_and_named(self):
        ad = AppendingAdapter()
        book = DoorBook(enter=_refused("the rows drained at the act"))
        state, detail = ad.submit("h1", self.TEXT, settle=0,
                                  admit=lambda door: book("a", "s", door=door))
        self.assertEqual(self.enters(ad), [],
                         "Enter was pressed after the reason expired")
        self.assertEqual(state, harness.NOT_DELIVERED, detail)
        self.assertIn("Enter withheld at the act", detail)
        self.assertIn("drained at the act", detail)
        self.assertEqual(getattr(detail, "door", None), "enter")
        self.assertEqual(len(self.placements(ad)), 1,
                         "fixture: nothing was placed, so this is not the "
                         "Enter door")

    def test_a_human_append_during_the_enter_preparation_withholds_enter(self):
        ad = AppendingAdapter()

        def prepare(door):
            if door == "enter":
                ad.human(" and a human's own words")
            return _granted()

        state, detail = ad.submit("h1", self.TEXT, settle=0, admit=prepare)
        self.assertEqual(self.enters(ad), [],
                         "Enter submitted a composer a human appended to "
                         "during the Enter preparation")
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("a human may have edited", detail)

    def test_an_input_that_moves_after_the_proof_restarts_then_refuses(self):  # noqa: VACUOUS_ASSERTION — the absence is no Enter after an exhausted restart budget; test_a_stable_world_places_and_submits drives the same door on the same adapter and presses exactly one Enter
        ad = AppendingAdapter()
        prepared = []

        def prepare(door):
            prepared.append(door)
            if door != "enter":
                return _granted()
            return harness.Grant(True, still=lambda: (
                False, "this seat's delivery cursor for main moved"))

        state, detail = ad.submit("h1", self.TEXT, settle=0, admit=prepare)
        self.assertEqual(self.enters(ad), [])
        self.assertEqual(state, harness.NOT_DELIVERED, detail)
        self.assertIn("delivery cursor for main moved", detail)
        self.assertEqual(getattr(detail, "kind", None), "moved")
        self.assertEqual(prepared.count("enter"),
                         1 + harness.ADMISSION_RESTARTS,
                         "the restart budget was not spent on whole "
                         "preparations")

    def test_an_unvalidatable_grant_authorizes_nothing(self):
        ad = AppendingAdapter()
        state, detail = ad.submit("h1", self.TEXT, settle=0,
                                  admit=lambda door: harness.Grant(True))
        self.assertEqual(ad.sent, [], "a grant with no validator authorized "
                                      "a keystroke")
        self.assertEqual(getattr(detail, "kind", None), "unknown")

    def test_chrome_only_after_holds_is_unreadable_not_an_edit(self):
        ad = AppendingAdapter()
        seen = {"enter_rounds": 0}

        def repaint(n):
            # Once the first Enter round has proven HOLDS, the terminal
            # repaints: chrome is readable and the prompt line is not there.
            return CHROME_ONLY if seen["enter_rounds"] >= 2 else None

        ad.on_read = repaint

        def prepare(door):
            if door != "enter":
                return _granted()
            seen["enter_rounds"] += 1
            return harness.Grant(True, still=lambda: (
                False, "an input moved") if seen["enter_rounds"] == 1
                else (True, ""))

        with mock.patch.object(harness, "SUBMIT_VERIFY_INTERVAL_S", 0):
            state, detail = ad.submit("h1", self.TEXT, settle=0, admit=prepare)
        self.assertEqual(self.enters(ad), [])
        self.assertEqual(seen["enter_rounds"], 2,
                         "fixture: the proof after the restart never ran")
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("could not be read", detail)
        self.assertNotIn("a human may have edited", detail,
                         "an unreadable composer was reported as a human edit")


    def test_a_human_edit_during_the_authorization_reads_is_seen_last(self):  # noqa: VACUOUS_ASSERTION — the absence is no act into an edited composer; the same arm's unconditional control drives the same door on the same adapter shape and asserts one placement and one Enter
        """THE COMPOSER IS READ AFTER THE DEPENDENCIES. A person who types
        while the final capture re-reads the authorization is seen by the one
        composer read that closes the capture, at placement and at Enter."""
        for door, words in (("placement", "a draft typed mid-capture"),
                            ("enter", " and words appended mid-capture")):
            ad = AppendingAdapter()
            typed = []

            def capture(door_name=door):
                if not typed and (door_name == "placement"
                                  or ad.composer):
                    typed.append(words)
                    ad.human(words)
                return True, ""

            def prepare(name, door_name=door):
                return harness.Grant(True, still=capture) \
                    if name == door_name else _granted()
            state, detail = ad.submit("h1", self.TEXT, settle=0,
                                      admit=prepare)
            self.assertEqual(typed, [words],
                             "fixture: the human never typed inside the "
                             "%s capture" % door)
            acted = self.placements(ad) if door == "placement" \
                else self.enters(ad)
            self.assertEqual(acted, [],
                             "a human edit made during the authorization "
                             "reads was not seen by the final composer "
                             "capture at %s: %r" % (door, ad.sent))
            self.assertEqual(state, harness.UNKNOWN, detail)
        # THE CONTROL, unconditional: the same capture with nobody typing
        # places and submits.
        ad = AppendingAdapter()
        state, detail = ad.submit("h1", self.TEXT, settle=0,
                                  admit=lambda door: harness.Grant(
                                      True, still=lambda: (True, "")))
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual((len(self.placements(ad)), len(self.enters(ad))),
                         (1, 1))

    def test_a_stalled_final_capture_refuses_inside_its_deadline(self):  # noqa: VACUOUS_ASSERTION — the absence is nothing sent after a stall; the same arm's unconditional control releases the same capture and asserts DELIVERED
        """A dependency read that STALLS is abandoned at the declared
        deadline: the round restarts, then refuses, and nothing is acted on.
        The stall is real -- a worker blocked on an event -- and so is the
        clock."""
        import threading
        release = threading.Event()
        stalls = []

        def capture():
            stalls.append(time.monotonic())
            release.wait(3.0)
            return True, ""
        ad = AppendingAdapter()
        started = time.monotonic()
        try:
            with mock.patch.object(harness, "ACT_DEADLINE_S", 0.2):
                state, detail = ad.submit(
                    "h1", self.TEXT, settle=0,
                    admit=lambda door: harness.Grant(True, still=capture))
            elapsed = time.monotonic() - started
        finally:
            release.set()
        self.assertEqual(ad.sent, [],
                         "acted on a capture that stalled past its deadline")
        self.assertEqual(len(stalls), 1 + harness.ADMISSION_RESTARTS,
                         "fixture: the stalled capture was not reached on "
                         "every round")
        self.assertLess(elapsed, 2.5,
                        "the door waited out the stall instead of its "
                        "deadline (%.2fs)" % elapsed)
        self.assertEqual(state, harness.NOT_DELIVERED, detail)
        self.assertEqual(getattr(detail, "kind", None), "moved", detail)
        self.assertIn("deadline", detail)
        self.assertRegex(detail, r"the capture ran 0\.\d{3}s: dependencies "
                                 r"0\.\d{3}s, composer 0\.\d{3}s")
        # THE CONTROL, unconditional: the same capture, released, acts.
        ad = AppendingAdapter()
        with mock.patch.object(harness, "ACT_DEADLINE_S", 0.2):
            state, detail = ad.submit(
                "h1", self.TEXT, settle=0,
                admit=lambda door: harness.Grant(True, still=capture))
        self.assertEqual(state, harness.DELIVERED, detail)

    def test_an_act_publishes_its_measured_window(self):  # noqa: VACUOUS_ASSERTION — the receipts list is asserted equal to the two named doors, which cannot hold for an empty list
        ad = AppendingAdapter()
        state, detail = ad.submit("h1", self.TEXT, settle=0,
                                  admit=lambda door: _granted())
        self.assertEqual(state, harness.DELIVERED, detail)
        receipts = getattr(ad, "act_receipts", [])
        self.assertEqual([r["door"] for r in receipts], ["placement", "enter"])
        for receipt in receipts:
            self.assertEqual(receipt["outcome"], harness.ACT_CLEAN, receipt)
            for field in ("capture_to_send_start_s",
                          "capture_to_send_return_s",
                          "send_start_to_reconciliation_s"):
                self.assertGreaterEqual(receipt[field], 0.0, field)
                self.assertLess(receipt[field], receipt["deadline_s"], field)
            self.assertEqual(receipt["send_bound_s"], harness.ACT_SEND_S)
        self.assertIn("[placement act window: final capture to send start",
                      detail)
        self.assertIn("[enter act window", detail)

    def test_an_attempt_past_its_horizon_is_refused_before_the_keystroke(self):  # noqa: VACUOUS_ASSERTION — the absence is nothing sent past the horizon; the same arm's unconditional control with a horizon ahead asserts DELIVERED
        ad = AppendingAdapter()
        state, detail = ad.submit(
            "h1", self.TEXT, settle=0,
            admit=lambda door: harness.Grant(True, still=lambda: (
                True, "", {"horizon": time.time() - 1})))
        self.assertEqual(ad.sent, [], "an attempt past its horizon acted")
        self.assertEqual(getattr(detail, "kind", None), "stale", detail)
        # THE CONTROL: a horizon still ahead acts.
        ad = AppendingAdapter()
        state, detail = ad.submit(
            "h1", self.TEXT, settle=0,
            admit=lambda door: harness.Grant(True, still=lambda: (
                True, "", {"horizon": time.time() + 60})))
        self.assertEqual(state, harness.DELIVERED, detail)


class TheActDoorsOwnEdgesTest(ResumeTurnBase):
    """The edges of the act door the final capture does not cover by itself:
    a final composer read that fails, the send that carries the keystroke,
    the directive a recovery prompt points at, and the spawn register."""

    TEXT = "GO NOW"

    def setUp(self):
        super().setUp()
        from helm import resumeturn
        resumeturn._OBSOLETE_HERE.clear()

    def test_a_final_read_that_raises_is_unknown_and_the_child_survives(self):  # noqa: VACUOUS_ASSERTION — the placement control asserts the same adapter shape delivers when its final read does not raise
        """AN UNREADABLE COMPOSER AT THE FINAL CAPTURE IS UNKNOWN, NOT AN
        EXCEPTION. The orca CLI fails at the Enter door's final HOLDS read:
        the door refuses UNKNOWN, no Enter is pressed, and the child that
        drove it returns a mode instead of dying with a traceback."""
        import threading

        class Failing(AppendingAdapter):
            failed = 0

            def read(self, handle, limit=3000, timeout=60):
                if self.composer and threading.current_thread().name \
                        == "helm-act-capture":
                    Failing.failed += 1
                    raise harness.HarnessError(
                        "orca terminal read: rc 1 — terminal not found")
                return super().read(handle, limit=limit, timeout=timeout)
        ad = Failing()
        try:
            state, detail = ad.submit("h1", self.TEXT, settle=0,
                                      admit=lambda door: _granted())
        except Exception as exc:             # noqa: BLE001 — the witness
            self.fail("a final read that raised escaped the act door: %r"
                      % (exc,))
        self.assertGreaterEqual(Failing.failed, 1,
                                "fixture: the final HOLDS read never raised")
        self.assertEqual([r for r in ad.sent if r[2]], [],
                         "Enter was pressed past an unreadable final capture")
        self.assertEqual(state, harness.NOT_DELIVERED, detail)
        self.assertEqual(getattr(detail, "kind", None), "unknown", detail)
        self.assertIn("could not be read at the final capture", detail)
        # THROUGH THE CHILD: the same failure at the child's Enter door and
        # at its recovery door ends in a mode.
        from helm import resumeturn
        Failing.failed = 0
        child_ad = Failing()
        try:
            with mock.patch.object(resumeturn, "_prepare_due",
                                   side_effect=DoorBook()), \
                    mock.patch.object(resumeturn, "_alert"):
                mode, detail = resumeturn.child(
                    "codex", SID, self.TEXT, 0, adapter=child_ad,
                    record_key="deaf:codex:%s" % SID)
        except Exception as exc:             # noqa: BLE001 — the witness
            self.fail("a final read that raised escaped the act door: %r"
                      % (exc,))
        self.assertGreaterEqual(Failing.failed, 1, "fixture: never raised")
        self.assertIsInstance(mode, str)
        self.assertEqual([r for r in child_ad.sent if r[2]], [])
        # THE CONTROL: the same adapter shape, reading normally, delivers.
        ad = AppendingAdapter()
        state, detail = ad.submit("h1", self.TEXT, settle=0,
                                  admit=lambda door: _granted())
        self.assertEqual(state, harness.DELIVERED, detail)

    # -- task/2463 r10: the arm specifications a review wrote for findings F4
    # and F5, followed as written.

    #: A frame the read RETURNS, with terminal chrome and no prompt line: the
    #: final composer read succeeded and the composer in it is unreadable.
    RETURNED_UNREADABLE = "\n".join((
        "─" * 40,
        "  opus-5 | ~/dev/example/repo",
        "─" * 40,
    ))

    #: (door, the final composer helper that door takes) for every act door.
    FINAL_HELPERS = (
        ("placement", lambda ad: ad._final_clean("h1")),
        ("enter", lambda ad: ad._final_holds("h1", "GO")),
        ("recovery-attempt", lambda ad: ad._final_exact("h1", "GO")),
    )

    def test_a_returned_unreadable_final_composer_reads_unknown(self):
        """F4, A RETURNED UNREADABLE OBSERVATION. The final read returns a
        frame whose composer cannot be identified -- no exception is raised
        -- and every final helper answers None (UNKNOWN), not False (moved)."""
        chrome = self.RETURNED_UNREADABLE
        self.assertIsNone(harness._prompt_line(chrome))
        self.assertIs(harness.observe_composer(chrome, "GO"),
                      harness.UNREADABLE)
        self.assertIsNone(harness.composer_exactly_holds(chrome, "GO"))
        for door, make_final in self.FINAL_HELPERS:
            with self.subTest(door=door):
                ad = AppendingAdapter()
                with mock.patch.object(ad, "read", return_value=chrome):
                    seen, detail = make_final(ad)(0.25)
                self.assertIsNone(
                    seen, "the %s door's final helper answered %r for a "
                    "returned unreadable composer: %s" % (door, seen, detail))

    def test_a_returned_unreadable_final_composer_refuses_at_the_door(self):  # noqa: VACUOUS_ASSERTION — the refusal kind is asserted equal to unknown; test_a_readable_final_composer_acts_once_through_its_door drives the same door with the same helpers and asserts one act
        """F4 THROUGH THE DOOR: a granted preparation, a standing earlier
        proof, and the real final helper reading the returned unreadable
        frame. The door refuses UNKNOWN at once and acts on nothing, instead
        of restarting as if the composer had moved."""
        chrome = self.RETURNED_UNREADABLE
        for door, make_final in self.FINAL_HELPERS:
            with self.subTest(door=door):
                ad = AppendingAdapter()
                acts = []
                with mock.patch.object(ad, "read", return_value=chrome):
                    admitted, refusal = ad._through_door(
                        door,
                        lambda _door: _granted(),
                        lambda: (True, None),
                        make_final(ad),
                        lambda timeout: acts.append(timeout))
                self.assertFalse(admitted)
                self.assertEqual(acts, [])
                self.assertEqual(
                    refusal.kind, "unknown",
                    "the %s door read a returned unreadable final composer as "
                    "%r: %s" % (door, refusal.kind, refusal))

    def test_a_readable_final_composer_acts_once_through_its_door(self):
        """F4's HEALTHY CONTROL: the ordinary frame -- an empty composer for
        the placement helper, Helm's text for the Enter and recovery helpers
        -- answers True and acts once through the door."""
        for door, make_final in self.FINAL_HELPERS:
            with self.subTest(door=door):
                ad = AppendingAdapter()
                ad.composer = "" if door == "placement" else "GO"
                seen, detail = make_final(ad)(0.25)
                self.assertIs(seen, True, detail)
                acts = []
                admitted, got = ad._through_door(
                    door,
                    lambda _door: _granted(),
                    lambda: (True, None),
                    make_final(ad),
                    lambda timeout: acts.append(timeout))
                self.assertTrue(admitted, got)
                self.assertEqual(len(acts), 1, acts)

    def test_the_send_bound_row_publishes_no_end_to_end_bound(self):
        """F5, PUBLICATION. The send bound is a client subprocess timeout and
        the act window a capture admission window; neither, nor their sum, is
        an enforced capture-to-return deadline, so the row must not say one
        is. A targeted editorial regression assertion, not a validator of the
        whole sentence."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "docs", "ENVIRONMENT.md"),
                  encoding="utf-8") as f:
            environment_text = f.read()
        row = next(line for line in environment_text.splitlines()
                   if line.startswith("| `HELM_ACT_SEND_S` |"))
        self.assertIn("client-side bound", row)
        self.assertNotIn(
            "The enforced end-to-end bound of an act is "
            "`HELM_ACT_DEADLINE_S` plus this, to the send's return", row)

    def test_the_act_door_source_publishes_no_end_to_end_bound(self):
        """F5's matching editorial check on the source commentary that
        declares the send bound and documents the act door."""
        with open(harness.__file__, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("\nACT_SEND_S = ", source,
                      "fixture: the file read is not the module that declares "
                      "the send bound")
        self.assertNotIn("ENFORCED END-TO-END BOUND", source,
                         "the harness commentary still publishes an enforced "
                         "end-to-end bound")

    def _cli(self, stall_enter_s=0.0):
        """A REAL CLI the shipped OrcaAdapter runs as a subprocess: it keeps
        a composer in a file, and an Enter send can sleep before it lands."""
        import stat
        import sys
        box = os.path.join(self.tmp, "cli")
        os.makedirs(box, exist_ok=True)
        composer = os.path.join(box, "composer")
        with open(composer, "w", encoding="utf-8") as f:
            f.write("")
        script = os.path.join(box, "orca")
        with open(script, "w", encoding="utf-8") as f:
            f.write("""#!%s
import json, sys, time
argv = sys.argv[1:]
path, stall = %r, %r
text = open(path, encoding="utf-8").read()
if argv[:2] == ["terminal", "read"]:
    rule = "\\u2500" * 40
    tail = [rule, "\\u276f\\u00a0" + text, rule, "  opus-5 | ~/dev/repo"]
    print(json.dumps({"ok": True, "result": {"terminal": {"tail": tail}}}))
elif argv[:2] == ["terminal", "send"]:
    if "--enter" in argv:
        time.sleep(stall)
        text = ""
    else:
        text += argv[argv.index("--text") + 1]
    open(path, "w", encoding="utf-8").write(text)
    print(json.dumps({"ok": True, "result": {}}))
else:
    print(json.dumps({"ok": False, "error": {"message": "unsupported"}}))
    sys.exit(1)
""" % (sys.executable, composer, stall_enter_s))
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        return harness.OrcaAdapter(path=script)

    def test_the_send_that_carries_the_keystroke_is_bounded(self):
        """THE WINDOW ENDS AT THE SEND'S RETURN, AND THE SEND HAS A BOUND. A
        real CLI whose Enter send hangs is killed at the declared send bound;
        the act is accounted obsolete-authorization with its landing unknown,
        and both measured edges of the window are published."""
        ad = self._cli(stall_enter_s=3.0)
        with mock.patch.object(harness, "ACT_SEND_S", 0.5):
            state, detail = ad.submit("h1", self.TEXT, settle=0, reads=1,
                                      interval=0,
                                      admit=lambda door: _granted())
        receipts = getattr(ad, "act_receipts", [])
        self.assertEqual([r["door"] for r in receipts], ["placement", "enter"],
                         "fixture: the doors did not both act: %s" % detail)
        enter = receipts[1]
        held = enter["capture_to_send_return_s"] - enter["capture_to_send_start_s"]
        self.assertLess(held, 2.0,
                        "the act's send was not bounded: it held %.2fs" % held)
        self.assertGreaterEqual(held, 0.5)
        self.assertEqual(enter["outcome"], harness.OBSOLETE_AUTHORIZATION,
                         enter)
        self.assertIn("landed is unknown", enter["what"])
        self.assertIn("final capture to send return", detail)
        # THE CONTROL: the same CLI, answering at once, acts clean.
        ad = self._cli(stall_enter_s=0.0)
        with mock.patch.object(harness, "ACT_SEND_S", 0.5):
            state, detail = ad.submit("h1", self.TEXT, settle=0, reads=1,
                                      interval=0,
                                      admit=lambda door: _granted())
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual([r["outcome"] for r in ad.act_receipts],
                         [harness.ACT_CLEAN, harness.ACT_CLEAN])

    def test_a_recovery_enter_past_its_directive_horizon_is_refused(self):  # noqa: VACUOUS_ASSERTION — the control recovers the same pointer with a directive that outlives the act and asserts DELIVERED
        """THE DIRECTIVE'S HORIZON IS CAPTURED AND COMPARED BY CPU BEFORE THE
        KEYSTROKE. The recovery prompt points at a directive that is still
        fetchable when the final capture reads it and expires while the final
        composer read runs; the Enter is refused as stale."""
        import threading
        from helm import resumeturn
        long_text = "a directive long enough to be stored " * 12

        def recover(ttl):
            with mock.patch.object(resumeturn, "injection_ttl_s",
                                   return_value=ttl):
                wire = resumeturn._wire_text("codex", SID, long_text)
            self.assertTrue(resumeturn._WIRE_INDIRECTION.match(wire),
                            "fixture: the directive was not stored")
            expires = resumeturn._directive_authority(wire)[0]
            ad = AppendingAdapter()
            ad.human(wire)
            waited = []

            def slow_final(_n):
                if threading.current_thread().name == "helm-act-capture" \
                        and not waited:
                    waited.append(expires - time.time())
                    time.sleep(max(0.0, expires - time.time()) + 0.05)
                return None
            ad.on_read = slow_final
            gen = resumeturn._record_injection("codex", SID, "h1", wire,
                                               adapter="fake")
            resumeturn._observe_injection("codex", "h1", wire, gen)
            inj = resumeturn.recorded_injections(include_expired=True)["h1"]
            with mock.patch.object(harness, "ACT_DEADLINE_S", 1.25):
                state, detail = resumeturn.recover_injection(
                    inj, adapter=ad, admit=lambda door: _granted())
            return ad, state, detail, waited
        # THE ARM'S WALL TIME IS THE TTL PLUS THE ACT DEADLINE. The first
        # final read sleeps out what is left of the TTL, so the TTL only has
        # to outlast the path from the store to that read (the fixture
        # asserts waited[0] > 0). The control's first final read stalls on
        # its 3600s horizon, is abandoned at ACT_DEADLINE_S and restarts.
        # The deadline stays 2.5 times the short TTL, as 5.0 was to 2.0, so
        # the short arm's stalled read still returns inside it.
        ad, state, detail, waited = recover(0.5)
        self.assertEqual(len(waited), 1, "fixture: no final read ran")
        self.assertGreater(waited[0], 0,
                           "fixture: the directive had expired before the "
                           "final composer read")
        self.assertEqual([r for r in ad.sent if r[2]], [],
                         "an Enter was pressed after the directive it points "
                         "at expired: %s" % detail)
        self.assertIn("act horizon", detail)
        # THE CONTROL: a directive that outlives the act recovers.
        resumeturn._OBSOLETE_HERE.clear()
        pk.write_json(resumeturn.state_path(), {})
        ad, state, detail, _waited = recover(3600.0)
        self.assertEqual(state, harness.DELIVERED, detail)

    def test_a_project_seat_s_spawn_register_is_a_dependency(self):  # noqa: VACUOUS_ASSERTION — the register path is asserted EQUAL to the file written, and its identity asserted to change on a rewrite; the numbered-seat absence is the control
        """A PROJECT-CANONICAL seat's family comes from its spawn register, so
        the register is compared by identity at the final capture and at the
        accounting; a numbered seat has no such file."""
        from helm import resumeturn
        d = seat._instance_dir("codex", "proj-a-codex")
        os.makedirs(d, exist_ok=True)
        register = os.path.join(d, "spawn.json")
        with open(register, "w") as f:
            json.dump({"v": 1, "seat": "proj-a-codex", "family": "codex",
                       "project": "proj-a", "harness": "fake",
                       "handle": "h9", "session": SID}, f)
        paths = dict(resumeturn._dependency_paths("proj-a-codex", SID, "main"))
        self.assertEqual(paths.get("spawn register"), register,
                         "the spawn register that names a project seat's "
                         "family is not a dependency: %r" % (paths,))
        before = resumeturn._versions("proj-a-codex", SID, "main")
        time.sleep(0.01)
        with open(register, "w") as f:
            json.dump({"v": 1, "seat": "proj-a-codex", "family": "codex",
                       "project": "proj-a", "harness": "fake",
                       "handle": "h9", "session": SID}, f)
        after = resumeturn._versions("proj-a-codex", SID, "main")
        self.assertNotEqual(before["spawn register"], after["spawn register"])
        # THE CONTROL: a numbered seat's family needs no file.
        self.assertNotIn("spawn register", dict(
            resumeturn._dependency_paths("codex-41", SID, "main")))


class APlacedDirectiveKeepsItsOwnerTest(ResumeTurnBase):
    """A refusal AFTER placement is not a refusal before it. Once Helm has
    typed into the composer the injection record exists, so withholding the
    unauthorized Enter is right and walking away from the obligation is not."""

    class Held(FakeAdapter):
        """The composer keeps the text after Enter: the real producer's
        NOT_DELIVERED, read back through the inherited `submit`."""

        def read(self, handle, limit=3000, timeout=60):
            return "\n".join(("─" * 40, "❯\xa0" + (self.typed or ""),
                               "─" * 40,
                               "  opus-5 | ~/dev/example/repo"))

    def _child(self, book, ad):
        from helm import resumeturn
        reached = []
        real = resumeturn.recover_injection

        def spy(*a, **k):
            reached.append(k.get("admit"))
            return real(*a, **k)

        with mock.patch.object(resumeturn, "_prepare_due", side_effect=book), \
                mock.patch.object(resumeturn, "recover_injection",
                                  side_effect=spy), \
                mock.patch.object(resumeturn, "_alert"):
            mode, detail = resumeturn.child("codex", SID, "GO NOW", 0,
                                            adapter=ad,
                                            record_key="deaf:codex:%s" % SID)
        return mode, detail, reached

    def test_a_refusal_after_placement_routes_the_obligation(self):
        from helm import tasks
        ad = self.Held()
        book = DoorBook(recovery=_refused(
            "the rows drained while the injection held"))
        mode, detail, reached = self._child(book, ad)
        self.assertIn("recovery", book.asked,
                      "fixture: the recovery preparation was never asked")
        self.assertIn("drained while the injection held", detail)
        self.assertIn("recovery task task/resume-turn-", detail,
                      "a typed directive was abandoned without an owner")
        self.assertTrue(tasks.rows(), "no durable row carries the obligation")
        self.assertNotEqual(mode, "resumed")

    def test_every_recovery_attempt_reauthorizes(self):
        """THE RECOVERY RETRIES ARE ENTERS TOO. The recovery is ENTERED, its
        actuator is reached, and the refusal comes from the per-attempt door."""
        ad = self.Held()
        book = DoorBook(recovery_attempt=_refused(
            "the rows drained between two Enters"))
        mode, detail, reached = self._child(book, ad)
        self.assertEqual(len(reached), 1,
                         "fixture: recover_injection was never reached, so "
                         "the per-attempt door is unexercised")
        self.assertIn("recovery-attempt", book.asked,
                      "the recovery Enter was never prepared at its own door")
        enters = [row for row in ad.sent if row[2]]
        self.assertEqual(len(enters), 1,
                         "the recovery pressed Enter after the reason "
                         "expired: %r" % (ad.sent,))
        self.assertIn("drained between two Enters", detail)
        self.assertNotEqual(mode, "resumed")

    def test_one_recovery_enter_allowed_then_the_next_refused(self):
        from helm import resumeturn
        ad = self.Held()
        book = DoorBook(recovery_attempt=[
            _granted(), _refused("the rows drained before the second Enter")])
        with mock.patch.object(resumeturn, "RECOVERY_ATTEMPTS", 2):
            mode, detail, reached = self._child(book, ad)
        self.assertEqual(len(reached), 1, "fixture: recovery never reached")
        self.assertEqual(book.asked.count("recovery-attempt"), 2,
                         "fixture: the second recovery attempt was never "
                         "prepared")
        enters = [row for row in ad.sent if row[2]]
        self.assertEqual(len(enters), 2,
                         "one permitted recovery Enter and one refused one "
                         "must press exactly two Enters in all: %r"
                         % (ad.sent,))
        self.assertIn("drained before the second Enter", detail)
        self.assertNotEqual(mode, "resumed")

    def test_a_refusal_BEFORE_placement_routes_nothing(self):  # noqa: VACUOUS_ASSERTION — the absence is nothing typed and no task; test_a_refusal_after_placement_routes_the_obligation drives the same child on the same adapter, types, and mints the task
        """THE CONTROL: a refusal before anything was typed has no obligation
        to hand over, and minting a recovery task for it would invent one."""
        from helm import tasks
        ad = self.Held()
        book = DoorBook(settle=_refused("the rows drained"))
        mode, detail, _reached = self._child(book, ad)
        self.assertEqual(ad.sent, [], "fixture: the control typed something, "
                                      "so it is not a pre-placement refusal")
        self.assertEqual(mode, "drained", detail)
        self.assertNotIn("recovery task", detail)
        self.assertFalse(tasks.rows(),
                         "a refusal that typed nothing minted an obligation")


class TheAdmissionIsAtTheActTest(ResumeTurnBase):
    """The preparation before the pane lifecycle lock and the settle wait is
    not the authorization for a keystroke on the far side of that window: the
    placement door prepares again, and a refusal there types nothing."""

    def _run(self, book):
        from helm import resumeturn
        ad = FakeAdapter()

        def reproved(_row, _adapter, action, **_kw):
            return action(ad, "h1", "re-proved h1"), None

        with mock.patch.object(resumeturn, "_prepare_due", side_effect=book), \
                mock.patch("helm.autocompact._pane_action",
                           side_effect=reproved), \
                mock.patch.object(resumeturn, "_alert"):
            mode, detail = resumeturn.child("codex", SID, "GO NOW", 0,
                                            adapter=ad,
                                            record_key="deaf:codex:%s" % SID)
        return ad, mode, detail

    def test_a_reason_that_expires_during_the_wait_stops_the_keystroke(self):
        book = DoorBook(placement=_refused(
            "the rows drained while the pane settled"))
        ad, mode, detail = self._run(book)
        self.assertEqual(book.asked[:2], ["settle", "placement"])
        self.assertEqual(ad.sent, [],
                         "the keystroke was pressed after the reason expired")
        self.assertIn("drained while the pane settled", detail)
        self.assertEqual(mode, "drained", detail)

    def test_a_reason_that_HOLDS_through_the_wait_still_types(self):
        """THE CONTROL, unconditional."""
        ad, mode, detail = self._run(DoorBook())
        self.assertTrue(ad.sent, detail)
        self.assertEqual(mode, "resumed", detail)


class OneEpisodeSpendsOneSlotTest(ResumeTurnBase):
    """The parent charges an attempt when it decides to spawn and the child
    charges the same attempt when it starts. One attempt is one slot, in either
    order, and the compaction leg with no parent still spends its own."""

    def _at(self, key):
        from helm import resumeturn
        return list(((resumeturn._peek(key) or {}).get("at")) or [])

    def test_the_child_does_not_charge_the_parents_episode_again(self):
        from helm import resumeturn
        key = "deaf:alpha:s1"
        resumeturn._record(key, "s1", "resume", "", count_it=True)  # the parent
        self.assertEqual(len(self._at(key)), 1)
        with mock.patch.object(resumeturn, "_await_or_withdraw",
                               return_value=(True, "withdrawn for the arm")):
            resumeturn.child("alpha", "s1", "text", 0, record_key=key)
        self.assertEqual(len(self._at(key)), 1,
                         "the child charged the parent's episode a second time")

    def test_a_leg_with_no_parent_key_still_counts(self):
        """THE CONTROL: a child that owns its own episode -- the compaction
        leg -- must still spend a slot."""
        from helm import resumeturn
        with mock.patch.object(resumeturn, "_await_or_withdraw",
                               return_value=(True, "withdrawn for the arm")):
            resumeturn.child("solo-seat", "s2", "text", 0)
        self.assertEqual(len(self._at("solo-seat")), 1)

    def test_the_third_supported_episode_is_admitted(self):
        from helm import resumeturn
        now = 10_000.0
        entry = {"at": [now - 3000, now - 1500]}
        action, _why = resumeturn._decide(entry, now)
        self.assertEqual(action, "resume",
                         "two repaired spells must not exhaust a cap of three")


class AnAttemptIsBornOnceAndChargedOnceTest(ResumeTurnBase):
    """ATTEMPT IDENTITY IS NOT SPELL IDENTITY. An attempt is minted with its
    own birth into the repair episode; it launches only inside a window from
    that birth; it is charged once, by receipt, through either writer, while
    the episode names it; and an id the episode no longer names is refused
    without accounting. The rate stamp and its receipt are one write."""

    SEAT = "alpha"

    def seed(self, since, state=None):
        from helm import beacons, pk
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = pk.read_json(path, {}) or {}
        row = rows.setdefault(self.SEAT, {"session": "s1"})
        att = dict(row.get("attendance") or {})
        att.update({"state": state or beacons.DEAF_IN_EFFECT, "alarm": True,
                    "since": since, "at": since})
        row["attendance"] = att
        pk.write_json(path, rows)
        return att

    def mint(self, since, now):
        from helm import beacons
        att = self.seed(since)
        attempt = beacons.install_repair(self.SEAT, att, now)
        self.assertIsNotNone(attempt, "fixture: the episode did not install")
        return attempt

    def key(self):
        return "deaf:%s:s1" % self.SEAT

    def at(self):
        from helm import resumeturn
        return list(((resumeturn._peek(self.key()) or {}).get("at")) or [])

    def charge_both(self, attempt, clock, t):
        from helm import resumeturn
        clock[0] = t
        resumeturn._record_nb(self.key(), "s1", "resume", "", True,
                              attempt=attempt, seat=self.SEAT)
        resumeturn._record(self.key(), "s1", "spawned", "t", count_it=True,
                           attempt=attempt, seat=self.SEAT)

    def test_a_fresh_retry_of_an_old_spell_is_charged_and_rate_limited(self):
        """The spell starts at T. Attempts launch at T+1, T+901 and T+1801 and
        each is refused at a dirty composer, so the spell stays owed. At
        T+2701 the cap holds. At T+3602 the first stamp expires and a FOURTH
        attempt is minted under the same, now hour-old, spell: both writers
        must charge it exactly once, and the very next decision must see that
        charge and debounce."""
        from helm import resumeturn
        T = 1_800_000_000.0
        clock = [T]
        with mock.patch("time.time", side_effect=lambda: clock[0]):
            for offset in (1, 901, 1801):
                clock[0] = T + offset
                self.assertEqual(
                    resumeturn._decide(resumeturn._peek(self.key()),
                                       clock[0])[0], "resume",
                    "fixture: the decision at +%d refused" % offset)
                attempt = self.mint(T, clock[0])
                self.charge_both(attempt, clock, T + offset)
            self.assertEqual(len(self.at()), 3)
            self.assertEqual(resumeturn._decide(resumeturn._peek(self.key()),
                                                T + 2701)[0], "capped")
            clock[0] = T + 3602
            self.assertEqual(resumeturn._decide(resumeturn._peek(self.key()),
                                                clock[0])[0], "resume",
                             "fixture: the fourth attempt was not admitted")
            fourth = self.mint(T, clock[0])
            self.charge_both(fourth, clock, T + 3602)
            self.assertEqual(self.at()[-1:], [T + 3602],
                             "a fresh retry of an old spell was not charged")
            self.assertEqual(len(self.at()), 3,
                             "one attempt was charged by both writers")
            action, _why = resumeturn._decide(resumeturn._peek(self.key()),
                                              T + 3603)
            self.assertEqual(action, "debounce",
                             "the next decision did not see the fresh charge")

    def test_a_delayed_second_writer_never_recounts(self):
        from helm import beacons, resumeturn
        T = 1_800_000_000.0
        clock = [T]
        with mock.patch("time.time", side_effect=lambda: clock[0]):
            first = self.mint(T, T)
            self.charge_both(first, clock, T)
            self.assertEqual(len(self.at()), 1)
            # The same attempt's child arrives much later, still current.
            clock[0] = T + 5000
            resumeturn._record(self.key(), "s1", "spawned", "t", count_it=True,
                               attempt=first, seat=self.SEAT)
            self.assertEqual(self.at(), [],
                             "a delayed writer of a still-current attempt "
                             "recounted after its stamp expired")
            # Terminal cleanup: the attempt settles, a NEW spell installs.
            self.assertTrue(beacons.settle_repair(self.SEAT, "typed", "landed",
                                                  attempt=first))
            second = self.mint(T + 6000, T + 6000)
            self.assertNotEqual(first, second)
            clock[0] = T + 6001
            resumeturn._record(self.key(), "s1", "spawned", "t", count_it=True,
                               attempt=first, seat=self.SEAT)
            self.assertEqual(self.at(), [],
                             "a delayed writer of a retired attempt recounted "
                             "after its episode was cleaned up")
            receipts = (resumeturn._peek(self.key()) or {}).get("receipts")
            self.assertNotIn(second, receipts or {})
            # THE CONTROL, unconditional: the current attempt charges once.
            self.charge_both(second, clock, T + 6002)
            self.assertEqual(self.at(), [T + 6002])
            self.assertEqual(sorted(
                (resumeturn._peek(self.key()) or {}).get("receipts")),
                [second], "receipts outlived the attempt they were for")

    def test_an_absent_or_retired_id_is_refused_without_accounting(self):  # noqa: VACUOUS_ASSERTION — the absence is an unchanged rate list; test_the_parent_skipped_the_child_charges_once charges the same store through the same writer for a current attempt
        from helm import resumeturn
        for label, prepare in (
                ("absent", lambda: "1799999999.0#1"),
                ("retired", lambda: self.retire())):
            with self.subTest(label):
                attempt = prepare()
                before = self.at()
                resumeturn._record(self.key(), "s1", "spawned", "t",
                                   count_it=True, attempt=attempt,
                                   seat=self.SEAT)
                self.assertEqual(self.at(), before,
                                 "an %s attempt was accounted" % label)
                self.assertTrue(resumeturn._launch_refusal(
                    self.SEAT, attempt, time.time()))

    def retire(self):
        now = time.time()
        old = self.mint(now - 50, now - 50)
        self.mint(now - 50, now - 10)
        return old

    def test_the_parent_skipped_the_child_charges_once(self):
        from helm import resumeturn
        now = time.time()
        attempt = self.mint(now - 5, now - 5)
        resumeturn._record(self.key(), "s1", "spawned", "t", count_it=True,
                           attempt=attempt, seat=self.SEAT)
        self.assertEqual(len(self.at()), 1,
                         "a launch whose parent stamp was skipped went "
                         "unbilled")

    def test_a_failed_charge_write_loses_both_halves_and_reconciles(self):  # noqa: VACUOUS_ASSERTION — the same arm asserts the child's charge landed exactly once after the failed write
        """THE STAMP AND ITS RECEIPT ARE ONE WRITE. The parent's write fails:
        neither half persists, so the child charges once; the parent's retry
        then finds the receipt and adds nothing."""
        from helm import pk, resumeturn
        now = time.time()
        attempt = self.mint(now - 5, now - 5)
        real = pk.write_json

        def crash(path, value, *a, **k):
            if path == resumeturn.state_path():
                raise OSError("disk full at the charge write")
            return real(path, value, *a, **k)

        with mock.patch.object(pk, "write_json", side_effect=crash):
            self.assertIsNone(resumeturn._record_nb(
                self.key(), "s1", "resume", "", True, attempt=attempt,
                seat=self.SEAT))
        entry = resumeturn._peek(self.key()) or {}
        self.assertEqual((entry.get("at"), entry.get("receipts")),
                         (None, None),
                         "half of a charge persisted from a failed write")
        resumeturn._record(self.key(), "s1", "spawned", "t", count_it=True,
                           attempt=attempt, seat=self.SEAT)
        resumeturn._record_nb(self.key(), "s1", "resume", "", True,
                              attempt=attempt, seat=self.SEAT)
        self.assertEqual(len(self.at()), 1,
                         "the crashed charge was suppressed or duplicated")

    def test_the_parents_outcome_write_keeps_the_birth(self):
        from helm import beacons, resumeturn
        now = time.time()
        attempt = self.mint(now - 5, now - 5)
        att = self.seed(now - 5)
        beacons._record_repairs([(self.SEAT, att, {
            "action": "wake", "detail": None, "attempt": attempt})], now)
        self.assertEqual(resumeturn._attempt_standing(self.SEAT, attempt)[0],
                         "current",
                         "the parent's outcome write erased the attempt's "
                         "birth")

    def test_a_stale_mint_is_refused_and_re_minted(self):  # noqa: VACUOUS_ASSERTION — the same arm launches the re-minted attempt and asserts its receipt
        from helm import beacons, resumeturn
        now = time.time()
        stale = self.mint(now - resumeturn._window_s() - 5,
                          now - resumeturn._window_s() - 5)
        # The parent charged it at mint: a precharge is not a launch licence.
        with mock.patch("time.time",
                        side_effect=lambda: now - resumeturn._window_s() - 5):
            resumeturn._record_nb(self.key(), "s1", "resume", "", True,
                                  attempt=stale, seat=self.SEAT)
        delivered = []
        with mock.patch.object(resumeturn, "deliver",
                               side_effect=lambda *a, **k: delivered.append(a)
                               or ("resumed", "typed")):
            mode, detail = resumeturn.child(
                self.SEAT, "s1", "text", 0, record_key=self.key(),
                attempt=stale)
        self.assertEqual(mode, "stale", detail)
        self.assertEqual(delivered, [], "a stale mint was launched")
        receipts = (resumeturn._peek(self.key()) or {}).get("receipts") or {}
        self.assertEqual(list(receipts), [stale],
                         "the stale launch added a charge of its own")
        att = self.seed(now - resumeturn._window_s() - 5)
        self.assertTrue(beacons._repair_due(att),
                        "a stale mint settled the spell")
        fresh = beacons.install_repair(self.SEAT, att, time.time())
        self.assertNotEqual(fresh, stale)
        self.assertEqual(resumeturn._launch_refusal(self.SEAT, fresh,
                                                    time.time()), "")
        with mock.patch.object(resumeturn, "_await_or_withdraw",
                               return_value=(True, "withdrawn for the arm")):
            mode, _detail = resumeturn.child(
                self.SEAT, "s1", "text", 0, record_key=self.key(),
                attempt=fresh)
        self.assertEqual(mode, "withdrawn")
        receipts = (resumeturn._peek(self.key()) or {}).get("receipts") or {}
        self.assertEqual(list(receipts), [fresh],
                         "the re-minted attempt was not charged")


    def launch(self, attempt, standing=None):
        """child() for `attempt` with the pane seams doubled -> (mode, detail,
        delivered). `standing`, given, replaces every roster read AFTER the
        child's first one, which is the read the launch check makes."""
        from helm import resumeturn
        real, reads, delivered = resumeturn._attempt_standing, [], []

        def standing_of(seat, att):
            reads.append(att)
            return real(seat, att) if standing is None or len(reads) == 1 \
                else standing

        with mock.patch.object(resumeturn, "_attempt_standing",
                               side_effect=standing_of), \
                mock.patch.object(resumeturn, "_prepare_due",
                                  side_effect=DoorBook()), \
                mock.patch.object(resumeturn, "_await_or_withdraw",
                                  return_value=(False, "")), \
                mock.patch.object(resumeturn, "deliver",
                                  side_effect=lambda *a, **k:
                                  delivered.append(a) or ("resumed", "typed")):
            mode, detail = resumeturn.child(
                self.SEAT, "s1", "text", 0, record_key=self.key(),
                attempt=attempt)
        self.assertGreaterEqual(len(reads), 1 if standing is None else 2,
                                "fixture: the charge never read the roster")
        return mode, detail, delivered

    def test_a_charge_refused_at_the_launch_launches_nothing(self):  # noqa: VACUOUS_ASSERTION — after the loop the same child with two current reads must deliver once and charge once, unconditionally
        """The standing moves while the child waits for the store's lock: the
        charge refuses the attempt, and a refused charge is a refused launch."""
        from helm import resumeturn
        for moved in (("retired", None), ("absent", None), ("unknown", None)):
            now = time.time()
            attempt = self.mint(now - 5, now - 5)
            before = self.at()
            mode, detail, delivered = self.launch(attempt, standing=moved)
            self.assertEqual(delivered, [],
                             "a child whose charge was refused (%s) still "
                             "delivered" % moved[0])
            self.assertEqual(mode, "stale", detail)
            self.assertEqual(self.at(), before,
                             "a refused launch (%s) accounted a slot"
                             % moved[0])
        # THE CONTROL: the same child, both reads current, delivers once and
        # charges once.
        now = time.time()
        attempt = self.mint(now + 1, now)
        before = len(self.at())
        mode, detail, delivered = self.launch(attempt)
        self.assertEqual((mode, len(delivered)), ("resumed", 1), detail)
        self.assertEqual(len(self.at()), before + 1)

    def test_a_mint_past_its_debounce_is_stale(self):  # noqa: VACUOUS_ASSERTION — the same arm launches a one-second-old mint through the same helper and asserts one delivery
        """A mint older than the debounce its charge buys is stale, even well
        inside the rate window; a mint a second old launches."""
        from helm import resumeturn
        old = time.time() - resumeturn._debounce_s() - 5
        self.assertLess(resumeturn._debounce_s() + 5, resumeturn._window_s(),
                        "fixture: the arm needs a mint inside the window")
        stale = self.mint(old, old)
        with mock.patch("time.time", return_value=old):
            resumeturn._record_nb(self.key(), "s1", "resume", "", True,
                                  attempt=stale, seat=self.SEAT)
        mode, detail, delivered = self.launch(stale)
        self.assertEqual(delivered, [], "a mint past its debounce launched")
        self.assertEqual(mode, "stale", detail)
        # THE CONTROL: a fresh mint of a new spell launches.
        now = time.time()
        fresh = self.mint(now - 1, now - 1)
        mode, detail, delivered = self.launch(fresh)
        self.assertEqual((mode, len(delivered)), ("resumed", 1), detail)

    def test_a_launch_re_dates_its_one_stamp(self):
        """The parent charges at mint and the child launches later: the one
        stamp moves to the launch, so the next decision debounces from it."""
        from helm import resumeturn
        T = 1_800_000_000.0
        clock = [T]
        with mock.patch("time.time", side_effect=lambda: clock[0]):
            attempt = self.mint(T, T)
            resumeturn._record_nb(self.key(), "s1", "resume", "", True,
                                  attempt=attempt, seat=self.SEAT)
            self.assertEqual(self.at(), [T], "fixture: the parent's charge")
            clock[0] = T + 100
            self.assertEqual(resumeturn._launch_charge(
                self.key(), "s1", "t", attempt, self.SEAT), "")
            self.assertEqual(self.at(), [T + 100],
                             "a launch kept the mint's stamp")
            self.assertEqual(resumeturn._decide(resumeturn._peek(self.key()),
                                                T + 200)[0], "debounce")
            # A second launch-writer of the same attempt moves nothing new in.
            clock[0] = T + 110
            resumeturn._record(self.key(), "s1", "spawned", "t", count_it=True,
                               attempt=attempt, seat=self.SEAT)
            self.assertEqual(self.at(), [T + 100])

    def test_an_unreadable_standing_still_debounces_the_next_pass(self):  # noqa: VACUOUS_ASSERTION — the loop's first pass is the clean-roster control on the same observables, and the healed charge asserts exactly one stamp
        """A roster that fails its strict read cannot say whether the minted
        attempt is current. The parent still charges, so the next pass is
        debounced instead of minting and spawning again; when the roster heals
        the child finds the receipt and does not charge a second time."""
        from helm import pk, resumeturn
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        # THE CONTROL runs first, on a clean roster; the malformed row second.
        for label, bad in (("clean roster", False), ("malformed row", True)):
            state = resumeturn.state_path()
            if os.path.exists(state):
                os.remove(state)
            att = self.seed(time.time() + (50 if bad else 0))
            rows = pk.read_json(path, {})
            if bad:
                rows["zeta"] = {"session": 5}
                pk.write_json(path, rows)
            spawned, got = [], []
            with mock.patch.object(resumeturn, "_registered",
                                   return_value=("", [4242])), \
                    mock.patch.object(resumeturn, "_still_owed",
                                      return_value=(900.0, "")), \
                    mock.patch.object(resumeturn, "_delivery_paused",
                                      return_value=""), \
                    mock.patch.object(resumeturn, "spawn_child",
                                      side_effect=spawned.append):
                for _ in range(2):
                    got.append(resumeturn.wake_undelivered(
                        self.SEAT, "s1", waited=900, att=att))
            first = got[0].get("attempt")
            self.assertEqual(got[0]["action"], "wake", (label, got))
            self.assertEqual(
                resumeturn._attempt_standing(self.SEAT, first)[0],
                "unknown" if bad else "current",
                "fixture: the roster's strict read (%s)" % label)
            self.assertEqual(
                [g["action"] for g in got], ["wake", "debounce"],
                "an unreadable standing disabled the limiter (%s)" % label)
            self.assertEqual(len(spawned), 1, label)
        rows = pk.read_json(path, {})      # the roster the install wrote
        rows.pop("zeta")
        pk.write_json(path, rows)
        self.assertEqual(resumeturn._attempt_standing(self.SEAT, first)[0],
                         "current", "fixture: the healed roster")
        self.assertEqual(resumeturn._launch_charge(
            self.key(), "s1", "t", first, self.SEAT), "")
        self.assertEqual(len(self.at()), 1,
                         "the healed child charged a second time")


    def test_a_determined_refusal_survives_a_failed_state_write(self):  # noqa: VACUOUS_ASSERTION — the loop's uncontended pass is the control on the same observables and asserts one delivery; the refusal passes are the witness
        """THE CHILD'S PRE-CHECK PASSES AT T+110; THE STORE'S LOCK IS HELD
        UNTIL T+125; THE CHARGE, UNDER THE LOCK, JUDGES THE ATTEMPT STALE; AND
        THE WRITE THAT WOULD PERSIST THAT FAILS. The refusal was determined,
        so the child launches nothing whether or not it persisted."""
        from helm import pk, resumeturn
        T = 1_800_000_000.0
        real_write, real_flock = pk.write_json, resumeturn.fcntl.flock
        for label, contended, failing in (("written", True, False),
                                          ("write failed", True, True),
                                          ("uncontended", False, True)):
            state = resumeturn.state_path()
            if os.path.exists(state):
                os.remove(state)
            clock = [T]
            with mock.patch("time.time", side_effect=lambda: clock[0]):
                attempt = self.mint(T + len(label), T)
            clock[0] = T + 110

            def flock(fd, op):
                target = os.readlink("/proc/self/fd/%d" % (
                    fd if isinstance(fd, int) else fd.fileno()))
                if contended and target == os.path.realpath(
                        state + ".lock"):
                    clock[0] = T + 125   # the lock was held this long
                return real_flock(fd, op)

            def write(path, value, *a, **k):
                if failing and path == state:
                    raise OSError("ENOSPC at the refusal write")
                return real_write(path, value, *a, **k)
            with mock.patch("time.time", side_effect=lambda: clock[0]), \
                    mock.patch.object(resumeturn.fcntl, "flock",
                                      side_effect=flock), \
                    mock.patch.object(pk, "write_json", side_effect=write):
                mode, detail, delivered = self.launch(attempt)
            if contended:
                self.assertEqual(delivered, [],
                                 "a refusal the charge determined was "
                                 "discarded when its write %s" % label)
                self.assertEqual(mode, "stale", detail)
            else:
                # THE CONTROL: an attempt inside its horizon whose write
                # fails keeps its preliminary verdict and launches.
                self.assertEqual((mode, len(delivered)), ("resumed", 1),
                                 detail)

    def test_the_act_closure_carries_the_attempt(self):
        from helm import resumeturn
        now = time.time()
        attempt = self.mint(now - 1, now - 1)
        book = DoorBook()
        ad = FakeAdapter()

        def reproved(_row, _adapter, action, **_kw):
            return action(ad, "h1", "re-proved h1"), None
        with mock.patch.object(resumeturn, "_prepare_due", side_effect=book), \
                mock.patch("helm.autocompact._pane_action",
                           side_effect=reproved), \
                mock.patch.object(resumeturn, "_await_or_withdraw",
                                  return_value=(False, "")), \
                mock.patch.object(resumeturn, "_alert"):
            mode, detail = resumeturn.child(self.SEAT, "s1", "GO NOW", 0,
                                            adapter=ad, record_key=self.key(),
                                            attempt=attempt)
        self.assertEqual(mode, "resumed", detail)
        self.assertIn("placement", book.asked, "fixture: no act door was asked")
        self.assertEqual(set(book.attempts), {attempt},
                         "an act door was prepared without the attempt it "
                         "acts for: %r" % (list(zip(book.asked,
                                                    book.attempts)),))


class AnAdmissionIsNotALicenceHeldOpenTest(ResumeTurnBase):
    """The child prepares before it enters the pane transaction, and each
    refusal kind is recorded as itself: a drain settles, a pause defers, and
    an UNKNOWN withholds without claiming a drain."""

    def _child(self, book):
        from helm import resumeturn
        typed = []
        with mock.patch.object(resumeturn, "_await_or_withdraw",
                               return_value=(False, "")), \
                mock.patch.object(
                    resumeturn, "deliver",
                    side_effect=lambda *a, **k: (typed.append(a) or
                                                 ("resumed", "typed"))), \
                mock.patch.object(resumeturn, "_prepare_due", side_effect=book):
            mode, detail = resumeturn.child("alpha", "s1", "text", 0,
                                            record_key="deaf:alpha:s1")
        return mode, detail, typed

    def test_a_row_consumed_during_the_settle_stops_the_keystroke(self):
        mode, detail, typed = self._child(DoorBook(settle=_refused(
            "nothing addressed to alpha is waiting any more — it drained "
            "while this repair was settling")))
        self.assertEqual(typed, [], "the pane was typed into about a backlog "
                                    "that had already drained")
        self.assertEqual(mode, "drained")

    def test_a_pause_that_lands_during_the_settle_stops_it_too(self):
        mode, detail, typed = self._child(DoorBook(settle=_refused(
            "delivery to alpha is paused (the credential wall is up)",
            "paused")))
        self.assertEqual(typed, [])
        self.assertEqual(mode, "paused")
        self.assertIn("credential wall", detail)

    def test_an_unknown_preparation_withholds_and_claims_no_drain(self):
        mode, detail, typed = self._child(DoorBook(settle=_refused(
            "whether rows are still owed to alpha could not be re-read",
            "unknown")))
        self.assertEqual(typed, [])
        self.assertEqual(mode, "withheld",
                         "an UNKNOWN preparation was recorded as something "
                         "other than a deferral")

    def test_a_still_owed_unpaused_seat_IS_typed_into(self):
        """THE CONTROL, unconditional."""
        mode, _detail, typed = self._child(DoorBook())
        self.assertEqual(len(typed), 1)
        self.assertEqual(mode, "resumed")


class _Retryable(str):
    retryable = True


class TheEpisodeIsOneTransactionTest(ResumeTurnBase):
    """The parent opens the episode LAST, and only for a launch that happens;
    the authorization reaches the adapter's act doors on every branch."""

    def _wake(self, install, **over):
        from helm import resumeturn, beacons
        att = {"state": beacons.DEAF_IN_EFFECT, "alarm": True,
               "since": 1789400000.0, "at": 1789400000.0, "waited": 900}
        spawned = []
        with mock.patch.object(resumeturn, "_registered",
                               return_value=("", [4242])), \
                mock.patch.object(resumeturn, "_still_owed",
                                  return_value=(900.0, "")), \
                mock.patch.object(resumeturn, "_delivery_paused",
                                  return_value=""), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=spawned.append), \
                mock.patch.object(beacons, "install_repair",
                                  side_effect=install) as inst:
            got = resumeturn.wake_undelivered(
                "alpha", over.get("session", "s-%s" % id(install)),
                waited=900, att=over.get("att", att))
        return got, spawned, inst

    def test_a_failed_required_install_launches_nothing(self):
        """A required install that fails is a refusal, not a downgrade."""
        got, spawned, inst = self._wake(lambda *a, **k: None)
        inst.assert_called_once()
        self.assertEqual(spawned, [], "an unbound child was launched after "
                                      "the install it needed had failed")
        self.assertEqual(got["action"], "alert", got)
        self.assertIn("NOT launching", got["detail"])

    def test_a_launched_child_carries_the_attempt_it_was_installed_for(self):
        """THE CONTROL, unconditional and through the same door."""
        got, spawned, inst = self._wake(lambda *a, **k: "1789400000.0#1")
        inst.assert_called_once()
        self.assertEqual(got["action"], "wake", got)
        self.assertEqual(got.get("attempt"), "1789400000.0#1")
        self.assertEqual(len(spawned), 1)
        argv = spawned[0]
        self.assertIn("--attempt", argv)
        self.assertEqual(argv[argv.index("--attempt") + 1], "1789400000.0#1")

    def test_the_install_is_the_last_act_before_the_fork(self):  # noqa: VACUOUS_ASSERTION — the absence is inst.assert_not_called() under a paused refusal; its unconditional positive control is test_a_launched_child_carries_the_attempt_it_was_installed_for, which drives the same door past every refusal and observes the install
        """A refusal made by this actuator must find NO episode installed."""
        from helm import resumeturn, beacons
        att = {"state": beacons.DEAF_IN_EFFECT, "alarm": True,
               "since": 1789400000.0, "at": 1789400000.0, "waited": 900}
        with mock.patch.object(resumeturn, "_registered",
                               return_value=("", [4242])), \
                mock.patch.object(resumeturn, "_still_owed",
                                  return_value=(900.0, "")), \
                mock.patch.object(resumeturn, "_delivery_paused",
                                  return_value="state CRED-WALL"), \
                mock.patch.object(resumeturn, "spawn_child") as spawn, \
                mock.patch.object(beacons, "install_repair") as inst:
            got = resumeturn.wake_undelivered("alpha", "s-order", waited=900,
                                              att=att)
        self.assertEqual(got["action"], "paused", got)
        inst.assert_not_called()
        spawn.assert_not_called()
        self.assertIsNone(got.get("attempt"))

    def test_every_deliver_branch_hands_the_admission_to_the_adapter(self):
        """THREE BRANCHES, ONE CONTRACT: the registered, adopted and nameless
        paths all hand the caller's authorization to `adapter.submit`."""
        from helm import resumeturn, orcaadopt

        class Spy(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.admits = []

            def submit(self, handle, text, settle=None, reads=None,
                       interval=None, on_typed=None, admit=None):
                self.admits.append(admit)
                return super().submit(handle, text, settle=0, reads=reads,
                                      interval=interval, on_typed=on_typed,
                                      admit=admit)

        marker = lambda door: _granted()   # noqa: E731

        ad = Spy()

        def reproved(_row, _adapter, action, **_kw):
            return action(ad, "h1", "re-proved h1"), None

        with mock.patch("helm.autocompact._pane_action",
                        side_effect=reproved):
            resumeturn.deliver("codex", "GO", SID, adapter=ad, admit=marker)
        self.assertEqual(ad.admits, [marker], "the registered branch")

        ad = Spy()
        with mock.patch.object(orcaadopt, "resolve",
                               return_value={"pids": [4242]}), \
                mock.patch.object(orcaadopt, "authorized_handle",
                                  return_value=("h1", "bound")):
            resumeturn.deliver("codex", "GO", SID, adapter=ad,
                               pids=[4242], admit=marker)
        self.assertEqual(ad.admits, [marker], "the adopted branch")

        ad = Spy()
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], [])), \
                mock.patch.object(orcaadopt, "_nameless_identity",
                                  return_value=(4242, "its own sid")), \
                mock.patch.object(orcaadopt, "authorized_handle",
                                  return_value=("h1", "bound")):
            mode, detail = resumeturn.deliver("", "GO", SID, adapter=ad,
                                              pids=[4242], admit=marker)
        self.assertEqual(ad.admits, [marker],
                         "the nameless branch (%s: %s)" % (mode, detail))

        # THE CONTROL: a caller with no authorization hands none.
        ad = Spy()
        with mock.patch("helm.autocompact._pane_action",
                        side_effect=reproved):
            resumeturn.deliver("codex", "GO", SID, adapter=ad)
        self.assertEqual(ad.admits, [None])

    def test_the_whole_delivery_retry_carries_the_admission(self):
        from helm import resumeturn, harness
        seen = []

        def fake_deliver(*a, **k):
            seen.append(k.get("admit"))
            if len(seen) == 1:
                k["on_submit"](None, "h1", harness.UNKNOWN,
                               _Retryable("blind pre-read"), None)
                return "unverified", _Retryable("blind pre-read")
            k["on_submit"](None, "h1", harness.DELIVERED, "advanced", None)
            return "resumed", "typed"

        book = DoorBook()
        with mock.patch.object(resumeturn, "_await_or_withdraw",
                               return_value=(False, "")), \
                mock.patch.object(resumeturn, "_prepare_due", side_effect=book), \
                mock.patch.object(resumeturn, "deliver",
                                  side_effect=fake_deliver), \
                mock.patch.object(resumeturn, "recovery_backoff_s",
                                  return_value=0), \
                mock.patch.object(resumeturn, "RECOVERY_ATTEMPTS", 2):
            mode, _detail = resumeturn.child("codex", SID, "GO NOW", 0,
                                             record_key="deaf:codex:%s" % SID)
        self.assertEqual(len(seen), 2,
                         "fixture: the retry loop did not run: %r" % (seen,))
        self.assertTrue(callable(seen[1]),
                        "the RETRY delivery carried no admission")
        self.assertIn("retry", book.asked)
        self.assertEqual(mode, "resumed")


class TheChildReAsksAboutTheOwedRoomTest(ResumeTurnBase):
    """The alarm recorded the room it was raised on; that room rides the fork
    and pins every preparation, so a rotating sample elsewhere cannot starve
    the one room the question is about."""

    def test_the_census_is_pinned_to_the_named_room(self):
        from helm import resumeturn, beacons
        seen = []

        def spy(seat, session=None, room=None, **_k):
            seen.append(room)
            return 300.0, None, {"oldest": (room, "r9"), "scanned": (room,),
                                 "seen": ((room, "r9"),)}

        with mock.patch.object(beacons, "undrained", side_effect=spy):
            waited, why = resumeturn._still_owed("alpha", "s1", 900.0,
                                                 room="foreign-x")
        self.assertEqual(waited, 300.0, why)
        self.assertEqual(seen, ["foreign-x"], "the re-check was not pinned")
        # THE CONTROL: with no room named, the home room is asked.
        seen.clear()
        with mock.patch.object(beacons, "undrained", side_effect=spy), \
                mock.patch("helm.seats.roster",
                           return_value={"alpha": {"home_room": "home-a"}}):
            resumeturn._still_owed("alpha", "s1", 900.0)
        self.assertEqual(seen, ["home-a"])

    def test_the_room_crosses_the_fork_and_reaches_every_preparation(self):
        from helm import resumeturn, beacons
        att = {"state": beacons.DEAF_IN_EFFECT, "alarm": True,
               "since": 1789400000.0, "at": 1789400000.0, "waited": 900,
               "undrained_row": ["foreign-x", "r9"]}
        spawned = []
        with mock.patch.object(resumeturn, "_registered",
                               return_value=("", [4242])), \
                mock.patch.object(resumeturn, "_still_owed",
                                  return_value=(900.0, "")), \
                mock.patch.object(resumeturn, "_delivery_paused",
                                  return_value=""), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=spawned.append), \
                mock.patch.object(beacons, "install_repair",
                                  return_value="1789400000.0#1"):
            got = resumeturn.wake_undelivered("alpha", "s-room", waited=900,
                                              att=att)
        self.assertEqual(got["action"], "wake", got)
        argv = spawned[0]
        self.assertEqual(argv[argv.index("--owed-room") + 1], "foreign-x")
        asked = []

        def book(seat, session, room=None, door="", **_carried):
            asked.append((door, room))
            return _refused("drained")

        with mock.patch.object(resumeturn, "_await_or_withdraw",
                               return_value=(False, "")), \
                mock.patch.object(resumeturn, "_prepare_due", side_effect=book):
            resumeturn.child("alpha", "s1", "GO", 0,
                             record_key="deaf:alpha:s1", owed_room="foreign-x")
        self.assertEqual(asked, [("settle", "foreign-x")])


class TheSharedChildServesTwoQuestionsTest(ResumeTurnBase):
    """`child` is the ONE deliverer for TWO callers. The deaf repair types
    because rows are owed and prepares that at every act; the compaction
    resume types because the seat compacted and carries no authorization."""

    def _run(self, record_key, book):
        from helm import resumeturn
        typed = []
        with mock.patch.object(resumeturn, "_await_or_withdraw",
                               return_value=(False, "")), \
                mock.patch.object(
                    resumeturn, "deliver",
                    side_effect=lambda *a, **k: (typed.append(k.get("admit"))
                                                 or ("resumed", "typed"))), \
                mock.patch.object(resumeturn, "_prepare_due", side_effect=book):
            mode, _d = resumeturn.child("alpha", "s1", "text", 0,
                                        record_key=record_key)
        return mode, typed

    def test_an_ORDINARY_compaction_resume_needs_no_backlog(self):  # noqa: VACUOUS_ASSERTION — the same arm asserts the compaction resume delivered exactly once
        book = DoorBook(settle=_refused("nothing owed"))
        mode, typed = self._run(None, book)
        self.assertEqual(len(typed), 1,
                         "a compacted seat with a clean inbox was refused its "
                         "context resume")
        self.assertEqual(typed, [None],
                         "the compaction resume carried an authorization")
        self.assertEqual(book.asked, [])
        self.assertEqual(mode, "resumed")

    def test_the_DEAF_repair_still_needs_one(self):
        """THE CONTROL: the leg the preparation was written for obeys it."""
        mode, typed = self._run("deaf:alpha:s1",
                                DoorBook(settle=_refused("nothing owed")))
        self.assertEqual(typed, [])
        self.assertEqual(mode, "drained")

    def test_the_recovery_path_is_admitted_too(self):  # noqa: VACUOUS_ASSERTION — the absence is no recovery Enter under a paused preparation; test_one_recovery_enter_allowed_then_the_next_refused reaches the real recovery and presses its Enter
        from helm import harness, resumeturn
        recovered = []

        def submitted(*_a, **kw):
            cb = kw.get("on_submit")
            if cb:
                cb(object(), "h1", harness.NOT_DELIVERED, None, "g1")
            return "unverified", "held"

        book = DoorBook(recovery=_refused("the wall went up mid-settle",
                                          "paused"))
        with mock.patch.object(resumeturn, "_await_or_withdraw",
                               return_value=(False, "")), \
                mock.patch.object(resumeturn, "deliver", side_effect=submitted), \
                mock.patch.object(resumeturn, "_matching_injection",
                                  return_value={"handle": "h1"}), \
                mock.patch.object(resumeturn, "injection_persistent",
                                  return_value=True), \
                mock.patch.object(resumeturn, "recovery_persist_s",
                                  return_value=0), \
                mock.patch.object(
                    resumeturn, "recover_injection",
                    side_effect=lambda *a, **k: (recovered.append(a) or
                                                 (harness.DELIVERED, "ok"))), \
                mock.patch.object(resumeturn, "_prepare_due", side_effect=book), \
                mock.patch.object(resumeturn, "_route_recovery",
                                  return_value=(None, "synthetic route")):
            mode, _d = resumeturn.child("alpha", "s1", "text", 0,
                                        record_key="deaf:alpha:s1")
        self.assertEqual(recovered, [],
                         "Enter was pressed after the pause landed")
        self.assertEqual(mode, "paused")


class KeyPresenceIsNotAChargeReceiptTest(ResumeTurnBase):
    """The parent stamps NONBLOCKING and is skipped on contention, so a record
    key proves an episode NAMESPACE exists and never that it was charged.
    Skipping the child's charge on the key alone loses the charge exactly when
    the parent lost that race, and a FOURTH repair then fits inside a cap of
    three because only two of the priors were ever paid."""

    def _at(self, key):
        from helm import resumeturn
        return list(((resumeturn._peek(key) or {}).get("at")) or [])

    def _child(self, key):
        from helm import resumeturn
        with mock.patch.object(resumeturn, "_await_or_withdraw",
                               return_value=(True, "withdrawn for the arm")):
            resumeturn.child("alpha", "s1", "text", 0, record_key=key)

    def test_the_child_charges_when_the_parents_stamp_was_SKIPPED(self):
        key = "deaf:alpha:s1"
        self.assertEqual(self._at(key), [])      # the parent lost the race
        self._child(key)
        self.assertEqual(len(self._at(key)), 1,
                         "an episode nobody charged went unbilled, so the cap "
                         "admits one more repair than it ever paid for")

    def test_the_child_does_NOT_charge_when_the_parent_DID(self):
        """THE CONTROL: the ordinary path must still spend exactly one slot."""
        from helm import resumeturn
        key = "deaf:beta:s1"
        resumeturn._record(key, "s1", "resume", "", count_it=True)
        self.assertEqual(len(self._at(key)), 1)
        self._child(key)
        self.assertEqual(len(self._at(key)), 1)


class AnInjectionIsProvedByItsPaneNotByItsBucketTest(ResumeTurnBase):
    """task/2463 finding 6. Injection records are filed under the key of
    whichever leg wrote them, and `_matching_injection` demanded that key
    match the CALLER's episode. The ordinary delivery leg files under a bare
    seat/session key while the DEAF-IN-EFFECT repair runs under its own
    `deaf:<seat>:<session>` namespace, so a pane still holding the exact text
    helm put there was refused for recovery by the leg that could have freed
    it -- and the wedged pane had no exit."""

    TEXT = "Run helm seat resume-turn --show"
    HANDLE = "h-bucket"

    def _write(self, key="main:alpha:s1", text=None, handle=None):
        from helm import pk, resumeturn
        text = self.TEXT if text is None else text
        inj = {"text": text, "handle": handle or self.HANDLE,
               "generation": "g1",
               "digest": resumeturn._injection_digest(text),
               # THE HORIZON IS ABSOLUTE AND COMPARED AGAINST WALL TIME, so a
               # round number that LOOKS large is already in the past and the
               # reader drops the record as expired -- which reads as the
               # refusal this arm is about.
               "held_at": 1, "expires_at": time.time() + 3600,
               "session": "s1", "adapter": "fake", "recorded_at": 0}
        path = resumeturn.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, {key: {"injection": inj}})
        return inj

    def test_another_legs_record_is_recoverable_when_the_pane_proves_it(self):
        from helm import resumeturn
        self._write()
        got = resumeturn._matching_injection(
            "deaf:alpha:s1", self.HANDLE, self.TEXT, "g1")
        self.assertIsNotNone(
            got, "a pane holding helm's exact text at the same generation was "
                 "refused because a different leg recorded it")
        self.assertEqual(got["key"], "main:alpha:s1")

    def test_the_SAME_legs_record_is_still_recoverable(self):
        """THE CONTROL: the bucket check is loosened, so the ordinary same-leg
        path it also governs must still work."""
        from helm import resumeturn
        self._write(key="deaf:alpha:s1")
        self.assertIsNotNone(resumeturn._matching_injection(
            "deaf:alpha:s1", self.HANDLE, self.TEXT, "g1"))

    def test_provenance_still_decides_and_it_refuses(self):
        """AND THE REFUSALS THE BUCKET CHECK WAS STANDING IN FRONT OF. What
        proves a record is helm's is the text, its digest, the handle and the
        generation -- so each of those, made wrong, must still refuse."""
        from helm import resumeturn
        self._write()
        for label, args in (
                ("another pane", (self.HANDLE + "-other", self.TEXT, "g1")),
                ("another text", (self.HANDLE, self.TEXT + " tampered", "g1")),
                ("another generation", (self.HANDLE, self.TEXT, "g2")),
        ):
            with self.subTest(label):
                self.assertIsNone(resumeturn._matching_injection(
                    "deaf:alpha:s1", *args))


class ADeafInEffectPaneIsNudgedNotReArmedTest(ResumeTurnBase):
    """task/2463 — the seat whose wake path is live and whose rows still sit.

    RE-ARMING THE BEACON FIXES NOTHING THERE, because the waiter is not what
    failed: the delivery reached an armed route and never became a turn. So
    the repair has to reach the PANE, and this is the leg that does it.

    IT IS NOT THE COMPACTION LEG. `hook` answers a context LOSS — it gates on
    the SessionStart source, forgets the injector ledger for the session, and
    composes a post-compaction resume. A seat that is simply not consuming has
    lost no context, so only the pane proof, the child argv and the fork are
    shared.
    """

    # THE SESSION THE FIXTURE'S SPAWN REGISTER ACTUALLY NAMES, taken from the
    # fixture's own constant rather than retyped. The pane proof is
    # session-anchored AND exact, so a sid that merely LOOKS the same is
    # refused for the right reason and the arm would measure that refusal
    # instead of the wake.
    SID = SID

    #: The re-derived backlog these arms stand on. `wake_undelivered` asks
    #: the consumption census AGAIN at the actuator, and this fixture has no
    #: chat store, so that read answers "nothing waiting" and the arms below
    #: would all measure the drained refusal instead of the fork. Its own
    #: rung is measured separately — see ThePaneIsNotTypedIntoWithoutAReason.
    OWED = 4020

    def wake(self, owed=OWED, **kw):
        with mock.patch.object(resumeturn, "_still_owed",
                               return_value=(owed, "")), \
                mock.patch.object(resumeturn, "_delivery_paused",
                                  return_value=""), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.spawned.append) as fork:
            got = resumeturn.wake_undelivered("codex", self.SID, **kw)
        return got, fork

    def test_it_forks_the_deliverer_with_this_seats_own_text(self):
        got, fork = self.wake(waited=4020)
        self.assertEqual(got["action"], "wake", got)
        fork.assert_called_once()
        argv = self.spawned[0]
        self.assertEqual(argv[1:4], ["seat", "resume-turn", "--deliver"])
        self.assertIn("--seat", argv)
        self.assertEqual(argv[argv.index("--seat") + 1], "codex")
        self.assertEqual(argv[argv.index("--session") + 1], self.SID)
        text = io.open(argv[argv.index("--text-file") + 1],
                       encoding="utf-8").read()
        self.assertIn("4020s", text)
        self.assertIn("helm chat read", text)
        # THE SENTENCE SAYS WHAT THIS IS NOT, because a seat reading a
        # post-compaction resume would go looking for context it never lost.
        self.assertNotIn("compact", text.lower())

    def test_a_second_pass_inside_the_window_debounces_and_forks_NOTHING(self):
        """A CENSUS RUNS ON A CADENCE, so without this the fallback types into
        the same composer on every pass for as long as the rows sit."""
        first, _fork = self.wake(waited=4020)
        self.assertEqual(first["action"], "wake")
        again, fork = self.wake(owed=4080, waited=4080)
        self.assertEqual(again["action"], "debounce", again)
        fork.assert_not_called()

    def test_the_pane_is_NOT_typed_into_once_the_backlog_is_gone(self):
        """A VALID PANE IS NOT A STILL-OWED REASON. The census that ordered
        this repair ran earlier; between then and now the seat may have
        drained everything, and pane identity would still be valid, the
        cooldown would still permit, and the seat would be told to catch up on
        nothing."""
        with mock.patch.object(resumeturn, "_still_owed",
                               return_value=(None, "the backlog is gone")), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.spawned.append) as fork:
            got = resumeturn.wake_undelivered("codex", self.SID, waited=4020)
        self.assertEqual(got["action"], "drained", got)
        self.assertEqual(got["detail"], "the backlog is gone")
        fork.assert_not_called()
        # THE MUST-HIT: the same fixture with a backlog DOES fork, so this
        # refusal is about the re-derived reason and not about a fixture that
        # cannot reach the fork at all.
        again, forked = self.wake(waited=4020)
        self.assertEqual(again["action"], "wake", again)
        forked.assert_called_once()

    def test_a_paused_delivery_is_not_typed_into_through_the_side_door(self):
        """THE PAUSE THAT GOVERNS EVERY OTHER DELIVERY GOVERNS THIS ONE. A
        seat behind a verified credential wall is deliberately holding its
        addressed rows until it is HEALTHY; a pane nudge is a delivery with
        the pause left out."""
        with mock.patch.object(resumeturn, "_still_owed",
                               return_value=(4020, "")), \
                mock.patch.object(resumeturn, "_delivery_paused",
                                  return_value="state CREDENTIAL_WALL"), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.spawned.append) as fork:
            got = resumeturn.wake_undelivered("codex", self.SID, waited=4020)
        self.assertEqual(got["action"], "paused", got)
        self.assertIn("CREDENTIAL_WALL", got["detail"])
        # AND THE ALARM IS NOT CANCELLED BY THE PAUSE — the backlog is still
        # owed, which is the half a "we skipped it" refusal would lose.
        self.assertIn("stays owed", got["detail"])
        fork.assert_not_called()
        again, forked = self.wake(waited=4020)
        self.assertEqual(again["action"], "wake", again)
        forked.assert_called_once()

    def test_the_child_carries_the_parents_episode_namespace(self):
        """THE NAMESPACE CROSSES THE FORK OR IT DOES NOT EXIST. The child is
        where the bookkeeping actually lands, and it was re-deriving the bare
        seat key — the COMPACTION leg's key — so a pane repair spent the
        compaction debounce, spiral and cap."""
        _got, _fork = self.wake(waited=4020)
        argv = self.spawned[0]
        self.assertIn("--record-key", argv)
        self.assertEqual(argv[argv.index("--record-key") + 1],
                         "deaf:codex:%s" % self.SID)

    def test_the_argv_the_alarm_PRINTS_is_one_the_parser_accepts(self):
        """DRIVE THE EXACT DISPLAYED COMMAND. The census line advertised
        `--deliver`, which is the detached child's own interface: its parser
        returns 2 without --session and --text-file, so the repair helm told
        an operator to run could not run. Reading the string proves nothing —
        only handing it to the parser does."""
        from helm import beacons
        argv = beacons.repair_argv("codex").split()
        self.assertEqual(argv[:3], ["helm", "seat", "resume-turn"])
        with mock.patch.object(resumeturn, "_still_owed",
                               return_value=(4020, "")), \
                mock.patch.object(resumeturn, "_delivery_paused",
                                  return_value=""), \
                mock.patch.object(resumeturn, "_seat_session",
                                  return_value=(self.SID, "")), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.spawned.append) as fork:
            rc = resumeturn.cmd_resume_turn(argv[3:])
        self.assertEqual(rc, 0, "the advertised repair was refused by its own "
                                "parser")
        fork.assert_called_once()
        # THE MUST-HIT, so the arm is about THIS argv and not about a parser
        # that accepts anything: the old advertisement still returns 2.
        self.assertEqual(
            resumeturn.cmd_resume_turn(["--seat", "codex", "--deliver"]), 2)

    def test_the_manual_door_reads_the_session_from_the_roster(self):
        """AN OPERATOR DOES NOT HOLD A SID. The repair names a seat; the
        session is where helm already writes it."""
        with mock.patch.object(resumeturn, "_seat_session",
                               return_value=(None, "codex is not in the "
                                                   "roster")) as look, \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.spawned.append) as fork:
            rc = resumeturn.cmd_resume_turn(["--nudge", "--seat", "codex"])
        self.assertEqual(rc, 1)
        look.assert_called_once_with("codex")
        fork.assert_not_called()

    def test_an_unproven_pane_is_an_alert_and_forks_NOTHING(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is fork.assert_not_called(); its unconditional positive control is test_it_forks_the_deliverer_with_this_seats_own_text, which drives THIS function on THIS fixture and does fork
        """THE PROOF IS THE PARENT'S JOB AND IT REFUSES OUT LOUD. A pane the
        parent cannot identify must never be typed into, and the reason is
        returned rather than swallowed."""
        with mock.patch.object(resumeturn, "_registered",
                               return_value=("no pane evidence", ())), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.spawned.append) as fork:
            got = resumeturn.wake_undelivered("codex", self.SID, waited=4020)
        self.assertEqual(got, {"action": "alert",
                               "detail": "no pane evidence"})
        fork.assert_not_called()

    def test_its_cap_is_its_own_and_never_the_compaction_leg(self):
        """ONE SEAT'S NUDGES MUST NOT SPEND THE OTHER LEG'S BUDGET, and a
        SPIRAL verdict is a statement about compactions that must never
        silence a seat which has not compacted."""
        self.wake(waited=4020)
        state = resumeturn._peek("deaf:codex:%s" % self.SID)
        self.assertIsInstance(state, dict, resumeturn.state_path())
        self.assertIsNone(resumeturn._peek("codex:%s" % self.SID),
                          "the nudge wrote into the compaction leg's key")


class AnUnreadableResumeStoreRefusesTest(ResumeTurnBase):
    """task/2530 (a): the launch decision reads the store the charge writes.

    The charge (`_stamp`) reads the resume state strictly and writes nothing
    when the store exists and cannot be read. The decision before it read the
    SAME store leniently, so an unreadable store answered {} there: no stamp,
    no debounce, no spiral, no cap, and every compaction resumed. A missing
    store and an unreadable one shared the value "nothing recorded", and only
    the first one means that.

    Every fixture here is a real file at the real `state_path()` in a temp
    HELM_HOME, read through the shipped readers. The must-hit in
    `corrupt_store` proves the bytes are the input the lenient read collapses
    to {}, so a green arm is about the reader and not about a fixture that
    never reached it."""

    CORRUPT = "{not json\n"

    def corrupt_store(self):
        path = resumeturn.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.CORRUPT)
        with self.assertRaises(ValueError):
            pk.read_json(path, {}, strict=True)
        self.assertEqual(pk.read_json(path, {}), {})
        return path

    def store_bytes(self):
        with open(resumeturn.state_path(), encoding="utf-8") as f:
            return f.read()

    def deliveries(self):
        return [argv for argv in self.spawned if "--deliver" in argv]

    def wake(self):
        calls, err = [], io.StringIO()
        with mock.patch.object(seats, "beacon_procs",
                               return_value=([123], None)), \
                mock.patch("helm.chat.post",
                           side_effect=lambda text, **kw:
                           calls.append((text, kw))), \
                mock.patch("sys.stderr", err):
            ok = resumeturn.wake_alert({
                "display": "codex", "reason": "wake up", "seat": "codex",
                "session": SID, "episode": "ep1"})
        return (ok, [t for t, kw in calls if kw.get("dm")],
                [t for t, kw in calls if not kw.get("dm")], err.getvalue())

    def test_a_compaction_on_an_unreadable_store_injects_nothing_and_says_so(self):
        self.corrupt_store()
        first = self.run_hook()
        second = self.run_hook()
        self.assertEqual(first["action"], "unknown", first)
        self.assertEqual(second["action"], "unknown", second)
        self.assertIn("could not be read", first["detail"])
        self.assertEqual(self.deliveries(), [])
        wakes = self.wake_children()
        self.assertEqual(len(wakes), 2, "each refusal must alert the room")
        self.assertIn("could not be read", wakes[0]["reason"])
        # NO DM AUTHORITY, for the reason spiral and capped carry none: the
        # guard that would stop poking this seat cannot be judged.
        self.assertEqual([w["seat"] for w in wakes], ["", ""])
        self.assertEqual(self.store_bytes(), self.CORRUPT)

    def test_a_readable_store_holding_another_key_launches_then_debounces(self):
        """The control: a store that EXISTS and reads launches exactly as a
        missing one does, and the second firing is the same episode."""
        path = resumeturn.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, {"other-seat": {"at": [], "mode": "spawned"}})
        first = self.run_hook()
        second = self.run_hook()
        self.assertEqual((first["action"], second["action"]),
                         ("spawned", "debounce"))
        self.assertEqual(len(self.deliveries()), 1)
        self.assertIn("other-seat", pk.read_json(path, {}, strict=True))

    def test_a_deaf_nudge_on_an_unreadable_store_forks_nothing(self):
        self.corrupt_store()
        with mock.patch.object(resumeturn, "_still_owed",
                               return_value=(4020, "")), \
                mock.patch.object(resumeturn, "_delivery_paused",
                                  return_value=""), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=self.spawned.append) as fork:
            got = resumeturn.wake_undelivered("codex", SID, waited=4020)
        self.assertEqual(got["action"], "unknown", got)
        self.assertIn("could not be read", got["detail"])
        # The refusal stamp is not attempted, so it cannot be blamed on a lock.
        self.assertNotIn("locked", got["detail"])
        fork.assert_not_called()
        self.assertEqual(self.store_bytes(), self.CORRUPT)

    def test_a_wake_on_an_unreadable_store_posts_the_room_row_and_no_dm(self):
        """The episode debounce reads the same store. Unreadable, it cannot
        say whether this episode already woke the seat, so the DM is refused
        and the room row, which carries no debounce, still posts."""
        self.corrupt_store()
        ok, dms, rooms, err = self.wake()
        self.assertTrue(ok)
        self.assertEqual(len(rooms), 1, rooms)
        self.assertEqual(dms, [])
        self.assertIn("could not be read", err)

    def test_a_wake_on_a_readable_store_still_dms_once_per_episode(self):
        """The control: the same payload on a readable store DMs, and a
        replay of its episode is debounced."""
        ok, dms, rooms, _err = self.wake()
        self.assertTrue(ok)
        self.assertEqual((len(dms), len(rooms)), (1, 1))
        _ok, again, rooms, err = self.wake()
        self.assertEqual((len(again), len(rooms)), (0, 1))
        self.assertIn("wake debounced", err)

    def test_the_status_surface_names_an_unreadable_store(self):
        path = self.corrupt_store()
        lines = "\n".join(resumeturn.report_lines())
        self.assertIn("UNREADABLE", lines)
        self.assertIn(path, lines)
        self.assertNotIn("no compaction resume recorded yet", lines)

    def run_installed_hook(self):
        """The SessionStart hook as hooks.py installs it: `seat resume-turn
        --hook-json` with the payload on stdin. Its stdout is the only
        sentence the compacted seat itself reads; the room alert is addressed
        to the room."""
        return self.run_installed(self.payload())

    def test_the_installed_hook_tells_the_compacted_seat_why_it_refused(self):
        """The spiral and capped refusals print a line to the seat. An unknown
        refusal printed nothing, so the seat lost its resume and was not told
        why."""
        self.corrupt_store()
        rc, out = self.run_installed_hook()
        self.assertEqual(rc, 0)
        # MUST-HIT: the stdin payload reached the unknown branch, which alerts
        # the room once and injects nothing.
        self.assertEqual(len(self.wake_children()), 1)
        self.assertEqual(self.deliveries(), [])
        self.assertTrue(out.startswith("helm seat resume-turn: unknown"), out)
        self.assertIn("could not be read", out)

    def test_the_installed_hook_on_a_readable_store_still_says_it_spawned(self):
        """The control, through the same stdin and stdout: a store that reads
        launches and says so."""
        rc, out = self.run_installed_hook()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.deliveries()), 1)
        self.assertTrue(out.startswith("helm seat resume-turn: spawned"), out)


class ANonObjectResumeStoreIsOneStoreToEveryReaderTest(ResumeTurnBase):
    """task/2530 (a), the readers beside `_peek`: one store has one answer.

    `_peek` calls a store holding JSON that is not an object unreadable, so the
    launch decision refuses it as unknown and doctor FAILs it. The bookkeeping
    stamp, the ledger mutation and the obsolete-act check each read the same
    file as `read_json(..., strict=True) or {}`. A JSON `null` or `[]` parses
    without raising and is falsy, so those three read it as a clean empty
    store: the act door granted a clean attempt, and the first bookkeeping
    write replaced the file, after which the launch decision said "resume"
    over records it never read.

    Every fixture is real bytes at the real `state_path()`. The must-hit in
    `plant` proves the strict read does NOT raise on them and returns a falsy
    non-object, and that `_peek` already refuses them, so each arm is about a
    reader that disagrees with `_peek` and not about bytes nothing parses."""

    SHAPES = ("null\n", "[]\n")

    def setUp(self):
        super().setUp()
        resumeturn._OBSOLETE_HERE.clear()
        self.addCleanup(resumeturn._OBSOLETE_HERE.clear)

    def decision(self):
        return resumeturn._decide(resumeturn._peek("codex"), time.time())[0]

    def plant(self, raw):
        path = resumeturn.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        return path

    def plant_non_object(self, raw):
        path = self.plant(raw)
        got = pk.read_json(path, {}, strict=True)
        self.assertFalse(got)
        self.assertNotIsInstance(got, dict)
        self.assertEqual(self.decision(), "unknown")

    def store_bytes(self):
        with open(resumeturn.state_path(), encoding="utf-8") as f:
            return f.read()

    def test_a_bookkeeping_stamp_writes_nothing_over_a_non_object_store(self):
        for raw in self.SHAPES:
            with self.subTest(store=raw):
                self.plant_non_object(raw)
                err = io.StringIO()
                with mock.patch("sys.stderr", err):
                    got = resumeturn._record("other-seat", SID, "spawned", "x")
                self.assertIsNone(got, "the stamp read a non-object store "
                                       "as empty and wrote over it")
                self.assertEqual(self.store_bytes(), raw)
                self.assertIn("state write failed", err.getvalue())
                self.assertEqual(self.decision(), "unknown")

    def test_a_ledger_mutation_raises_over_a_non_object_store(self):
        for raw in self.SHAPES:
            with self.subTest(store=raw):
                self.plant_non_object(raw)
                with self.assertRaises(ValueError) as cm:
                    resumeturn._mutate_entry(
                        "codex", lambda entry: (None, True))
                self.assertIn(str(cm.exception), resumeturn._store()[1])
                self.assertEqual(self.store_bytes(), raw)

    def test_the_obsolete_act_check_refuses_a_non_object_store(self):
        for raw in self.SHAPES:
            with self.subTest(store=raw):
                self.plant_non_object(raw)
                got = resumeturn._obsolete_act("codex", "a1")
                self.assertTrue(got and got.get("unreadable"),
                                "the act door read a non-object store as a "
                                "clean attempt: %r" % (got,))

    def test_an_empty_object_store_is_clean_to_every_reader(self):
        """The control: `{}` reads, so the act door finds no record, the
        stamp and the mutation write, and the launch decision resumes."""
        path = self.plant("{}\n")
        self.assertEqual(pk.read_json(path, {}, strict=True), {})
        self.assertIsNone(resumeturn._obsolete_act("codex", "a1"))
        self.assertEqual(self.decision(), "resume")
        self.assertIsNotNone(
            resumeturn._record("other-seat", SID, "spawned", "x"))
        self.assertEqual(
            resumeturn._mutate_entry("codex", lambda entry: ("ok", True)),
            "ok")
        self.assertEqual(sorted(pk.read_json(path, {}, strict=True)),
                         ["codex", "other-seat"])
        self.assertEqual(self.decision(), "resume")


def _within(test, fn, fifo, seconds=5):
    """fn() on a thread with a deadline. A reader blocked opening `fifo` is
    released by opening its write end, so a RED arm fails instead of wedging
    the suite."""
    import threading
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:      # noqa: BLE001 — re-raised below
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        t.join(5)
        test.fail("blocked for %ss opening the FIFO at %s" % (seconds, fifo))
    if "error" in box:
        raise box["error"]
    return box["value"]


class AResumeStoreThatIsNotAFileRefusesWithoutBlockingTest(ResumeTurnBase):
    """task/2530 r1 P2: `open()` on a FIFO with no writer blocks. The
    one strict reader opened the resume state that way, so a FIFO at
    `state_path()` hung the doctor rung this lane added, and the launch
    decision behind it, instead of naming the store. Every strict authority
    read goes through `pk.read_json(strict=True)`, so that is where a
    non-regular file is refused, before anything can block on it.

    The fixture is a real FIFO at the real `state_path()`; the must-hit proves
    it is one."""

    def plant_fifo(self):
        import stat
        path = resumeturn.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.mkfifo(path)
        self.assertTrue(stat.S_ISFIFO(os.stat(path).st_mode))
        return path

    def test_the_strict_read_refuses_a_fifo_and_names_it(self):
        path = self.plant_fifo()

        def read():
            with self.assertRaises(ValueError) as cm:
                pk.read_json(path, {}, strict=True)
            return str(cm.exception)
        why = _within(self, read, path)
        self.assertIn("not a regular file", why)
        self.assertIn(path, why)

    def test_the_launch_decision_refuses_a_fifo_store_as_unknown(self):
        path = self.plant_fifo()
        entry = _within(self, lambda: resumeturn._peek("codex"), path)
        self.assertIsInstance(entry, resumeturn.Unreadable)
        self.assertIn("not a regular file", entry.why)
        self.assertEqual(resumeturn._decide(entry, time.time())[0], "unknown")

    def test_a_symlink_to_a_regular_store_still_reads(self):
        """Control: the refusal is about the file the path opens, so a store
        reached through a symlink reads as it did."""
        path = resumeturn.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        real = path + ".real"
        pk.write_json(real, {"codex": {"at": [], "mode": "spawned"}})
        os.symlink(real, path)
        self.assertEqual(sorted(pk.read_json(path, {}, strict=True)), ["codex"])
        self.assertNotIsInstance(resumeturn._peek("codex"), resumeturn.Unreadable)

    def test_absent_and_directory_stores_keep_their_answers(self):  # noqa: VACUOUS_ASSERTION — the directory half asserts a raise and an Unreadable, unconditional positive controls on the same reader
        """Controls: a missing store is still empty, and a directory still
        refuses."""
        path = resumeturn.state_path()
        self.assertFalse(os.path.lexists(path))
        self.assertEqual(pk.read_json(path, {}, strict=True), {})
        self.assertIsNone(resumeturn._peek("codex"))
        os.makedirs(path)
        with self.assertRaises((OSError, ValueError)):
            pk.read_json(path, {}, strict=True)
        self.assertIsInstance(resumeturn._peek("codex"), resumeturn.Unreadable)
        # A refused read closes what it opened: a roster reader polls every
        # beacon pass, so one leaked descriptor per refusal is a slow outage.
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            with self.assertRaises(OSError) as cm:
                pk.read_json(path, {}, strict=True)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        self.assertIn(path, str(cm.exception))



class ALenientReadOfANonFileAnswersItsDefaultTest(ResumeTurnBase):
    """task/2536, found by the adversarial verify of task/2530 r2: the STRICT
    read opens without blocking, but the lenient read `pk.read_json(path,
    default)` still did a blocking `open()`, so a FIFO at `state_path()` hung
    `recorded_injections` and `show_directive`, which `helm seat composers`
    and `helm seat resume-turn --show` reach. A lenient read answers its
    default for a file it cannot read; a store that is not a regular file is
    one, answered at once. The census of lenient callers found none that
    reads a pipe on purpose (every one names a helm store path)."""

    plant_fifo = AResumeStoreThatIsNotAFileRefusesWithoutBlockingTest.plant_fifo

    def test_the_lenient_read_answers_its_default_for_a_fifo(self):
        path = self.plant_fifo()
        self.assertEqual(
            _within(self, lambda: pk.read_json(path, {"default": 1}), path),
            {"default": 1})

    def test_a_fifo_fed_a_valid_store_still_answers_the_default(self):
        """The refusal is the file type, not a read that happened to fail: a
        non-blocking read of a writerless FIFO ends at EOF and fails to parse
        anyway, so only a FIFO whose writer already queued valid JSON tells
        the type check apart from the parse."""
        path = self.plant_fifo()
        writer = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, writer)
        os.write(writer, b'{"codex": {"at": []}}')
        self.assertEqual(
            _within(self, lambda: pk.read_json(path, {"default": 1}), path),
            {"default": 1})

    def test_the_resume_store_readers_do_not_block_on_a_fifo(self):  # noqa: VACUOUS_ASSERTION — the empty answers are the defaults a regular store replaces, shown in test_a_regular_and_a_missing_store_keep_their_lenient_answers; this arm is about returning at all inside the deadline
        path = self.plant_fifo()
        self.assertEqual(_within(self, lambda: resumeturn.recorded_injections(
            include_expired=True), path), {})
        self.assertIsNone(_within(
            self, lambda: resumeturn.show_directive("0" * 16), path))
        self.assertEqual(_within(self, lambda: resumeturn._directive_authority(
            "Run `helm seat resume-turn --show %s`" % ("0" * 16)), path)[0],
            None)

    def test_a_regular_and_a_missing_store_keep_their_lenient_answers(self):
        """Controls: a regular store reads, through a symlink too, and a
        missing one is the default."""
        path = resumeturn.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.assertEqual(pk.read_json(path, {"default": 1}), {"default": 1})
        real = path + ".real"
        pk.write_json(real, {"codex": {"at": []}})
        os.symlink(real, path)
        self.assertEqual(pk.read_json(path, {"default": 1}),
                         {"codex": {"at": []}})


class AWakePayloadIsWholeJSONOrRefusedTest(unittest.TestCase):
    """The pipe bound cuts the REASON, never the serialized payload.

    The defect the arms below pin: `json.dumps(payload)[:4096]` shipped bytes
    torn mid-JSON, and the only reader of that pipe answers a torn payload
    with "wake payload malformed" and exits — a lost alert whose written form
    said nothing about why it was lost.
    """

    def payload(self, reason, display="seat-x"):
        return {"display": display, "reason": reason, "seat": "s",
                "session": SID, "episode": "", "record_key": "",
                "mode": "alert", "holder": ""}

    def test_a_reason_past_the_pipe_bound_round_trips_as_json_saying_so(self):
        reason = "why " * 4000
        body = resumeturn._fit_reason(self.payload(reason),
                                      resumeturn.WAKE_PIPE_BOUND)
        self.assertLessEqual(len(body), resumeturn.WAKE_PIPE_BOUND)
        self.assertGreater(len(body), 4000,
                           "the bisection gave up early — the bound above is "
                           "met by a near-empty payload too, so this is the "
                           "control that says the reason is still in there")
        got = json.loads(body.decode("utf-8"))      # whole JSON, not a tear
        self.assertIn("[cut: ", got["reason"])
        self.assertIn("of %d chars]" % len(reason), got["reason"])
        kept = int(re.search(r"\[cut: (\d+) of", got["reason"]).group(1))
        self.assertEqual(got["reason"], pk.cut_marked(reason, kept))
        self.assertTrue(reason.startswith(got["reason"][:kept]))

    def test_a_reason_inside_the_bound_carries_no_mark_and_is_unchanged(self):
        """The must-hit control: an under-bound value is byte-identical, so
        the mark above is evidence of a real loss and not decoration."""
        body = resumeturn._fit_reason(self.payload("the composer is dirty"),
                                      resumeturn.WAKE_PIPE_BOUND)
        self.assertEqual(json.loads(body.decode("utf-8"))["reason"],
                         "the composer is dirty")

    def test_escaping_is_measured_on_the_serialized_bytes_not_guessed(self):
        """A quote costs two serialized bytes and a control character six, so
        a character count cannot stand in for the pipe's byte bound."""
        reason = '"\n' * 3000
        body = resumeturn._fit_reason(self.payload(reason),
                                      resumeturn.WAKE_PIPE_BOUND)
        self.assertLessEqual(len(body), resumeturn.WAKE_PIPE_BOUND)
        self.assertGreater(len(body), 4000)
        self.assertIn("[cut: ", json.loads(body.decode("utf-8"))["reason"])

    def test_a_payload_that_cannot_fit_without_its_reason_is_refused(self):
        """Cutting the one field this function owns cannot meet the bound, so
        it writes nothing rather than bytes the reader will reject."""
        over = self.payload("short", display="D" * 5000)
        self.assertIsNone(resumeturn._fit_reason(
            over, resumeturn.WAKE_PIPE_BOUND))
        self.assertEqual(over["reason"], "short")   # left as handed in

    def test_the_child_reads_back_what_the_bound_produced(self):
        """THE CONSUMER, not a model of it: the same bytes through the real
        `--alert` door reach `wake_alert` as a parsed payload."""
        body = resumeturn._fit_reason(self.payload("why " * 4000),
                                      resumeturn.WAKE_PIPE_BOUND)
        r, w = os.pipe()
        os.write(w, body)
        os.close(w)
        with mock.patch.object(resumeturn, "wake_alert",
                               return_value=True) as woke:
            rc = resumeturn.cmd_resume_turn(
                ["--alert", "--alert-fd", str(r)])
        self.assertEqual(rc, 0)
        self.assertIn("[cut: ", woke.call_args[0][0]["reason"])


class TheStatusListingCountsSeatsNotKeysTest(ResumeTurnBase):
    """task/2881: `--status` printed a LIVE seat twice, under two modes.

    The resume state is keyed by LEG, not by seat, and deliberately so: the
    compaction leg, each DEAF-IN-EFFECT repair episode, the wake-DM debounce
    latch and a nameless delivery each need their own act ledger. `--status`
    printed one line per KEY, so `deaf:helm-claude-2:1953843d` stood beside
    `helm-claude-2` carrying the SAME LIVE SESSION and a contradictory mode,
    and a reader counting rows counted seats that do not exist. Measured on
    the live host: 66 keys over 32 seats.

    THE PROPERTY THESE ARMS PIN is the one a reader uses: the rows at indent
    2 that are not parenthesised are SEATS, one apiece, and every
    session-scoped leg sits indented beneath the seat it belongs to."""

    OTHER = "6e33a7c0-0000-4000-8000-000000000001"

    def meta(self, lines, text):
        """A parenthesised meta row, matched as a WHOLE line: a substring
        match over the joined text would pass on a row that merely contains
        the count."""
        self.assertIn("  " + text, [ln.rstrip() for ln in lines], lines)

    def seat_rows(self, lines=None):
        """The rows a counting reader would count as seats: indent 2, and not
        one of the parenthesised meta lines."""
        lines = resumeturn.report_lines() if lines is None else lines
        return [ln[2:].split()[0] for ln in lines
                if ln.startswith("  ") and ln[2:3] not in (" ", "(", "")]

    def leg_rows(self, lines):
        return [ln.strip() for ln in lines if ln.startswith("      ")]

    def test_a_deaf_leg_is_not_a_second_seat(self):
        """THE MEASURED DEFECT. One seat, two legs, ONE row a reader counts."""
        resumeturn._record("codex", SID, "native-auto", "compacted")
        resumeturn._record("deaf:codex:%s" % SID, SID, "resumed", "nudged")
        lines = resumeturn.report_lines()
        self.assertEqual(self.seat_rows(lines), ["codex"], lines)
        self.meta(lines, "(1 seat(s) across 2 record(s))")
        legs = self.leg_rows(lines)
        self.assertEqual(len(legs), 1, lines)
        self.assertTrue(legs[0].startswith("deaf-in-effect session %s" % SID),
                        legs)

    def test_a_deaf_leg_of_an_OLDER_session_is_still_that_seat(self):
        """THE SESSION IS NOT THE JOIN, THE KEY IS. Measured live:
        `deaf:gemini:01d151a6` stood beside a `gemini` row whose session was
        `89cee695`. A cure that folded a deaf leg in only when its session
        matched the seat's CURRENT one would have left that twin standing."""
        resumeturn._record("codex", self.OTHER, "native-auto", "compacted")
        resumeturn._record("deaf:codex:%s" % SID, SID, "resumed", "nudged")
        lines = resumeturn.report_lines()
        self.assertEqual(self.seat_rows(lines), ["codex"], lines)
        self.assertEqual(len(self.leg_rows(lines)), 1, lines)

    def test_a_wake_latch_is_not_a_second_seat(self):
        """The fourth namespace, 17 keys on the live host. An empty episode
        is a real absence and is named as one, never as `?`."""
        resumeturn._record("codex", SID, "native-auto", "compacted")
        resumeturn._record("wake:codex", "", "wake", "woke the seat")
        lines = resumeturn.report_lines()
        self.assertEqual(self.seat_rows(lines), ["codex"], lines)
        self.assertEqual(self.leg_rows(lines)[0].split(" wake ")[0],
                         "wake latch (no episode)", lines)

    def test_a_nameless_delivery_joins_the_seat_owning_its_session(self):
        """`session:<sid8>` is minted when the deliverer never learned the
        seat's name; its record still carries the session, and 3 of the 14
        live ones twinned a named seat through it."""
        resumeturn._record("codex", SID, "native-auto", "compacted")
        resumeturn._record("session:%s" % SID[:8], SID, "withdrawn", "no name")
        lines = resumeturn.report_lines()
        self.assertEqual(self.seat_rows(lines), ["codex"], lines)
        self.assertNotIn("naming no seat", "\n".join(lines))

    def test_an_unclaimed_nameless_record_is_named_and_never_counted(self):
        """The control for the arm above: with no seat claiming its session
        the record is NOT folded into one and NOT counted as one — it is
        listed under its own heading, visible and uncounted."""
        resumeturn._record("codex", SID, "native-auto", "compacted")
        resumeturn._record("session:%s" % self.OTHER[:8], self.OTHER,
                           "withdrawn", "no name")
        lines = resumeturn.report_lines()
        self.assertEqual(self.seat_rows(lines), ["codex"], lines)
        self.meta(lines, "(1 seat(s) across 2 record(s))")
        self.meta(lines, "(1 record(s) naming no seat)")
        self.assertIn("session:%s" % self.OTHER[:8],
                      "\n".join(self.leg_rows(lines)))

    def test_an_unknown_namespace_is_never_folded_into_a_seat(self):
        """A FIFTH MINTER MUST FAIL CHEAPLY. An unlisted prefix surfaces
        unattributed rather than silently twinning a seat, which is the
        failure this row was filed for."""
        resumeturn._record("codex", SID, "native-auto", "compacted")
        resumeturn._record("newleg:codex:%s" % SID, SID, "resumed", "?")
        lines = resumeturn.report_lines()
        self.assertEqual(sorted(self.seat_rows(lines)), ["codex"], lines)
        self.meta(lines, "(1 record(s) naming no seat)")

    def test_a_seat_known_only_by_a_deaf_leg_still_gets_exactly_one_row(self):
        """A seat that never compacted still owns its repair episode, and the
        listing must not lose it to the grouping."""
        resumeturn._record("deaf:codex:%s" % SID, SID, "resumed", "nudged")
        lines = resumeturn.report_lines()
        self.assertEqual(self.seat_rows(lines), ["codex"], lines)
        self.meta(lines, "(1 seat(s) across 1 record(s))")
        self.assertIn("no compaction record", "\n".join(lines))

    def test_two_seats_each_twinned_still_count_two(self):
        """The count is not merely 1: grouping that collapsed everything
        would pass every arm above."""
        resumeturn._record("codex", SID, "native-auto", "a")
        resumeturn._record("deaf:codex:%s" % SID, SID, "resumed", "b")
        resumeturn._record("alpha", self.OTHER, "native-auto", "c")
        resumeturn._record("wake:alpha", self.OTHER, "wake", "d")
        lines = resumeturn.report_lines()
        self.assertEqual(self.seat_rows(lines), ["alpha", "codex"], lines)
        self.meta(lines, "(2 seat(s) across 4 record(s))")


if __name__ == "__main__":
    unittest.main()
