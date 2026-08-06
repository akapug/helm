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
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from helm import (handoff, harness, inject, orcaadopt, pk, resumeturn,
                  seat, seats)

SID = "33333333-3333-3333-3333-333333333333"
_ENV = ("HELM_SUBMIT_SETTLE_S",
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
        "HELM_HANDOFF_EVENT_FRESH_H")


# A pane whose composer is EMPTY — one that took its turn. Synthetic, but a
# real frame's shape: `submit` proves delivery by reading the composer back, so
# a double returning "" models an UNREADABLE pane (UNKNOWN), not a working one.
ADVANCED_PANE = "\n".join(("─" * 40, "❯", "─" * 40,
                           "  opus-5 | ~/dev/example/repo",
                           "  ⏵⏵ bypass permissions on"))


class FakeAdapter(harness._CLIAdapter):
    """Inherits the REAL `submit` (split send + read-back) from the base, so a
    second implementation cannot agree with a broken one."""
    name = "fake"

    def __init__(self, panes=({"handle": "h1", "title": "codex",
                               "status": "connected"},)):
        self.panes, self.sent = list(panes), []

    def list(self):
        return self.panes

    def read(self, handle, limit=3000, timeout=60):
        return ADVANCED_PANE

    def send(self, handle, text, enter=True):
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
        self.cwd = os.path.join(self.tmp, "repo")
        os.makedirs(self.cwd)
        self.d = seat._instance_dir("codex", "codex")
        os.makedirs(self.d, exist_ok=True)
        self.spawn()
        self.posts = []
        self.spawned = []

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
                                  side_effect=self.spawned.append), \
                mock.patch.object(seats, "beacon_procs", return_value=([], None)), \
                mock.patch("helm.chat.post",
                           side_effect=lambda text, **kw: self.posts.append(text)):
            return resumeturn.hook(payload or self.payload())

    def child_text(self):
        """The text the spawned child was handed — read from the file the hook
        wrote, exactly as the real child reads it."""
        argv = self.spawned[-1]
        with open(argv[argv.index("--text-file") + 1], encoding="utf-8") as f:
            return f.read()


class SourceGateTest(ResumeTurnBase):
    def test_only_a_compaction_arms_the_resume(self):
        """SessionStart fires on startup/resume/clear/compact. Every other
        source already HAS a live turn loop; arming there would inject a
        directive into a session that is about to be given one by a human."""
        for src in ("startup", "resume", "clear", ""):
            with self.subTest(source=src):
                self.spawned = []
                res = self.run_hook(self.payload(source=src))
                self.assertEqual(res["action"], "skip")
                self.assertEqual(self.spawned, [])
        res = self.run_hook(self.payload(source="compact"))
        self.assertEqual(res["action"], "spawned")
        self.assertEqual(len(self.spawned), 1)

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
        forgets, while only a compaction ever arms the resume. (@codex and
        @codex-2 found the /clear leg independently; deny-list default per
        @opus-integrator, board #181; empty tuple by unanimous meld
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
        """@codex-2's finding. forget_session swallowed every OSError from
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
                         {"turn": 0, "fired": {}, "pinned": None},
                         "a truncated file must read as a FRESH session")

    def test_a_boundary_that_cannot_clear_alerts_instead_of_shrugging(self):
        """The seat cannot detect this itself, so the alert IS the remedy."""
        from helm.inject import _ledger
        with mock.patch.object(_ledger, "forget_session", return_value=False):
            self.run_hook(self.payload(source="clear"))
        self.assertTrue(any("SUPPRESSION SURVIVED" in p for p in self.posts),
                        "a survived boundary must be LOUD: %r" % (self.posts,))
        # CONTROL: the same path stays quiet when the clear succeeds, so the
        # assertion above is the failure arm and not an alert that always fires.
        self.posts = []
        with mock.patch.object(_ledger, "forget_session", return_value=True):
            self.run_hook(self.payload(source="clear"))
        self.assertFalse(any("SUPPRESSION SURVIVED" in p for p in self.posts))

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

    def test_a_session_that_no_longer_owns_the_pane_is_never_injected(self):
        """The settle wait is a window in which the pane can be replaced, so
        identity is re-proven at SEND time, not at hook time."""
        ad = FakeAdapter()
        self.spawn(session="99999999-9999-9999-9999-999999999999")
        with mock.patch("helm.chat.post",
                        side_effect=lambda text, **kw: self.posts.append(text)):
            mode, detail = resumeturn.child("codex", SID, "GO NOW", 0, adapter=ad)
        self.assertNotEqual(mode, "resumed")
        self.assertEqual(ad.sent, [], "a stale session must not reach the pane")
        self.assertTrue(any("RESUME-TURN" in p for p in self.posts), self.posts)


class PaneIdentityTest(ResumeTurnBase):
    def test_an_unidentifiable_pane_alerts_loudly_and_never_guesses(self):
        cases = {
            "no register": lambda: os.remove(os.path.join(self.d, "spawn.json")),
            "headless": lambda: self.spawn(harness="headless"),
            "unbound session": lambda: self.spawn(session=None),
            "other session": lambda: self.spawn(session="other-session-id"),
        }
        for name, break_it in cases.items():
            with self.subTest(case=name):
                self.posts, self.spawned = [], []
                break_it()
                res = self.run_hook()
                self.assertEqual(res["action"], "alert")
                self.assertEqual(self.spawned, [],
                                 "an unidentifiable pane must not be guessed at")
                self.assertEqual(len(self.posts), 1, self.posts)
                self.assertIn("RESUME-TURN", self.posts[0])
                self.assertIn("codex", self.posts[0])
                self.assertIn("No live inbox beacon was observed", self.posts[0])
                self.assertIn("pane input is the fallback", self.posts[0])

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
        self.assertEqual(self.spawned, [])
        self.assertIn("HELM_CHAT_NAME", self.posts[0])
        self.assertIn("no live claude argv", self.posts[0])


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
        self.assertEqual(self.spawned, [])
        self.assertIn("never guess", self.posts[0])

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
        self.assertEqual(self.spawned, [])
        self.assertIn("could not be read", self.posts[0])
        self.assertNotIn("no address", self.posts[0])

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
        self.assertIn("acme-platform-claude", self.posts[0])


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
        self.assertIn("re-ground", text)

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
        self.assertIn("UNKNOWN", text)
        self.assertIn("could not be READ", text)
        self.assertIn("REPORT the unreadable shelf", text)

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
        self.assertIn("cancelled or superseded rows are DEAD", text)
        self.assertIn("check the row's status", text)
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
        self.assertEqual(len(self.spawned), 1)
        self._age_state(300)                       # 5 minutes later
        res = self.run_hook()
        self.assertEqual(res["action"], "spiral")
        self.assertEqual(len(self.spawned), 1, "the spiral must not inject")
        self.assertTrue(any("RESUME-TURN" in p for p in self.posts), self.posts)

    def test_the_rolling_cap_stops_a_slow_spiral_and_re_arms_with_time(self):
        os.environ["HELM_RESUME_TURN_DEBOUNCE_S"] = "1"
        os.environ["HELM_RESUME_TURN_SPIRAL_S"] = "10"
        for _ in range(resumeturn.MAX_RESUMES):
            self.run_hook()
            self._age_state(60)
        self.assertEqual(len(self.spawned), resumeturn.MAX_RESUMES)
        res = self.run_hook()
        self.assertEqual(res["action"], "capped")
        self.assertEqual(len(self.spawned), resumeturn.MAX_RESUMES)
        self._age_state(resumeturn.WINDOW_S + 60)   # the window rolls past
        self.assertEqual(self.run_hook()["action"], "spawned")
        self.assertEqual(len(self.spawned), resumeturn.MAX_RESUMES + 1)

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

    def test_a_nameless_deliver_rides_pids_and_reaches_the_sid_path(self):
        tp = os.path.join(self.tmp, "resume.txt")
        pk.atomic_write(tp, "CONTINUE THE WORK")
        with mock.patch.object(orcaadopt, "send_to_sid_pane",
                               return_value=("resumed", "ok")) as sidp:
            rc = resumeturn.cmd_resume_turn(
                ["--deliver", "--session", SID, "--text-file", tp,
                 "--delay", "0", "--pids", "14632"])
        self.assertEqual(rc, 0)
        sidp.assert_called_once_with(SID, "CONTINUE THE WORK",
                                     expect_pids=[14632], adapter=None)
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

class HookWiringTest(ResumeTurnBase):
    def test_the_spec_is_installed_on_homes_AND_seats(self):
        from helm import hooks
        spec = next(s for s in hooks.SPECS if s["name"] == "resume-turn")
        self.assertEqual(spec["event"], "SessionStart")
        self.assertEqual(spec["args"], "seat resume-turn --hook-json")
        self.assertIn(spec, hooks.DELIVERY_SPECS)
        self.assertTrue(hooks.spec_command(spec).endswith(
            "seat resume-turn --hook-json || true"))

    def test_the_verb_is_reachable_through_seat(self):
        with mock.patch.object(resumeturn, "cmd_resume_turn",
                               return_value=0) as m:
            self.assertEqual(seat.cmd_seat(["resume-turn", "--status"]), 0)
        m.assert_called_once_with(["--status"])


if __name__ == "__main__":
    unittest.main()
