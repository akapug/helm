"""Hermetic tests for helm.autocompact — the proxy-seat /compact watchdog.
HELM_HOME points at a tmp dir; planted transcripts/proxy.logs drive the read;
a fake adapter records injections. No chat posts (post=False), no real panes."""
import contextlib
import fcntl
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from helm import autocompact, harness, pk, seat, seats

# DERIVED, never typed. Every plant below means "N% of the codex window", and
# this file used to say so by retyping the window itself forty-odd times — the
# one number most likely to move was the one hardcoded most. It moved on
# 2026-07-30, when the output reserve was finally taken out of it, and took
# twelve tests red with it, not one of which was about the window's value.
# Read from the real table, the percentages stay true through the next move.
CODEX_WINDOW = seat.FAMILIES["codex"]["max_context"]

SID = "11111111-1111-1111-1111-111111111111"
_ENV = ("HELM_AUTOCOMPACT_THRESHOLD", "HELM_AUTOCOMPACT_ASSUME_WINDOW",
        "HELM_AUTOCOMPACT_FRESH_S", "HELM_AUTOCOMPACT_LATCH_TTL",
        "HELM_AUTOCOMPACT_CLAUDE")


class FakeAdapter(harness._CLIAdapter):
    """Inherits the REAL `submit` (split send + composer read-back) from the
    adapter base rather than carrying a second implementation of it."""
    name = "fake"

    def __init__(self, panes=({"handle": "h1", "title": "dynamic summary",
                               "status": "connected"},), tail="❯ ",
                 post_send_tails=None, consume_compact=True):
        self.panes, self.sent, self.tail = list(panes), [], tail
        self.spawned, self.stopped = [], []
        self.post_send_tails = (None if post_send_tails is None else
                                list(post_send_tails))
        self.consume_compact = consume_compact
        self.compact_sent = False
        self.verification_tail = None

    def list(self):
        return self.panes

    def read(self, handle, limit=3000, timeout=60):
        if self.compact_sent and self.post_send_tails:
            value = self.post_send_tails.pop(0)
            if isinstance(value, Exception):
                raise value
            self.tail = value
        elif self.verification_tail is not None:
            value, self.verification_tail = self.verification_tail, None
            return value
        return self.tail

    def send(self, handle, text, enter=True):
        self.sent.append((handle, text, enter))
        compact = text in ("", "/compact")
        if not enter or not compact:
            return
        self.compact_sent = True
        if self.post_send_tails is None and self.consume_compact:
            self.verification_tail = (
                "Compacting context\n  ⏵⏵ bypass permissions on · "
                "esc to interrupt")

    def spawn(self, command, title=None, cwd=None):
        self.spawned.append((command, title, cwd))
        self.panes.append({"handle": "h2", "title": title,
                           "status": "connected"})
        return "h2"

    def stop(self, handle):
        self.stopped.append(handle)
        self.panes = [row for row in self.panes if row.get("handle") != handle]


class FakeOrcaAdapter(FakeAdapter):
    name = "orca"

    def resolve_pane(self, pane_key):
        return {"handle": "new", "pty_id": "pty-1"}


def usage_line(ctx, model="gpt-5.6-sol", sidechain=False):
    d = {"type": "assistant",
         "message": {"role": "assistant", "model": model,
                     "usage": {"input_tokens": ctx - 7000,
                               "cache_read_input_tokens": 5000,
                               "cache_creation_input_tokens": 2000,
                               "output_tokens": 42}}}
    if sidechain:
        d["isSidechain"] = True
    return json.dumps(d)


class AutocompactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ac-")
        self._env = {k: os.environ.pop(k, None) for k in
                     _ENV + ("HELM_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
                             "HELM_CHAT_EVENT_DIR", "MELD_CHAT_EVENT_DIR",
                             "HELM_SPAWN_SEND_DELAY", "HELM_SUBMIT_SETTLE_S")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_EVENT_DIR"] = os.path.join(
            self.tmp, "chat-events")
        # seat._resume now sends the wake-path re-arm prompt after spawn's
        # boot grace; a real 5s sleep per exact-resume test is suite poison
        os.environ["HELM_SPAWN_SEND_DELAY"] = "0"
        # `submit` settles between typing and its bare Enter — same reasoning
        os.environ["HELM_SUBMIT_SETTLE_S"] = "0"
        self.verify_delay = mock.patch.object(
            autocompact, "SUBMIT_VERIFY_INTERVAL_S", 0)
        self.verify_delay.start()
        self.d = seat.seat_dir("codex")
        self.proj = os.path.join(self.d, "claude", "projects", "-tmp-proj")
        os.makedirs(self.proj, exist_ok=True)
        self.spawn("h1")

    def tearDown(self):
        self.verify_delay.stop()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, handle, harness="fake", seat_name="codex"):
        d = seat._instance_dir("codex", seat_name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump({"v": 1, "seat": seat_name, "harness": harness,
                       "handle": handle, "session": SID,
                       "worktree": self.tmp,
                       "launch_sh": os.path.join(d, "launch.sh")}, f)

    def unbind_session(self):
        path = os.path.join(self.d, "spawn.json")
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        rec["session"] = None
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f)

    def plant(self, ctx, model="gpt-5.6-sol", sid=SID, age_s=0):
        p = os.path.join(self.proj, sid + ".jsonl")
        with open(p, "w") as f:
            f.write('{"type":"user","message":{"role":"user"}}\n')
            # a fat sidechain record must never be mistaken for the main chain
            f.write(usage_line(999999, sidechain=True) + "\n")
            f.write(usage_line(ctx, model=model) + "\n")
        if age_s:
            t = time.time() - age_s
            os.utime(p, (t, t))
        return p

    # -- the read ----------------------------------------------------------

    def test_read_pct_from_transcript(self):
        half = int(CODEX_WINDOW * 0.50)          # half of codex's window
        self.plant(half)
        row = autocompact.read("codex")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["source"], "transcript")
        self.assertEqual(row["ctx_tokens"], half)
        self.assertEqual(row["window"], CODEX_WINDOW)
        self.assertAlmostEqual(row["pct"], 50.0)
        self.assertEqual(row["headroom_tokens"], CODEX_WINDOW - half)
        self.assertEqual(row["session"], SID)

    def test_read_headroom_tokens_clamped_at_zero_on_overflow(self):
        self.plant(400000)                       # > 360k window
        row = autocompact.read("codex")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["ctx_tokens"], 400000)
        self.assertEqual(row["headroom_tokens"], 0)

    def test_a_ZERO_usage_record_is_skipped_not_reported_as_zero_context(self):
        """The newest assistant record can legitimately carry ALL-ZERO usage —
        an aborted turn, a tool-only turn, the first record after a compact —
        and returning that 0 as the seat's context is the one value that
        guarantees the watchdog never fires.

        Measured live 2026-07-26: codex read 0.0% of 360k against a 33-minute-old
        transcript of 2363 lines and 755 usage-bearing records, THIRTY-THREE
        MINUTES AFTER being rescued from 102%. grok read 0.0% while sitting at
        115%. Both were wedged and invisible to the surface built to catch them;
        with the zero skipped they read 102.7% and 115.2% and both fired.
        """
        import os
        pth = os.path.join(self.proj, SID + ".jsonl")
        with open(pth, "w") as f:
            f.write('{"type":"user","message":{"role":"user"}}\n')
            f.write(usage_line(200000) + "\n")          # the real context
            f.write(usage_line(0) + "\n")               # an empty turn on top
        row = autocompact.read("codex")
        self.assertEqual(row["ctx_tokens"], 200000,
                         "a zero-usage record must not mask the real context")
        self.assertAlmostEqual(row["pct"], 100.0 * 200000 / CODEX_WINDOW,
                               places=1)

    def test_all_zero_usage_is_UNKNOWN_never_a_confident_zero(self):
        """When EVERY usage record is zero the context is unknown, and unknown
        must not render as 0% — that is a confident lie which silences the
        alarm, the same shape as an empty pane read classified as a live pane."""
        import os
        pth = os.path.join(self.proj, SID + ".jsonl")
        with open(pth, "w") as f:
            f.write('{"type":"user","message":{"role":"user"}}\n')
            f.write(usage_line(0) + "\n")
        row = autocompact.read("codex")
        self.assertNotEqual(row.get("status"), "ok",
                            "all-zero usage is not a successful reading")
        self.assertNotEqual(row.get("ctx_tokens"), 0,
                            "unknown must never be reported as zero context")

    def test_no_transcript_no_data(self):
        self.assertEqual(autocompact.read("codex")["status"], "no-context-data")

    def test_proxy_log_fallback(self):
        with open(os.path.join(seat.seat_dir("codex"), "proxy.log"), "w") as f:
            f.write("plain gin access line, no tokens\n")
            f.write('{"session_id":"%s","usage":{"input_tokens":300000,'
                    '"cache_read_input_tokens":30000,'
                    '"cache_creation_input_tokens":0}}\n' % SID)
        row = autocompact.read("codex")
        self.assertEqual(row["source"], "proxy.log")
        self.assertEqual(row["ctx_tokens"], 330000)
        self.assertEqual(row["status"], "ok")

    def test_instance_never_reads_a_sibling_proxy_log(self):
        inst = seat._instance_dir("codex", "codex-2")
        os.makedirs(inst, exist_ok=True)
        with open(os.path.join(seat.seat_dir("codex"), "proxy.log"), "w") as f:
            f.write('{"usage":{"input_tokens":350000}}\n')
        self.assertEqual(autocompact.read("codex-2")["status"],
                         "no-context-data")
        with open(os.path.join(inst, "proxy.log"), "w") as f:
            f.write('{"usage":{"input_tokens":180000}}\n')
        row = autocompact.read("codex-2")
        self.assertEqual(row["source"], "proxy.log")
        self.assertEqual(row["ctx_tokens"], 180000)
        self.assertEqual(row["status"], "proxy-log-unattributed")
        self.spawn("h2", seat_name="codex-2")
        self.assertEqual(autocompact.read("codex-2")["status"],
                         "proxy-log-unattributed")
        with open(os.path.join(inst, "proxy.log"), "a") as f:
            f.write('{"session_id":"%s","usage":{"input_tokens":190000}}\n'
                    % SID)
        self.assertEqual(autocompact.read("codex-2")["status"], "ok")

    def test_window_unset_is_noop_when_assume_off(self):
        # a synthetic family with NO max_context exercises the unset path
        os.environ["HELM_AUTOCOMPACT_ASSUME_WINDOW"] = "0"
        with mock.patch.dict(seat.FAMILIES,
                             {"nowin": {"port": 8399, "model": "nowin-m",
                                        "mode": "proxy-key"}}):
            os.makedirs(seat.seat_dir("nowin"), exist_ok=True)
            row = autocompact.read("nowin")
            self.assertEqual(row["status"], "window-unset")
            res = autocompact.check(seats=["nowin"], post=False,
                                    adapter=FakeAdapter())
            self.assertEqual(res["fired"], [])

    def test_window_unset_mirrors_cc_default(self):
        # a family with no FAMILIES max_context -> CC's assumed 200k is the gauge
        with mock.patch.dict(seat.FAMILIES,
                             {"nowin": {"port": 8399, "model": "nowin-m",
                                        "mode": "proxy-key"}}):
            proj = os.path.join(seat.seat_dir("nowin"), "claude", "projects", "-p")
            os.makedirs(proj, exist_ok=True)
            with open(os.path.join(proj, SID + ".jsonl"), "w") as f:
                f.write(usage_line(100000, model="nowin-m") + "\n")
            row = autocompact.read("nowin")
            self.assertEqual(row["window"], 200000)
            self.assertEqual(row["window_src"], "cc-assumed-default")
            self.assertAlmostEqual(row["pct"], 50.0)

    def test_window_pinned_from_family_max_context(self):
        # kimi now declares max_context (k3 = 1M) -> the pinned window is the
        # gauge, NOT CC's 200k default. 500k of a 1M window reads 50%.
        proj = os.path.join(seat.seat_dir("kimi"), "claude", "projects", "-p")
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, SID + ".jsonl"), "w") as f:
            f.write(usage_line(500000, model="kimi-k3") + "\n")
        row = autocompact.read("kimi")
        self.assertEqual(row["window"], 1000000)
        self.assertEqual(row["window_src"], "FAMILIES.max_context")
        self.assertAlmostEqual(row["pct"], 50.0)

    # -- the trigger -------------------------------------------------------

    def test_no_fire_at_50(self):
        self.plant(int(CODEX_WINDOW * 0.50))
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [])

    def test_repeated_400_prunes_and_resumes_instead_of_clearing(self):
        # Recovery is pane-tail driven: this seat sits at half its window, well
        # below the compact trigger, and still recovers off the terminal 400s.
        self.plant(int(CODEX_WINDOW * 0.50))
        tail = ("API Error: 400 prompt is too long: context exceeds maximum\n"
                "API Error: 400 prompt is too long: context exceeds maximum")
        ad = FakeAdapter(tail=tail)
        new = "22222222-2222-2222-2222-222222222222"
        with mock.patch.object(autocompact, "_prune_context",
                               return_value=(new, "pruned 221k -> 167k", False)) as prune, \
                mock.patch.object(seat, "_resume", return_value=0) as resume:
            first = autocompact.check(seats=["codex"], post=False, adapter=ad)
            again = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(first["fired"][0]["mode"], "pruned-resumed")
        self.assertEqual(ad.sent, [], "/clear must not discard a resumable session")
        prune.assert_called_once()
        resume.assert_called_once_with(
            "codex", [], _locked=True, target_sid=new,
            expected_session=SID, adapter=ad)
        self.assertEqual(again["fired"], [])
        self.assertTrue(again["rows"][0].get("latched"))

    def test_prune_failure_is_the_only_path_that_falls_back_to_clear(self):
        self.plant(180000)
        tail = ("API Error: 400 prompt is too long: context exceeds maximum\n"
                "API Error: 400 prompt is too long: context exceeds maximum")
        ad = FakeAdapter(tail=tail)
        with mock.patch.object(autocompact, "_prune_context",
                               return_value=(None, "cv prune failed: fixture", True)):
            got = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(got["fired"][0]["mode"], "clear-pending")
        self.assertIn("cv prune failed", got["fired"][0]["detail"])
        self.assertEqual(ad.sent, [("h1", "/clear", True)])

    def test_resume_failure_after_a_good_prune_never_clears_or_reprunes(self):
        self.plant(180000)
        tail = ("API Error: 400 prompt is too long: context exceeds maximum\n"
                "API Error: 400 prompt is too long: context exceeds maximum")
        ad = FakeAdapter(tail=tail)
        new = "22222222-2222-2222-2222-222222222222"

        def prune_copy(_row):
            with open(os.path.join(self.proj, new + ".jsonl"), "w") as f:
                f.write(usage_line(167000) + "\n")
            return new, "pruned", False

        with mock.patch.object(autocompact, "_prune_context",
                               side_effect=prune_copy) as prune, \
                mock.patch.object(seat, "_resume", return_value=1):
            first = autocompact.check(seats=["codex"], post=False, adapter=ad)
            second = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(first["fired"][0]["mode"], "resume-manual")
        self.assertEqual(second["fired"], [])
        self.assertTrue(second["rows"][0].get("latched"))
        self.assertEqual(prune.call_count, 1)
        self.assertEqual(ad.sent, [], "a preserved pruned copy must never fall through to /clear")

    def test_prune_context_pins_home_window_thinking_and_default_revive(self):
        self.plant(360000)
        row = autocompact.read("codex")
        new = "22222222-2222-2222-2222-222222222222"
        new_path = os.path.join(self.proj, new + ".jsonl")

        def run(cmd, **kwargs):
            self.assertEqual(cmd[:3], ["cv", "prune", SID])
            self.assertIn("--json", cmd)
            self.assertIn("--thinking", cmd)
            self.assertEqual(cmd[cmd.index("--to") + 1], new)
            self.assertEqual(cmd[cmd.index("--window") + 1], "180000")
            self.assertNotIn("--no-revive", cmd,
                             "revive is cv prune's default and must stay enabled")
            self.assertEqual(kwargs["env"]["CLAUDE_CONFIG_DIR"],
                             os.path.join(self.d, "claude"))
            with open(new_path, "w") as f:
                f.write(usage_line(167000) + "\n")
            return mock.Mock(returncode=0, stdout=json.dumps({
                "sourceId": SID, "newId": new, "newPath": new_path,
                "beforeBytes": 21100000, "afterBytes": 1600000,
                "windowRealTokens": 167000, "tokensFreed": 54000,
                "revived": {"recordedTokensBefore": 221000,
                            "recordedTokensAfter": 167000,
                            "usageRecordsRewritten": 208},
            }), stderr="")

        with mock.patch("uuid.uuid4", return_value=new), \
                mock.patch("subprocess.run", side_effect=run):
            got, detail, clear_allowed = autocompact._prune_context(row)
        self.assertEqual(got, new)
        self.assertFalse(clear_allowed)
        self.assertIn("167,000", detail)
        self.assertTrue(os.path.exists(os.path.join(self.proj, SID + ".jsonl")),
                        "cv prune is copy-only; the source session survives")

    def test_malformed_prune_report_with_pinned_copy_never_clears(self):
        self.plant(360000)
        new = "22222222-2222-2222-2222-222222222222"
        new_path = os.path.join(self.proj, new + ".jsonl")
        tail = ("API Error: 400 prompt is too long: context exceeds maximum\n"
                "API Error: 400 prompt is too long: context exceeds maximum")
        ad = FakeAdapter(tail=tail)

        def run(_cmd, **_kwargs):
            with open(new_path, "w") as f:
                f.write(usage_line(167000) + "\n")
            return mock.Mock(returncode=0, stdout="not-json", stderr="")

        with mock.patch("uuid.uuid4", return_value=new), \
                mock.patch("subprocess.run", side_effect=run):
            got = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(got["fired"][0]["mode"], "recovery-manual")
        self.assertEqual(ad.sent, [])
        self.assertTrue(os.path.exists(new_path))
        self.assertTrue(os.path.exists(os.path.join(self.proj, SID + ".jsonl")))

    def test_overbudget_revived_usage_is_manual_never_resumed_or_cleared(self):
        self.plant(360000)
        row = autocompact.read("codex")
        new = "22222222-2222-2222-2222-222222222222"
        new_path = os.path.join(self.proj, new + ".jsonl")

        def run(_cmd, **_kwargs):
            with open(new_path, "w") as f:
                f.write(usage_line(180001) + "\n")
            return mock.Mock(returncode=0, stdout=json.dumps({
                "sourceId": SID, "newId": new, "newPath": new_path}), stderr="")

        with mock.patch("uuid.uuid4", return_value=new), \
                mock.patch("subprocess.run", side_effect=run):
            sid, detail, clear_allowed = autocompact._prune_context(row)
        self.assertIsNone(sid)
        self.assertIn("above 180,000 budget", detail)
        self.assertFalse(clear_allowed)
        self.assertTrue(os.path.exists(new_path))
        self.assertTrue(os.path.exists(os.path.join(self.proj, SID + ".jsonl")))

    def test_ambiguous_exact_source_never_prunes_or_clears(self):
        self.plant(180000)
        other = os.path.join(self.d, "claude", "projects", "-other")
        os.makedirs(other)
        with open(os.path.join(other, SID + ".jsonl"), "w") as f:
            f.write(usage_line(180000) + "\n")
        tail = ("API Error: 400 prompt is too long: context exceeds maximum\n"
                "API Error: 400 prompt is too long: context exceeds maximum")
        ad = FakeAdapter(tail=tail)
        with mock.patch("subprocess.run") as run:
            got = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(got["fired"][0]["mode"], "recovery-manual")
        self.assertIn("not one exact seat transcript", got["fired"][0]["detail"])
        self.assertFalse(run.called)
        self.assertEqual(ad.sent, [])

    def test_exact_resume_targets_pruned_session_and_rebinds_claim_sessions(self):
        self.plant(221000)
        new = "22222222-2222-2222-2222-222222222222"
        self.plant(167000, sid=new)
        distractor = "33333333-3333-3333-3333-333333333333"
        self.plant(1000, sid=distractor)  # newer mtime must NOT outrank target_sid
        launch = os.path.join(self.d, "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec claude \"$@\"\n")
        os.chmod(launch, 0o700)
        resource = "worktree:helm:fixture-" + os.path.basename(self.tmp)
        ok, message, lease = seats.claim(resource, "codex", session=SID)
        self.assertTrue(ok, message)
        self.assertTrue(lease)
        with open(seats.claims_path()) as f:
            before_claim = dict(json.load(f)[resource])
        ad = FakeAdapter()
        with mock.patch.object(seat, "_reap_stale", return_value=([], [])), \
                mock.patch.object(seat, "_write_launch_assets"), \
                mock.patch.object(seat, "_ensure_autocompact_timer"), \
                mock.patch.object(seats, "write_roster"), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = seat._resume("codex", [], target_sid=new,
                              expected_session=SID, adapter=ad)
        self.assertEqual(rc, 0)
        self.assertIn("--resume " + new, ad.spawned[0][0])
        self.assertEqual(ad.spawned[0][2], self.tmp,
                         "exact recovery preserves spawn.json worktree")
        with open(os.path.join(self.d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], new)
        with open(seats.claims_path()) as f:
            claim = json.load(f)[resource]
        self.assertEqual(claim["session"], new)
        for key, value in before_claim.items():
            if key != "session":
                self.assertEqual(claim[key], value, key)

    def test_exact_resume_refuses_missing_worktree_before_reaping(self):
        self.plant(221000)
        new = "22222222-2222-2222-2222-222222222222"
        self.plant(167000, sid=new)
        launch = os.path.join(self.d, "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec claude \"$@\"\n")
        os.chmod(launch, 0o700)
        with open(os.path.join(self.d, "spawn.json")) as f:
            rec = json.load(f)
        rec["worktree"] = os.path.join(self.tmp, "deleted-worktree")
        with open(os.path.join(self.d, "spawn.json"), "w") as f:
            json.dump(rec, f)
        with mock.patch.object(seat, "_reap_stale") as reap, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = seat._resume("codex", [], target_sid=new,
                              expected_session=SID, adapter=FakeAdapter())
        self.assertEqual(rc, 1)
        self.assertFalse(reap.called)

    def test_exact_resume_refuses_unproven_spawn_before_claims_or_register(self):
        self.plant(221000)
        new = "22222222-2222-2222-2222-222222222222"
        self.plant(167000, sid=new)
        launch = os.path.join(self.d, "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec claude \"$@\"\n")
        os.chmod(launch, 0o700)

        class DeadSpawnAdapter(FakeAdapter):
            def spawn(self, command, title=None, cwd=None):
                self.spawned.append((command, title, cwd))
                return "h2"                 # no inventory row: never became live

        ad = DeadSpawnAdapter()
        with mock.patch.object(seat, "_reap_stale", return_value=([], [])), \
                mock.patch.object(seat, "_write_launch_assets"), \
                mock.patch.object(seats, "rebind_claim_sessions") as rebind, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = seat._resume("codex", [], target_sid=new,
                              expected_session=SID, adapter=ad)
        self.assertEqual(rc, 1)
        self.assertEqual(ad.stopped, ["h2"])
        self.assertFalse(rebind.called)
        with open(os.path.join(self.d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], SID)

    def test_claim_rebind_failure_stops_new_pane_without_publishing(self):
        self.plant(221000)
        new = "22222222-2222-2222-2222-222222222222"
        self.plant(167000, sid=new)
        launch = os.path.join(self.d, "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec claude \"$@\"\n")
        os.chmod(launch, 0o700)
        resource = "worktree:helm:rebind-failure-" + os.path.basename(self.tmp)
        ok, _, _ = seats.claim(resource, "codex", session=SID)
        self.assertTrue(ok)
        with open(seats.claims_path()) as f:
            before = json.load(f)[resource]
        ad = FakeAdapter()
        with mock.patch.object(seat, "_reap_stale", return_value=([], [])), \
                mock.patch.object(seat, "_write_launch_assets"), \
                mock.patch.object(seats, "rebind_claim_sessions",
                                  side_effect=OSError("disk full")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = seat._resume("codex", [], target_sid=new,
                              expected_session=SID, adapter=ad)
        self.assertEqual(rc, 1)
        self.assertEqual(ad.stopped, ["h2"])
        with open(os.path.join(self.d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], SID)
        with open(seats.claims_path()) as f:
            self.assertEqual(json.load(f)[resource], before)
        self.assertTrue(os.path.exists(os.path.join(self.proj, SID + ".jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.proj, new + ".jsonl")))

    def test_registration_failure_rolls_claims_back_and_stops_new_pane(self):
        self.plant(221000)
        new = "22222222-2222-2222-2222-222222222222"
        self.plant(167000, sid=new)
        launch = os.path.join(self.d, "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec claude \"$@\"\n")
        os.chmod(launch, 0o700)
        resource = "worktree:helm:register-failure-" + os.path.basename(self.tmp)
        ok, _, _ = seats.claim(resource, "codex", session=SID)
        self.assertTrue(ok)
        with open(seats.claims_path()) as f:
            before = json.load(f)[resource]
        ad = FakeAdapter()
        with mock.patch.object(seat, "_reap_stale", return_value=([], [])), \
                mock.patch.object(seat, "_write_launch_assets"), \
                mock.patch.object(seat, "_register_spawn", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = seat._resume("codex", [], target_sid=new,
                              expected_session=SID, adapter=ad)
        self.assertEqual(rc, 1)
        self.assertEqual(ad.stopped, ["h2"])
        with open(os.path.join(self.d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], SID)
        with open(seats.claims_path()) as f:
            self.assertEqual(json.load(f)[resource], before)
        self.assertTrue(os.path.exists(os.path.join(self.proj, SID + ".jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.proj, new + ".jsonl")))

    def test_one_400_or_discussion_text_never_clears(self):
        self.plant(int(CODEX_WINDOW * 0.50))
        for tail in (
                "API Error: 400 prompt is too long: context exceeds maximum",
                "discussion: API Error: 400 prompt is too long\n"
                "discussion: API Error: 400 prompt is too long",
                "API Error: 400 prompt is too long: context exceeds maximum\n"
                "API Error: 400 prompt is too long: context exceeds maximum\n"
                "assistant completed normally\n❯ ",
                "API Error: 400 prompt caching maximum breakpoints exceeded\n"
                "API Error: 400 prompt caching maximum breakpoints exceeded",
                "API Error: 400 prompt is too long: context exceeds maximum\n"
                "API Error: 400 prompt is too long: context exceeds maximum\n"
                "HUNG: pane is quiet"):
            with self.subTest(tail=tail):
                ad = FakeAdapter(tail=tail)
                res = autocompact.check(seats=["codex"], post=False, adapter=ad)
                self.assertEqual(res["fired"], [])
                self.assertEqual(ad.sent, [])

    @mock.patch.object(autocompact, "_prune_context",
                       return_value=(None, "fixture prune failure", True))
    def test_pending_clear_rebriefs_only_after_session_changes(self, _prune):
        self.plant(int(CODEX_WINDOW * 0.50))
        tail = ("API Error: 400 prompt is too long: context exceeds maximum\n"
                "API Error: 400 prompt is too long: context exceeds maximum")
        ad = FakeAdapter(tail=tail)
        first = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(first["fired"][0]["mode"], "clear-pending")
        self.assertEqual(ad.sent, [("h1", "/clear", True)])
        second = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(second["fired"], [])
        sid = "22222222-2222-2222-2222-222222222222"
        self.plant(1000, sid=sid)
        ad.tail = ""
        artifact_only = autocompact.check(
            seats=["codex"], post=False, adapter=ad)
        self.assertEqual(artifact_only["fired"], [])
        self.assertEqual(len(ad.sent), 1,
                         "newest transcript is not SessionStart proof")
        with open(os.path.join(self.d, "spawn.json")) as f:
            rec = json.load(f)
        rec["session"] = None
        with open(os.path.join(self.d, "spawn.json"), "w") as f:
            json.dump(rec, f)
        unbound = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(unbound["fired"], [],
                         "an unbound newer transcript is not SessionStart proof")
        rec["session"] = sid
        with open(os.path.join(self.d, "spawn.json"), "w") as f:
            json.dump(rec, f)
        third = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(third["fired"][0]["mode"], "cleared")
        self.assertEqual(len(ad.sent), 2)
        self.assertIn("helm chat wait --seat codex --follow", ad.sent[1][1])

    def test_fires_over_threshold_and_injects_compact(self):
        self.plant(int(CODEX_WINDOW * 0.91))
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual([r["seat"] for r in res["fired"]], ["codex"])
        self.assertEqual(res["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_live_pane_without_bound_session_still_injects_compact(self):  # noqa: VACUOUS_ASSERTION — fired mode, adapter send, and persisted latch are positive controls
        """The pane handle owns unbound actuation and duplicate suppression."""
        self.plant(int(CODEX_WINDOW * 0.91))
        self.unbind_session()
        ad = FakeAdapter(tail="❯ ")
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        row = res["rows"][0]
        self.assertEqual(row["status"], "session-unbound")
        self.assertEqual(res["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])
        with open(autocompact._state_path(), encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["codex"]["identity"], "h1")

    def test_session_mismatch_injects_once_through_the_registered_pane(self):  # noqa: VACUOUS_ASSERTION — injected mode and exact pane send are positive controls
        """A stale session latch cannot wall a current, identity-proven pane."""
        live = "22222222-2222-2222-2222-222222222222"
        self.plant(int(CODEX_WINDOW * 0.91), sid=live)
        ad = FakeAdapter(tail="❯ ")
        first = autocompact.check(seats=["codex"], post=False, adapter=ad)
        row = first["rows"][0]
        self.assertEqual(row["status"], "session-mismatch")
        self.assertEqual(first["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])
        with open(autocompact._state_path(), encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["codex"]["identity"], SID)
        second = autocompact.check(
            seats=["codex"], post=False, adapter=ad)
        self.assertEqual(second["fired"], [])
        self.assertTrue(second["rows"][0].get("latched"))
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_unbound_pane_never_makes_stale_context_actionable(self):  # noqa: VACUOUS_ASSERTION — STALE is the positive classification control
        self.plant(int(CODEX_WINDOW * 0.91), age_s=8 * 3600)
        self.unbind_session()
        ad = FakeAdapter(tail="❯ ")
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["rows"][0]["status"], "stale")
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [])

    def test_unbound_pane_refuses_historical_prompt_chrome(self):  # noqa: VACUOUS_ASSERTION — exact UNKNOWN refusal and absence of sends are positive controls
        self.plant(int(CODEX_WINDOW * 0.91))
        self.unbind_session()
        for tail in (
                "❯ previous command\nassistant output still streaming",
                "\x1b[36m❯\x1b[0m rendered quotation\n"
                "assistant output still streaming",
                "❯ previous command\nWhich approach should I use?\n"
                "  1. Safe\n  2. Fast\nEnter to select"):
            with self.subTest(tail=tail):
                path = autocompact._state_path()
                if os.path.exists(path):
                    os.unlink(path)
                ad = FakeAdapter(tail=tail)
                res = autocompact.check(
                    seats=["codex"], post=False, adapter=ad)
                row = res["rows"][0]
                self.assertEqual(res["fired"], [])
                self.assertEqual(ad.sent, [])
                self.assertEqual(row["actuation_state"], "UNKNOWN")
                self.assertIn("requires an IDLE or CONTEXT_FULL prompt",
                              row["actuation_reason"])

    def test_unbound_pane_preserves_unsent_composer_input(self):  # noqa: VACUOUS_ASSERTION — exact UNKNOWN refusal and absence of sends are positive controls
        self.plant(int(CODEX_WINDOW * 0.91))
        self.unbind_session()
        ad = FakeAdapter(tail="❯ keep this draft")
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        row = res["rows"][0]
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [])
        self.assertEqual(row["actuation_state"], "UNKNOWN")
        self.assertIn("composer contains unsent input",
                      row["actuation_reason"])

    def test_same_handle_new_session_invalidates_unbound_scan(self):  # noqa: VACUOUS_ASSERTION — exact UNKNOWN refusal and absence of sends are positive controls
        self.plant(int(CODEX_WINDOW * 0.91))
        self.unbind_session()
        real = seat._spawn_record
        calls = [0]

        def rebound(d):
            calls[0] += 1
            rec = real(d)
            if calls[0] > 1 and rec:
                rec = dict(rec)
                rec["session"] = "22222222-2222-2222-2222-222222222222"
            return rec

        ad = FakeAdapter(tail="❯ ")
        with mock.patch.object(seat, "_spawn_record", side_effect=rebound):
            res = autocompact.check(
                seats=["codex"], post=False, adapter=ad)
        row = res["rows"][0]
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [])
        self.assertEqual(row["actuation_state"], "UNKNOWN")
        self.assertIn("registered session changed after context scan",
                      row["actuation_reason"])

    def test_unbound_pane_replacement_ends_the_latch_episode(self):
        self.plant(int(CODEX_WINDOW * 0.91))
        self.unbind_session()
        first = autocompact.check(
            seats=["codex"], post=False, adapter=FakeAdapter(tail="❯ "))
        self.assertEqual(first["fired"][0]["mode"], "injected")
        self.spawn("h2")
        self.unbind_session()
        ad = FakeAdapter(
            panes=({"handle": "h2", "title": "dynamic summary",
                    "status": "connected"},), tail="❯ ")
        second = autocompact.check(
            seats=["codex"], post=False, adapter=ad)
        self.assertEqual(second["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h2", "/compact", True)])

    def test_latch_identity_uses_bound_session_or_unbound_pane(self):
        row = {"seat": "codex", "registered_session": SID,
               "session": SID, "pane_handle": "h1"}
        self.assertEqual(autocompact._latch_identity(row), SID)
        row["registered_session"] = None
        self.assertEqual(autocompact._latch_identity(row), "h1")
        row["pane_handle"] = None
        self.assertEqual(autocompact._latch_identity(row), SID)
        row["session"] = None
        self.assertEqual(autocompact._latch_identity(row), "codex")

    def test_unbound_live_pane_refuses_every_non_idle_turn_state(self):  # noqa: VACUOUS_ASSERTION — non-empty state table asserts exact positive refusal state and reason per row
        self.plant(int(CODEX_WINDOW * 0.91))
        self.unbind_session()
        cases = (
            ("RUNNING", "work\nesc to interrupt", "open turn"),
            ("BLOCKED_ON_HUMAN",
             "Do you want to proceed? /tmp/plans/blocked.md", "blocked.md"),
            ("UNKNOWN", "", "requires an IDLE or CONTEXT_FULL prompt"),
        )
        for state, tail, reason in cases:
            with self.subTest(state=state):
                ad = FakeAdapter(tail=tail)
                res = autocompact.check(
                    seats=["codex"], post=False, adapter=ad)
                row = res["rows"][0]
                self.assertEqual(res["fired"], [])
                self.assertEqual(ad.sent, [])
                self.assertEqual(row["actuation_state"], state)
                self.assertIn(reason, row["actuation_reason"])
                if state == "UNKNOWN":
                    self.assertEqual(res["hot"], [],
                                     "the mature refusal alarm owns severity")
                    self.assertIs(res["alarms"][0], row)
                else:
                    self.assertIs(res["hot"][0], row)

    def test_default_threshold_is_eighty_not_ninety(self):
        """The owner set the trigger to 80 (2026-07-29). The systemd unit runs
        `helm seat autocompact --once` with no --threshold, so DEFAULT_THRESHOLD
        IS the fleet's live firing point — a regression back to 90 would let a
        proxied seat sit in the 80-90 dead zone that native cannot catch. Pins
        both the constant and a real fire at 81% (above 80, below the old 90)."""
        self.assertEqual(autocompact.DEFAULT_THRESHOLD, 80)
        self.assertEqual(autocompact.threshold_pct(), 80)   # no env override set
        self.plant(int(CODEX_WINDOW * 0.81))   # would NOT fire at the old 90
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual([r["seat"] for r in res["fired"]], ["codex"])
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_exact_compact_submits_enter_then_latches_until_context_drops(self):
        self.plant(int(CODEX_WINDOW * 0.96))
        ad = FakeAdapter(tail="\x1b[36m❯ /compact\x1b[0m\n"
                              "  ⏵⏵ bypass permissions on")
        first = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(first["fired"][0]["mode"], "submitted")
        self.assertEqual(ad.sent, [("h1", "", True)])
        second = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(second["fired"], [])
        self.assertTrue(second["rows"][0].get("latched"))
        self.assertEqual(ad.sent, [("h1", "", True)])
        self.plant(int(CODEX_WINDOW * 0.60))
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.plant(int(CODEX_WINDOW * 0.94))
        ad.tail = "❯ "
        third = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(third["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "", True),
                                   ("h1", "/compact", True)])

    def test_running_outranks_apparent_exact_compact_history(self):  # noqa: VACUOUS_ASSERTION — positive RUNNING state proves the no-send is caused by the live-turn guard
        self.plant(int(CODEX_WINDOW * 0.96))
        tail = ("❯ /compact\n  ⏵⏵ bypass permissions on · "
                "esc to interrupt")
        self.assertEqual(seat._classify_pane_tail(tail)[0], "RUNNING")
        self.assertTrue(autocompact._command_pending(tail, "compact"),
                        "submitted-message chrome is intentionally ambiguous")
        ad = FakeAdapter(tail=tail)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["rows"][0]["actuation_state"], "RUNNING")
        self.assertEqual(ad.sent, [])

    def test_exact_compact_is_explicit_intent_at_context_full(self):
        self.plant(int(CODEX_WINDOW * 1.01))
        tail = ("100% context used\n❯ /compact\n"
                "  ⏵⏵ bypass permissions on")
        self.assertEqual(seat._classify_pane_tail(tail)[0], "CONTEXT_FULL")
        ad = FakeAdapter(tail=tail)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "submitted")
        self.assertEqual(ad.sent, [("h1", "", True)])

    def test_empty_context_full_composer_receives_normal_injection(self):
        self.plant(int(CODEX_WINDOW * 1.01))
        tail = "100% context used\n❯ \n  ⏵⏵ bypass permissions on"
        self.assertEqual(seat._classify_pane_tail(tail)[0], "CONTEXT_FULL")
        ad = FakeAdapter(tail=tail)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_compact_arguments_are_a_draft_not_submission_authority(self):  # noqa: VACUOUS_ASSERTION — each subtest positively reaches UNKNOWN draft refusal while asserting no Enter
        self.plant(int(CODEX_WINDOW * 0.96))
        for command in ("/compact now", "/compact --preserve tasks",
                        "/compact/other"):
            with self.subTest(command=command):
                ad = FakeAdapter(tail="❯ %s" % command)
                res = autocompact.check(
                    seats=["codex"], post=False, adapter=ad)
                self.assertEqual(res["fired"], [])
                self.assertEqual(ad.sent, [])
                self.assertEqual(res["rows"][0]["actuation_state"], "UNKNOWN")
                os.unlink(autocompact._state_path())

    def test_delayed_composer_drain_is_verified_not_failed_early(self):
        self.plant(int(CODEX_WINDOW * 0.96))
        pending = "❯ /compact\n  ⏵⏵ bypass permissions on"
        ad = FakeAdapter(tail=pending, post_send_tails=(
            pending, pending,
            "Compacting context\n  ⏵⏵ bypass permissions on · esc to interrupt"))
        res = autocompact.check(
            seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "submitted")
        self.assertEqual(ad.sent, [("h1", "", True)])

    def test_static_exact_compact_fails_loud_without_success_latch_and_retries(self):  # noqa: VACUOUS_ASSERTION — the second pass positively submits on the same adapter after the failed no-latch pass
        self.plant(int(CODEX_WINDOW * 0.96))
        ad = FakeAdapter(tail="❯ /compact", consume_compact=False)
        with mock.patch.object(autocompact, "SUBMIT_VERIFY_READS", 3):
            first = autocompact.check(
                seats=["codex"], post=False, adapter=ad)
        self.assertEqual(first["fired"], [])
        self.assertEqual(first["rows"][0]["actuation_state"],
                         "FAILED_TO_SUBMIT")
        self.assertIn("still occupies", first["rows"][0]["actuation_reason"])
        self.assertEqual(ad.sent, [("h1", "", True)])
        with open(autocompact._state_path(), encoding="utf-8") as f:
            state = json.load(f)
        self.assertNotIn("codex", state, "an attempted Enter consumed the success latch")

        ad.consume_compact = True
        second = autocompact.check(
            seats=["codex"], post=False, adapter=ad)
        self.assertEqual(second["fired"][0]["mode"], "submitted")
        self.assertEqual(ad.sent, [("h1", "", True), ("h1", "", True)])

    def test_adapter_acceptance_failure_is_not_submission_success(self):
        self.plant(int(CODEX_WINDOW * 0.96))
        ad = FakeAdapter(tail="❯ /compact")
        ad.send = mock.Mock(side_effect=harness.HarnessError("input path down"))
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["rows"][0]["actuation_state"],
                         "FAILED_TO_SUBMIT")
        self.assertIn("input path down", res["rows"][0]["actuation_reason"])
        ad.send.assert_called_once_with("h1", "", enter=True)

    def test_cli_verification_reads_have_a_short_per_call_timeout(self):
        ad = harness.OrcaAdapter("/fake/bin/orca")
        ad.read = mock.Mock(return_value=(
            "Compacting context\n"
            "  ⏵⏵ bypass permissions on · esc to interrupt"))
        accepted, _ = autocompact._verify_compact_submission(
            ad, "h1", "❯ ")
        self.assertTrue(accepted)
        ad.read.assert_called_once_with(
            "h1", limit=2000,
            timeout=autocompact.SUBMIT_VERIFY_READ_TIMEOUT_S)

    def test_post_send_read_failure_is_unknown_not_submission_success(self):
        self.plant(int(CODEX_WINDOW * 0.96))
        ad = FakeAdapter(tail="❯ /compact", post_send_tails=(
            OSError("renderer gone"), OSError("renderer gone")))
        with mock.patch.object(autocompact, "SUBMIT_VERIFY_READS", 2):
            res = autocompact.check(
                seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["rows"][0]["actuation_state"], "UNKNOWN")
        self.assertIn("re-read failed", res["rows"][0]["actuation_reason"])

    def test_unchanged_empty_composer_is_not_injection_success(self):
        self.plant(int(CODEX_WINDOW * 0.96))
        ad = FakeAdapter(tail="❯ ", consume_compact=False)
        with mock.patch.object(autocompact, "SUBMIT_VERIFY_READS", 3):
            res = autocompact.check(
                seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["rows"][0]["actuation_state"], "UNKNOWN")
        self.assertIn("unproven", res["rows"][0]["actuation_reason"])
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_unrelated_post_send_output_is_not_compaction_success(self):
        self.plant(int(CODEX_WINDOW * 0.96))
        ad = FakeAdapter(tail="❯ ", post_send_tails=(
            "[helm chat] unrelated notification",))
        with mock.patch.object(autocompact, "SUBMIT_VERIFY_READS", 2):
            res = autocompact.check(
                seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["rows"][0]["actuation_state"], "UNKNOWN")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_causal_context_400_recovers_on_next_cadence_for_both_redraws(self):  # noqa: VACUOUS_ASSERTION — every redraw subtest positively reaches pruned-resumed on its second cadence
        error = "API Error: 400 prompt is too long: context exceeds maximum"
        for tail in (error,
                     error + "\n❯ \n  ⏵⏵ bypass permissions on"):
            with self.subTest(tail=tail):
                path = autocompact._state_path()
                if os.path.exists(path):
                    os.unlink(path)
                self.plant(int(CODEX_WINDOW * 0.96))
                ad = FakeAdapter(tail="❯ /compact", post_send_tails=(tail,))
                first = autocompact.check(
                    seats=["codex"], post=False, adapter=ad)
                self.assertEqual(first["fired"], [])
                self.assertEqual(first["rows"][0]["actuation_state"],
                                 "CONTEXT_400")
                self.assertEqual(ad.sent, [("h1", "", True)])
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["codex"]["mode"],
                                     "context-400")

                with mock.patch.object(
                        autocompact, "_fire_prune_resume",
                        return_value=("pruned-resumed", "causal recovery")) as recover:
                    second = autocompact.check(
                        seats=["codex"], post=False, adapter=ad)
                self.assertEqual(second["fired"][0]["mode"],
                                 "pruned-resumed")
                recover.assert_called_once()

    def test_disappeared_causal_400_reopens_ordinary_compact_retry(self):
        self.plant(int(CODEX_WINDOW * 0.96))
        error = "API Error: 400 prompt is too long: context exceeds maximum"
        ad = FakeAdapter(tail="❯ /compact", post_send_tails=(error,))
        first = autocompact.check(
            seats=["codex"], post=False, adapter=ad)
        self.assertEqual(first["rows"][0]["actuation_state"], "CONTEXT_400")
        ad.tail, ad.post_send_tails = "❯ ", None
        second = autocompact.check(
            seats=["codex"], post=False, adapter=ad)
        self.assertEqual(second["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "", True),
                                   ("h1", "/compact", True)])

    def test_historical_400_cannot_impersonate_a_disappeared_causal_strike(self):  # noqa: VACUOUS_ASSERTION — the same pass positively injects after proving recovery was not called
        self.plant(int(CODEX_WINDOW * 0.96))
        error = "API Error: 400 prompt is too long: context exceeds maximum"
        before = error + "\n❯ /compact"
        ad = FakeAdapter(tail=before, post_send_tails=(error + "\n" + error,))
        first = autocompact.check(
            seats=["codex"], post=False, adapter=ad)
        self.assertEqual(first["rows"][0]["actuation_state"], "CONTEXT_400")
        with open(autocompact._state_path(), encoding="utf-8") as f:
            strike = json.load(f)["codex"]
        self.assertEqual(strike["context_400_line"], error)
        self.assertEqual(strike["context_400_baseline"], 1)

        ad.tail, ad.post_send_tails = error + "\n❯ ", None
        with mock.patch.object(autocompact, "_fire_prune_resume") as recover:
            second = autocompact.check(
                seats=["codex"], post=False, adapter=ad)
        recover.assert_not_called()
        self.assertEqual(second["fired"][0]["mode"], "injected")

    def test_causal_confirmation_uses_the_same_tail_window_as_its_baseline(self):  # noqa: VACUOUS_ASSERTION — the limit-aware adapter positively retries and injects after recovery stays uncalled
        self.plant(int(CODEX_WINDOW * 0.96))
        error = "API Error: 400 prompt is too long: context exceeds maximum"

        class LimitAwareAdapter(FakeAdapter):
            def __init__(self):
                super().__init__(tail="❯ /compact")
                self.send_count = 0
                self.returned_causal = False

            def read(self, handle, limit=3000, timeout=60):
                if self.send_count == 0:
                    return error + "\n❯ /compact" if limit > 2000 else "❯ /compact"
                if self.send_count == 1 and not self.returned_causal:
                    self.returned_causal = True
                    return error
                if self.send_count == 1:
                    return error + "\n❯ " if limit > 2000 else "❯ "
                return ("Compacting context\n"
                        "  ⏵⏵ bypass permissions on · esc to interrupt")

            def send(self, handle, text, enter=True):
                self.sent.append((handle, text, enter))
                self.send_count += 1

        ad = LimitAwareAdapter()
        first = autocompact.check(
            seats=["codex"], post=False, adapter=ad)
        self.assertEqual(first["rows"][0]["actuation_state"], "CONTEXT_400")
        with mock.patch.object(autocompact, "_fire_prune_resume") as recover:
            second = autocompact.check(
                seats=["codex"], post=False, adapter=ad)
        recover.assert_not_called()
        self.assertEqual(second["fired"][0]["mode"], "injected")

    def test_rendered_compact_text_is_not_composer_identity(self):  # noqa: VACUOUS_ASSERTION — non-empty tails each assert injected mode and exact send
        self.plant(int(CODEX_WINDOW * 0.96))
        for tail in ("assistant markdown:\n> /compact\n❯ ",
                     "❯ /compact\nassistant output continued\n❯ "):
            with self.subTest(tail=tail):
                ad = FakeAdapter(tail=tail)
                res = autocompact.check(seats=["codex"], post=False, adapter=ad)
                self.assertEqual(res["fired"][0]["mode"], "injected")
                self.assertEqual(ad.sent, [("h1", "/compact", True)])
                os.unlink(autocompact._state_path())

    def test_legacy_pending_latch_is_unverified_and_retries_enter(self):
        self.plant(int(CODEX_WINDOW * 0.96))
        pk.write_json(autocompact._state_path(), {
            "codex": {"fired_at": time.time(), "identity": SID,
                      "session": SID, "pct": 96.0, "mode": "pending"}})
        ad = FakeAdapter(tail="❯ /compact")
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "submitted")
        self.assertEqual(ad.sent, [("h1", "", True)])

    def test_latch_blocks_refire_then_rearms(self):
        self.plant(int(CODEX_WINDOW * 0.91))
        ad = FakeAdapter()
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        res2 = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res2["fired"], [])          # mid-compaction: latched
        self.assertTrue(res2["rows"][0].get("latched"))
        self.assertEqual(len(ad.sent), 1)
        self.plant(50000)                            # compaction landed
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.plant(int(CODEX_WINDOW * 0.95))         # next episode
        res4 = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(len(res4["fired"]), 1)
        self.assertEqual(len(ad.sent), 2)

    def test_injected_latch_never_ttl_retries_without_context_drop(self):
        os.environ["HELM_AUTOCOMPACT_LATCH_TTL"] = "1"
        self.plant(int(CODEX_WINDOW * 0.92))
        ad = FakeAdapter()
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        time.sleep(1.1)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertTrue(res["rows"][0].get("latched"))
        self.assertEqual(len(ad.sent), 1)

    def test_any_observed_context_drop_completes_the_episode(self):
        self.plant(int(CODEX_WINDOW * 0.98))
        ad = FakeAdapter()
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.plant(int(CODEX_WINDOW * 0.70))   # dropped below the 80% trigger: rearmed,
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)  # no re-fire
        self.assertEqual(res["fired"], [])
        self.plant(int(CODEX_WINDOW * 0.95))
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(len(res["fired"]), 1)
        self.assertEqual(len(ad.sent), 2)

    def test_session_change_rearms_even_while_context_stays_high(self):
        self.plant(int(CODEX_WINDOW * 0.92))
        ad = FakeAdapter()
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        sid = "22222222-2222-2222-2222-222222222222"
        self.plant(int(CODEX_WINDOW * 0.93), sid=sid)
        self.spawn("h1")
        with open(os.path.join(self.d, "spawn.json")) as f:
            rec = json.load(f)
        rec["session"] = sid
        with open(os.path.join(self.d, "spawn.json"), "w") as f:
            json.dump(rec, f)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(len(res["fired"]), 1)
        self.assertEqual(len(ad.sent), 2)

    def test_session_mismatch_never_authorizes_destructive_recovery(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN state and unchanged pane sends are positive refusal controls
        old = SID
        registered = "22222222-2222-2222-2222-222222222222"
        self.plant(int(CODEX_WINDOW * 0.97), sid=old)
        with open(os.path.join(self.d, "spawn.json")) as f:
            rec = json.load(f)
        rec["session"] = registered
        with open(os.path.join(self.d, "spawn.json"), "w") as f:
            json.dump(rec, f)
        tail = ("API Error: 400 prompt is too long: context exceeds maximum\n"
                "API Error: 400 prompt is too long: context exceeds maximum")
        ad = FakeAdapter(tail=tail)
        with mock.patch.object(autocompact, "_prune_context") as prune:
            res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        row = res["rows"][0]
        self.assertEqual(row["status"], "session-mismatch")
        prune.assert_not_called()
        self.assertEqual(res["fired"], [])
        self.assertEqual(row["actuation_state"], "UNKNOWN")
        self.assertEqual(ad.sent, [],
                         "no prompt was proven, so even ordinary /compact refuses")

    def test_session_change_between_scan_and_send_never_actuates_new_pane(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN plus the session-change reason are positive refusal controls
        self.plant(int(CODEX_WINDOW * 0.97))
        outer = self

        class ReplacingAdapter(FakeAdapter):
            def read(self, handle, limit=3000, timeout=60):
                if handle == "h1":
                    sid = "22222222-2222-2222-2222-222222222222"
                    with open(os.path.join(outer.d, "spawn.json")) as f:
                        rec = json.load(f)
                    rec.update(handle="h2", session=sid)
                    with open(os.path.join(outer.d, "spawn.json"), "w") as f:
                        json.dump(rec, f)
                    self.panes = [{"handle": "h2", "title": "replacement",
                                   "status": "connected"}]
                return ""

        ad = ReplacingAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["rows"][0]["actuation_state"], "UNKNOWN")
        self.assertIn("session changed", res["rows"][0]["actuation_reason"])
        self.assertEqual(ad.sent, [])

    def test_missing_context_does_not_clear_injected_latch(self):
        p = self.plant(int(CODEX_WINDOW * 0.94))
        ad = FakeAdapter()
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        os.unlink(p)
        gap = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(gap["rows"][0]["status"], "no-context-data")
        self.plant(int(CODEX_WINDOW * 0.95))
        again = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(again["fired"], [])
        self.assertTrue(again["rows"][0].get("latched"))
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_a_claude_model_seat_is_compacted_like_every_other_seat(self):
        """The skip is GONE. It was right only about CC's NATIVE autocompaction
        (which continues the turn); the owner wants deliberate self-compaction
        at 90% for every helm agent, and that is safe now that
        resumeturn.py restarts the turn loop afterward."""
        self.plant(int(CODEX_WINDOW * 0.95), model="claude-opus-4")
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["rows"][0]["status"], "ok")
        self.assertEqual(res["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_the_claude_gate_is_configurable_for_a_fleet_without_the_resume_leg(self):
        os.environ["HELM_AUTOCOMPACT_CLAUDE"] = "0"
        self.plant(int(CODEX_WINDOW * 0.95), model="claude-opus-4")
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["rows"][0]["status"], "claude-model")
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [])

    def test_stale_transcript_never_fires(self):
        self.plant(int(CODEX_WINDOW * 0.95), age_s=8 * 3600)
        res = autocompact.check(seats=["codex"], post=False,
                                adapter=FakeAdapter())
        self.assertEqual(res["rows"][0]["status"], "stale")
        self.assertEqual(res["fired"], [])

    def test_stale_proxy_log_never_fires(self):
        p = os.path.join(seat.seat_dir("codex"), "proxy.log")
        with open(p, "w") as f:
            f.write('{"session_id":"%s","usage":{"input_tokens":350000}}\n'
                    % SID)
        t = time.time() - 8 * 3600
        os.utime(p, (t, t))
        row = autocompact.read("codex")
        self.assertEqual(row["status"], "stale")
        self.assertGreater(row["age_s"], 7 * 3600)
        self.assertEqual(autocompact.check(
            seats=["codex"], post=False, adapter=FakeAdapter())["fired"], [])

    def test_recent_plain_log_line_cannot_freshen_old_usage_row(self):
        p = os.path.join(seat.seat_dir("codex"), "proxy.log")
        with open(p, "w") as f:
            f.write('{"session_id":"%s","usage":{"input_tokens":350000}}\n'
                    % SID)
            f.write("recent gin line without usage\n")
        row = autocompact.read("codex")
        self.assertEqual(row["source"], "proxy.log")
        self.assertEqual(row["status"], "context-undated")
        self.assertIsNone(row["age_s"])
        self.assertEqual(autocompact.check(
            seats=["codex"], post=False, adapter=FakeAdapter())["fired"], [])

    def test_dry_run_decides_but_never_injects(self):
        self.plant(int(CODEX_WINDOW * 0.91))
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad,
                                fire=False)
        self.assertTrue(res["rows"][0].get("would_fire"))
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [])
        # dry-run must not latch: a real pass afterwards still fires
        res2 = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(len(res2["fired"]), 1)

    def test_dry_run_never_repairs_a_stale_orca_register(self):
        self.plant(int(CODEX_WINDOW * 0.91))
        self.spawn("old", harness="orca")

        ad = FakeOrcaAdapter(panes=({
            "handle": "new", "title": "dynamic", "status": "connected",
            "writable": True, "orphaned": True, "pty_id": "pty-1",
            "worktree_id": "workspace:/w"},))
        identity = {"pid": 42, "pane_key": "tab:leaf",
                    "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(identity, None)):
            res = autocompact.check(
                seats=["codex"], post=False, adapter=ad, fire=False)
        self.assertTrue(res["rows"][0].get("would_fire"))
        with open(os.path.join(self.d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["handle"], "old")

    def test_mismatch_repairs_stale_orca_handle_from_live_transcript_session(self):
        live = "22222222-2222-2222-2222-222222222222"
        self.plant(int(CODEX_WINDOW * 0.91), sid=live)
        self.spawn("old", harness="orca")
        with open(os.path.join(self.d, "spawn.json"), encoding="utf-8") as f:
            rec = json.load(f)
        rec.update(pane_key="tab:leaf", worktree_id="workspace:/w")
        with open(os.path.join(self.d, "spawn.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f)
        ad = FakeOrcaAdapter(panes=({
            "handle": "new", "title": "dynamic", "status": "connected",
            "writable": True, "pty_id": "pty-1",
            "worktree_id": "workspace:/w"},), tail="❯ ")
        identity = {"pid": 42, "pane_key": "tab:leaf",
                    "worktree_id": "workspace:/w"}
        with mock.patch.object(
                seat, "_live_session_orca_identity",
                return_value=(identity, None)) as prove:
            res = autocompact.check(
                seats=["codex"], post=False, adapter=ad)
        prove.assert_called_once_with(self.d, live)
        self.assertEqual(res["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("new", "/compact", True)])
        with open(os.path.join(self.d, "spawn.json"), encoding="utf-8") as f:
            repaired = json.load(f)
        self.assertEqual(repaired["handle"], "new")
        self.assertEqual(repaired["session"], SID,
                         "handle repair must not rebind the session latch")
        second = autocompact.check(
            seats=["codex"], post=False, adapter=ad)
        self.assertEqual(second["fired"], [])
        self.assertTrue(second["rows"][0].get("latched"))
        self.assertEqual(ad.sent, [("new", "/compact", True)])

    def test_mismatch_stale_handle_refuses_ambiguous_live_session(self):  # noqa: VACUOUS_ASSERTION — exact UNKNOWN reason and unchanged register are positive controls
        live = "22222222-2222-2222-2222-222222222222"
        self.plant(int(CODEX_WINDOW * 0.91), sid=live)
        self.spawn("old", harness="orca")
        ad = FakeOrcaAdapter(panes=(), tail="❯ ")
        with mock.patch.object(
                seat, "_live_session_orca_identity",
                return_value=(None, "session has 2 exact live Claude processes")):
            res = autocompact.check(
                seats=["codex"], post=False, adapter=ad)
        row = res["rows"][0]
        self.assertEqual(res["fired"], [])
        self.assertEqual(row["actuation_state"], "UNKNOWN")
        self.assertIn("2 exact live Claude processes", row["actuation_reason"])
        self.assertEqual(ad.sent, [])
        with open(os.path.join(self.d, "spawn.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["handle"], "old")

    def test_read_blind_orphaned_handle_refuses_inject_and_hope(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN and read-live evidence positively identify the refusal
        self.plant(int(CODEX_WINDOW * 0.91))
        self.spawn("old", harness="orca")
        with open(os.path.join(self.d, "spawn.json")) as f:
            rec = json.load(f)
        rec.update(pane_key="old-tab:old-leaf",
                   worktree_id="workspace:/w")
        with open(os.path.join(self.d, "spawn.json"), "w") as f:
            json.dump(rec, f)

        ad = FakeOrcaAdapter(panes=({
            "handle": "new", "title": "dynamic", "status": "connected",
            "writable": True, "orphaned": True, "pty_id": "pty-1",
            "worktree_id": "workspace:/w"},))
        identity = {"pid": 42, "pane_key": "old-tab:old-leaf",
                    "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(identity, None)):
            res = autocompact.check(
                seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["rows"][0]["actuation_state"], "UNKNOWN")
        self.assertIn("read-live", res["rows"][0]["actuation_reason"])
        self.assertEqual(ad.sent, [])

    def test_registered_handle_ignores_dynamic_title(self):
        self.plant(int(CODEX_WINDOW * 0.91))
        ad = FakeAdapter(panes=({"handle": "h1", "title": "changed by CC"},))
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_register_without_exact_seat_never_authorizes_handle(self):
        self.plant(int(CODEX_WINDOW * 0.91))
        for claimed in (None, "codex-2"):
            with self.subTest(claimed=claimed):
                with open(os.path.join(self.d, "spawn.json"), "w") as f:
                    json.dump({"v": 1, "seat": claimed, "harness": "fake",
                               "handle": "h1", "session": SID}, f)
                ad = FakeAdapter()
                got, handle, detail = autocompact.resolve_pane("codex", ad)
                self.assertIsNone(got)
                self.assertIsNone(handle)
                self.assertIn("identity mismatch", detail)
                self.assertEqual(ad.sent, [])

    def test_unregistered_content_and_title_never_become_identity(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN and missing-register evidence positively identify the refusal
        # Launch text is copyable and titles are mutable; neither authorizes a
        # write to a live pane without the spawn register.
        os.unlink(os.path.join(self.d, "spawn.json"))
        self.plant(int(CODEX_WINDOW * 0.91))
        launch = os.path.join(self.d, "launch.sh")
        ad = FakeAdapter(panes=(
            {"handle": "h8", "title": "codex",
             "preview": "running %s HELM_CHAT_NAME=codex " % launch},))
        got, handle, detail = autocompact.resolve_pane("codex", ad)
        self.assertIsNone(got)
        self.assertIsNone(handle)
        self.assertIn("no authoritative spawn handle", detail)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["rows"][0]["status"], "session-unbound")
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["rows"][0]["actuation_state"], "UNKNOWN")
        self.assertIn("no authoritative spawn handle",
                      res["rows"][0]["actuation_reason"])
        self.assertEqual(ad.sent, [])

    def test_stale_handle_never_falls_back_to_matching_title(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN and stale-handle evidence positively identify the refusal
        self.spawn("gone")
        self.plant(int(CODEX_WINDOW * 0.91))
        ad = FakeAdapter(panes=({"handle": "wrong", "title": "codex",
                                 "preview": ""},))
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [])
        self.assertEqual(res["rows"][0]["actuation_state"], "UNKNOWN")
        self.assertIn("registered handle gone is not live",
                      res["rows"][0]["actuation_reason"])

    def test_disconnected_registered_handle_is_not_live(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN and disconnected evidence positively identify the refusal
        self.plant(int(CODEX_WINDOW * 0.91))
        ad = FakeAdapter(panes=({"handle": "h1", "title": "codex",
                                 "status": "disconnected"},))
        got, handle, detail = autocompact.resolve_pane("codex", ad)
        self.assertIs(got, ad)
        self.assertIsNone(handle)
        self.assertIn("disconnected", detail)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["rows"][0]["actuation_state"], "UNKNOWN")
        self.assertIn("disconnected", res["rows"][0]["actuation_reason"])
        self.assertEqual(ad.sent, [])

    def test_headless_record_goes_unknown_loud(self):
        self.spawn(None, harness="headless")
        self.plant(int(CODEX_WINDOW * 0.91))
        res = autocompact.check(seats=["codex"], post=False,
                                adapter=FakeAdapter())
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["rows"][0]["actuation_state"], "UNKNOWN")
        self.assertIn("headless", res["rows"][0]["actuation_reason"])

    def test_no_pane_goes_unknown_loud(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN is the positive no-pane classification control
        self.plant(int(CODEX_WINDOW * 0.91))
        ad = FakeAdapter(panes=())
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["rows"][0]["actuation_state"], "UNKNOWN")
        self.assertEqual(ad.sent, [])

    def test_unknown_actuation_posts_the_reason_not_fake_success(self):
        self.plant(int(CODEX_WINDOW * 0.91))
        with mock.patch("helm.chat.post") as post:
            autocompact.check(seats=["codex"], adapter=FakeAdapter(panes=()))
        body = post.call_args.args[0]
        self.assertIn("actuation state=UNKNOWN", body)
        self.assertIn("/compact was NOT injected", body)
        self.assertEqual(post.call_args.kwargs["who"], "autocompact")

    def test_overlapping_checks_inject_once(self):
        self.plant(int(CODEX_WINDOW * 0.91))
        entered, release = threading.Event(), threading.Event()
        calls, results = [], []

        def slow_fire(row, adapter):
            calls.append(row["seat"])
            entered.set()
            release.wait(2)
            return "injected", "test"

        def run():
            results.append(autocompact.check(
                seats=["codex"], post=False, adapter=FakeAdapter()))

        with mock.patch.object(autocompact, "_fire", side_effect=slow_fire):
            a = threading.Thread(target=run)
            b = threading.Thread(target=run)
            a.start()
            self.assertTrue(entered.wait(1))
            b.start()
            release.set()
            a.join(2)
            b.join(2)
        self.assertEqual(calls, ["codex"])
        self.assertEqual(sum(len(r["fired"]) for r in results), 1)
        self.assertTrue(any(r["rows"][0].get("latched") for r in results))

    def test_threshold_env_knob(self):
        os.environ["HELM_AUTOCOMPACT_THRESHOLD"] = "60"
        self.plant(int(CODEX_WINDOW * 0.65))
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(len(res["fired"]), 1)

    # -- surfaces ----------------------------------------------------------

    def test_report_lines_read_only(self):
        self.plant(int(CODEX_WINDOW * 0.50))
        lines = autocompact.report_lines()
        self.assertTrue(any("codex" in ln and "50.0%" in ln for ln in lines))
        self.assertFalse(os.path.exists(autocompact._state_path()))

    def test_instances_discovered(self):
        inst = os.path.join(seat.seat_dir("codex"), "instances", "codex-2")
        os.makedirs(inst, exist_ok=True)
        seats = autocompact.proxy_seats()
        self.assertIn("codex", seats)
        self.assertIn("codex-2", seats)

    def test_timer_defaults_to_one_minute(self):
        _, service, _, timer = autocompact._timer_units()
        self.assertIn("helm seat autocompact --once", service)
        self.assertNotIn("PYTHONPATH", service)
        self.assertNotIn("helm-wt", service)
        self.assertNotIn("sys.executable", service)
        self.assertIn("OnUnitActiveSec=60s", timer)

    def test_timer_never_captures_a_path_worktree_helm(self):
        with mock.patch.object(autocompact.shutil, "which",
                               return_value="/tmp/helm-wt/gone/bin/helm"):
            _, service, _, _ = autocompact._timer_units()
        self.assertIn(os.path.expanduser("~/.local/bin/helm"), service)
        self.assertNotIn("/tmp/helm-wt", service)

    def test_timer_apply_uses_validated_interval(self):
        with mock.patch.object(autocompact, "ensure_timer",
                               return_value=(True, "armed")) as ensure:
            rc = autocompact._install_timer(["--interval", "45", "--apply"])
        self.assertEqual(rc, 0)
        ensure.assert_called_once_with(45)

    def test_timer_rejects_nonpositive_interval(self):
        self.assertEqual(autocompact._install_timer(
            ["--interval", "0", "--apply"]), 2)


class CmdAutocompactCliSeam(unittest.TestCase):
    """The CLI guard accepts the verb's WHOLE documented+consumed surface.
    The fable composition review (2026-07-22) caught the guard refusing
    --once — which killed every installed systemd watchdog: the minted unit
    runs `helm seat autocompact --once` on each tick and exited 2 before
    check() ever ran. These pins tie the guard to its two masters: the unit
    template it mints and the usage synopsis it prints."""

    def _run(self, argv):
        with mock.patch.object(autocompact, "check",
                               return_value={"rows": [], "fired": []}) as c, \
                mock.patch.object(autocompact, "_install_timer",
                                  return_value=0) as t:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(buf):
                rc = autocompact.cmd_autocompact(argv)
        return rc, buf.getvalue(), c, t

    def test_systemd_unit_argv_is_accepted(self):
        # derive the argv from the unit template itself, so a template
        # change keeps this pin honest.
        execline = [l for l in autocompact._UNIT_SERVICE.splitlines()
                    if l.startswith("ExecStart=")][0]
        toks = execline.split()
        self.assertIn("autocompact", toks)
        tail = toks[toks.index("autocompact") + 1:]
        self.assertEqual(tail, ["--once"])  # today's exact unit argv
        rc, out, c, _ = self._run(tail)
        self.assertEqual(rc, 0, out)
        self.assertTrue(c.called, "the watchdog pass never ran")

    def test_every_documented_flag_is_accepted(self):
        # every flag in the usage SYNOPSIS lines must pass the guard —
        # 'accepted for interface stability' has to be true, not prose.
        syn = [l for l in autocompact._USAGE.splitlines()
               if "helm seat autocompact" in l or l.strip().startswith("[--")]
        flags = sorted(set(re.findall(r"--[a-z-]+", "\n".join(syn))))
        self.assertIn("--once", flags)
        val = {"--seat": "codex", "--threshold": "95", "--interval": "60"}
        for flag in flags:
            if flag in ("--install-timer", "--apply", "--interval"):
                argv = ["--install-timer"]
                if flag != "--install-timer":
                    argv += [flag] + ([val[flag]] if flag in val else [])
            else:
                argv = [flag] + ([val[flag]] if flag in val else [])
                argv += ["--dry-run"]
            rc, out, _, _ = self._run(argv)
            self.assertEqual(rc, 0, (flag, argv, out))
            self.assertNotIn("unknown arg", out, (flag, argv))


class HotBlockedTest(AutocompactTest):
    """A seat the watchdog can MEASURE but is forbidden to ACT on must be LOUD.

    WHY THIS CLASS EXISTS, measured 2026-07-29. ds4pro reached 1,016,359 tokens
    over 3,441 turns and five days with ZERO compactions, then wedged into a 400
    that /compact could not escape (the summarisation request replays the same
    oversized transcript). Its live session was never the REGISTERED one, so
    every actionable path in check() declined — correctly, because injecting
    into an unproven pane is how a directive reaches the wrong agent — and each
    refusal was silent. `fired: []` and no chat post is indistinguishable from
    all-clear.

    Live at the moment this landed, both invisible for the same reason: codex-2
    at 93.8% (session-unbound, 6.1 days) and codex-3 at 103.0% (stale, 22.7h).

    The fix is not to loosen the refusal. It is that a guard which declines to
    act must still speak, or its correctness becomes the thing that hides the
    failure. Bug class: watchdogs-correct-composition-holed."""

    def stale_hot(self, pct=0.95):
        """A seat over threshold whose transcript is too old to act on."""
        self.plant(int(CODEX_WINDOW * pct), age_s=30000)   # > FRESH_S (6h)

    def check_with_live_codex(self, **kwargs):
        """Run check with the live-process fact this test owns planted."""
        kwargs.setdefault("seats", ["codex"])
        kwargs.setdefault("post", False)
        kwargs.setdefault("adapter", FakeAdapter())
        with mock.patch.object(autocompact, "_live_seat_names",
                               return_value={"codex"}) as census:
            result = autocompact.check(**kwargs)
        census.assert_called_once_with()
        return result

    def test_a_stale_seat_over_threshold_is_reported_not_swallowed(self):
        self.stale_hot()
        res = self.check_with_live_codex()
        self.assertEqual(res["fired"], [], "stale must NOT be injected into")
        self.assertTrue(res["rows"][0].get("hot_blocked"))
        self.assertEqual([r["seat"] for r in res["hot"]], ["codex"],
                         "the refusal has to reach a human surface")

    def test_the_alert_names_the_pct_the_window_and_the_REASON(self):
        """An alert saying only 'cannot act' sends the reader back to the CLI.
        The three facts that decide what to do next — how close to the wall, of
        what window, and which identity check failed — belong in the line."""
        self.stale_hot()
        res = self.check_with_live_codex()
        text = autocompact._hot_text(res["hot"][0], 90)
        self.assertIn("codex", text)
        self.assertIn("95.0%", text)
        self.assertIn("{:,}".format(CODEX_WINDOW), text)
        self.assertIn("stale", text)

    def test_a_seat_STILL_TAKING_TURNS_is_never_told_to_clear_itself(self):
        """OWNER OBSERVATION, 2026-07-31, and it outranks this module's own
        prose: he watched a codex seat run at 100% context and keep working —
        "it seems to reliably be able to hit 100% context reported in cc and
        keep going ... they don't like to end turns, they just keep working
        forever as long as they have stuff to do."

        The alarm used to assert, for EVERY non-claude seat regardless of
        evidence, that reaching 100% wedges it into an inescapable 400, and to
        prescribe prune+resume and /clear. That is not noise, it is DESTRUCTIVE
        ADVICE aimed at a seat that is mid-lane: following it throws away the
        very context doing the work. A seat whose transcript grew seconds ago is
        demonstrably taking turns, and the remedy must say so."""
        self.plant(int(CODEX_WINDOW * 0.95))
        row = {"seat": "codex", "pct": 95.0, "window": CODEX_WINDOW,
               "status": "session-unbound", "age_s": 12}
        text = autocompact._hot_text(row, 80)
        self.assertIn("STILL TAKING TURNS", text)
        # KEY ON THE PRESCRIPTION, NOT THE TOKEN. My first version of this test
        # asserted the string "/clear" was absent and went RED against correct
        # code — because the working-seat text says "Do NOT prune, /clear or
        # respawn it", where the token appears inside a PROHIBITION. A word
        # match cannot tell an instruction from its opposite; that is the exact
        # confusion the hardcode rung exists to avoid, and it caught me writing
        # the test. So assert the imperative recovery sentence is gone and the
        # prohibition is present.
        self.assertNotIn("prune+resume it", text,
                         "a working seat was PRESCRIBED the recovery path")
        self.assertIn("Do NOT prune", text,
                      "the prohibition is the whole point of this arm")

    def test_a_seat_that_has_gone_QUIET_still_gets_the_recovery_path(self):
        """The other arm, and without it the fix above is just a mute button.
        A high-context seat whose transcript stopped growing is the case the
        recovery text was written for, and it must still arrive intact."""
        self.plant(int(CODEX_WINDOW * 0.95))
        row = {"seat": "codex", "pct": 95.0, "window": CODEX_WINDOW,
               "status": "session-unbound", "age_s": 4000}
        text = autocompact._hot_text(row, 80)
        self.assertIn("gone quiet", text)
        self.assertIn("prune+resume", text)
        self.assertNotIn("STILL TAKING TURNS", text)

    def test_an_UNDATED_seat_is_not_assumed_to_be_working(self):
        """age_s is None when the context could not be dated. Unknown must fall
        to the cautious arm — assuming a seat is fine because we failed to
        measure it is how a silent wedge survives a watchdog."""
        row = {"seat": "codex", "pct": 99.0, "window": CODEX_WINDOW,
               "status": "context-undated", "age_s": None}
        text = autocompact._hot_text(row, 80)
        self.assertNotIn("STILL TAKING TURNS", text)
        self.assertIn("an unknown time", text)

    def test_an_ACTIONABLE_seat_fires_and_is_never_double_reported(self):
        """The hot path must not shadow the fire path: a seat helm CAN compact
        gets compacted, and does not also generate a cannot-act alert."""
        self.plant(int(CODEX_WINDOW * 0.91))
        res = autocompact.check(seats=["codex"], post=False,
                                adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in res["fired"]], ["codex"])
        self.assertEqual(res["hot"], [])
        self.assertFalse(res["rows"][0].get("hot_blocked"))

    def test_a_stale_seat_BELOW_threshold_stays_quiet(self):
        """Staleness alone is not news — most seats are idle most of the time.
        Only staleness WHILE approaching the wall is worth waking anyone for."""
        self.plant(int(CODEX_WINDOW * 0.40), age_s=30000)
        res = autocompact.check(seats=["codex"], post=False,
                                adapter=FakeAdapter())
        self.assertEqual(res["hot"], [])
        self.assertFalse(res["rows"][0].get("hot_blocked"))

    def test_it_announces_ONCE_not_every_sixty_seconds(self):
        """The timer runs each minute. An alert that repeats every pass is
        filtered within the hour, which is the original silence by a longer
        road."""
        self.stale_hot()
        first = self.check_with_live_codex()
        second = self.check_with_live_codex()
        self.assertEqual(len(first["hot"]), 1)
        self.assertEqual(second["hot"], [], "same debt, second announcement")
        self.assertTrue(second["rows"][0].get("hot_latched"))
        self.assertTrue(second["rows"][0].get("hot_blocked"),
                        "latched is about SPEAKING, not about being fine")

    def test_it_speaks_again_when_the_seat_gets_measurably_worse(self):
        """Latching on the pct BUCKET, not on the seat: sitting at 94% forever
        is one message, but climbing toward the wall is new information every
        five points. This is the difference between a latch and a mute."""
        self.stale_hot(0.91)
        self.assertEqual(len(self.check_with_live_codex()["hot"]), 1)
        self.stale_hot(1.03)                       # 91% -> 103%, past the wall
        again = self.check_with_live_codex()
        self.assertEqual(len(again["hot"]), 1,
                         "a seat crossing 100% must re-announce")

    def test_recovery_drops_the_latch_so_the_NEXT_episode_is_not_pre_muted(self):
        """The subtle one. If the latch outlived the episode, a seat that
        recovered and later went hot again would be greeted by its own stale
        latch and stay silent — the guard would work exactly once per seat,
        forever."""
        self.stale_hot()
        self.assertEqual(len(self.check_with_live_codex()["hot"]), 1)
        self.plant(int(CODEX_WINDOW * 0.20))       # re-bound + drained
        self.check_with_live_codex()
        self.stale_hot()                           # a NEW episode, same shape
        third = self.check_with_live_codex()
        self.assertEqual(len(third["hot"]), 1,
                         "the latch must not survive the debt it described")

    def test_a_seat_with_NO_PROCESS_is_dead_not_hot(self):
        """A seat nobody launched cannot wedge, so "re-bind it or /clear it" is
        advice about a pane that does not exist.

        Measured 2026-07-29, one hour after the hot rung shipped: codex-2
        (93.8%, 6.2d) and codex-3 (103.0%, 23.7h) were being announced as hot
        while the process census showed NEITHER had a claude process at all.
        Their percentages came from abandoned transcript files. proxywatch had
        already written the law — a seat nobody launched is not hung, it is
        off — and the hot rung shipped without it."""
        self.stale_hot()
        with mock.patch.object(autocompact, "_live_seat_names",
                               return_value=set()):
            res = autocompact.check(seats=["codex"], post=False,
                                    adapter=FakeAdapter())
        self.assertTrue(res["rows"][0].get("dead_holding_state"))
        self.assertFalse(res["rows"][0].get("hot_blocked"))
        self.assertEqual(res["hot"], [], "a dead seat is not a hot seat")
        self.assertEqual([r["seat"] for r in res["dead"]], ["codex"])
        self.assertIn("NO LIVE PROCESS",
                      autocompact._dead_text(res["dead"][0], 90))

    def test_a_seat_WITH_a_process_stays_hot(self):
        """The counterfactual. Without it the previous test passes just as well
        against a rung that calls everything dead."""
        self.stale_hot()
        with mock.patch.object(autocompact, "_live_seat_names",
                               return_value={"codex"}):
            res = autocompact.check(seats=["codex"], post=False,
                                    adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in res["hot"]], ["codex"])
        self.assertEqual(res["dead"], [])

    def test_an_UNREADABLE_census_still_alerts_rather_than_going_quiet(self):
        """None is not an empty set. If "I could not look" were treated as
        "nothing is alive", every hot seat would be reclassified dead and the
        alert would mute itself — the exact silence this rung exists to end."""
        self.stale_hot()
        with mock.patch.object(autocompact, "_live_seat_names",
                               return_value=None):
            res = autocompact.check(seats=["codex"], post=False,
                                    adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in res["hot"]], ["codex"])
        self.assertEqual(res["dead"], [])

    def test_the_None_comes_from_a_BLIND_census_not_only_from_an_EXCEPTION(self):
        """WHERE None IS DERIVED, not where it is honoured. The three tests
        above mock `_live_seat_names` and so prove only that `check()` reads
        its answer; this drives the function itself.

        `proxywatch._live_seats` reports the names it read AND whether it could
        read every claude on the host (@codex round 7, dispatch 90845108). A
        partial census hands back a set that LOOKS like an answer, and this
        rung's whole contract is that a look which did not fully happen is
        None. Measured with the flag dropped: the set arrives non-None, every
        hot seat reads as dead-holding-state, and the alert mutes itself —
        which is the silence this rung exists to end, rebuilt one layer down.
        """
        from helm import proxywatch
        with mock.patch.object(proxywatch, "_live_seats",
                               return_value=(set(), "1 live claude process "
                                                    "could not be identified")):
            self.assertIsNone(autocompact._live_seat_names(),
                              "a BLIND census came back as a confident empty "
                              "set — 'I could not look' became 'nothing is "
                              "alive'")
        with mock.patch.object(proxywatch, "_live_seats",
                               return_value=({"codex"}, None)):
            self.assertEqual(autocompact._live_seat_names(), {"codex"},
                             "a census that COULD see the whole host must "
                             "still deliver its names")
        with mock.patch.object(proxywatch, "_live_seats",
                               return_value=(set(), None)):
            self.assertEqual(autocompact._live_seat_names(), set(),
                             "a PROVEN-empty census is an answer, not "
                             "blindness — collapsing it to None would call "
                             "every unlaunched seat unknown")

    def test_a_BLIND_census_keeps_a_hot_seat_HOT_end_to_end(self):
        """The outcome, through the real `check()`, with the census mocked one
        layer BELOW the rung under test — so the derivation is in the path."""
        from helm import proxywatch
        self.stale_hot()
        with mock.patch.object(proxywatch, "_live_seats",
                               return_value=(set(), "1 live claude process "
                                                    "could not be identified")):
            res = autocompact.check(seats=["codex"], post=False,
                                    adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in res["hot"]], ["codex"])
        self.assertEqual(res["dead"], [],
                         "a seat helm could not look for was pronounced dead")

    def test_a_dry_run_reports_without_consuming_the_live_latch(self):
        """--dry-run is what an operator types to LOOK. If looking spent the
        latch, the next real timer pass would stay silent about a seat the
        operator had just been shown was dying."""
        self.stale_hot()
        dry = self.check_with_live_codex(fire=False)
        self.assertTrue(dry["rows"][0].get("hot_blocked"), "dry run still SEES")
        self.assertEqual(dry["hot"], [], "but announces nothing itself")
        live = self.check_with_live_codex()
        self.assertEqual(len(live["hot"]), 1,
                         "the real pass must still have its say")


class SilentBackstopTest(AutocompactTest):
    """The AS-PREVENTED guard for "autocompact ACTUALLY fires at 80" (owner
    2026-07-29): a seat over threshold that does NOT compact must never fall
    through in silence again. Fire/latch/hot/dead each answer a SPECIFIC "why
    didn't it compact"; `_silently_dropped` is the general invariant behind all
    of them, so a future edit that reopens a hole in any specific rung lights
    this alarm instead of going quiet. Bug class: watchdogs-correct-composition-
    holed."""

    def _blocked_hot_row(self, pct=0.97):
        """One measured, non-actionable hot row for composition-hole tests."""
        return {"seat": "codex", "family": "codex",
                "ctx_tokens": int(CODEX_WINDOW * pct), "pct": pct * 100,
                "window": CODEX_WINDOW, "status": "context-undated",
                "session": SID, "registered_session": SID,
                "pane_handle": "h1", "age_s": None}

    def test_silently_dropped_is_a_total_verifiable_predicate(self):
        thr = 80
        over = {"seat": "codex", "pct": 95.0, "status": "session-mismatch",
                "window": CODEX_WINDOW}
        self.assertTrue(autocompact._silently_dropped(over, thr))
        # ANY single handled flag clears it — every rung's own marker
        for flag in ("mode", "latched", "would_fire", "would_rebrief",
                     "would_recover", "would_clear", "overflow", "hot_blocked",
                     "dead_holding_state"):
            with self.subTest(flag=flag):
                self.assertFalse(autocompact._silently_dropped(
                    dict(over, **{flag: True}), thr))
        # below threshold / unknown pct / the by-design claude gate are not silent
        self.assertFalse(autocompact._silently_dropped(dict(over, pct=50.0), thr))
        self.assertFalse(autocompact._silently_dropped(dict(over, pct=None), thr))
        self.assertFalse(autocompact._silently_dropped(
            dict(over, status="claude-model"), thr))
        # an 'ok' seat over threshold carrying NO fire flag IS silent — precisely
        # the fire-path bug this backstop refuses to hide.
        self.assertTrue(autocompact._silently_dropped(dict(over, status="ok"), thr))

    def test_a_hole_in_the_rungs_surfaces_instead_of_going_silent(self):
        """Punch the exact regression the owner named: a rung above stops
        classifying an over-threshold seat. With BOTH the hot and dead rungs
        disabled, a context-undated seat at 97% has no specific answer left —
        the fire path skips it and, before this backstop, `fired: []` with no
        post was indistinguishable from all-clear. The backstop makes it loud."""
        row = self._blocked_hot_row()
        with mock.patch.object(autocompact, "scan", return_value=[row]), \
                mock.patch.object(autocompact, "_hot_blocked", return_value=False), \
                mock.patch.object(autocompact, "_dead_holding_state",
                                  return_value=False), \
                mock.patch("helm.chat.post") as post:
            res = autocompact.check(seats=["codex"], adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in res["silent"]], ["codex"])
        self.assertEqual(res["fired"], [])
        self.assertTrue(res["rows"][0].get("silent_non_fire"))
        bodies = [c.args[0] for c in post.call_args_list]
        self.assertTrue(any("SILENT NON-FIRING" in b and "context-undated" in b
                            for b in bodies), bodies)

    def test_normal_operation_leaves_the_backstop_empty(self):
        """No double-report: in normal operation the hot rung claims every
        non-actionable over-threshold seat, so it is hot, never ALSO silent."""
        row = self._blocked_hot_row()
        with mock.patch.object(autocompact, "scan", return_value=[row]), \
                mock.patch.object(autocompact, "_live_seat_names",
                                  return_value={"codex"}):
            res = autocompact.check(seats=["codex"], post=False,
                                    adapter=FakeAdapter())
        self.assertEqual(res["silent"], [], "a claimed seat is never silent too")
        self.assertEqual([r["seat"] for r in res["hot"]], ["codex"])

    def test_a_fired_seat_is_never_also_silent(self):
        """The seat helm CAN compact is compacted and does not also read as a
        silent drop — the backstop only fires for the truly unclaimed."""
        self.plant(int(CODEX_WINDOW * 0.91))
        res = autocompact.check(seats=["codex"], post=False, adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in res["fired"]], ["codex"])
        self.assertEqual(res["silent"], [])
        self.assertFalse(res["rows"][0].get("silent_non_fire"))

    def test_the_backstop_announces_once_not_every_sixty_seconds(self):
        """The timer runs each minute; a backstop that repeats every pass trains
        the room to filter it — the same silence by a longer road (HOT_BUCKET
        latch, shared with the hot/dead rungs)."""
        row = self._blocked_hot_row()
        with mock.patch.object(autocompact, "scan", return_value=[row]), \
                mock.patch.object(autocompact, "_hot_blocked", return_value=False), \
                mock.patch.object(autocompact, "_dead_holding_state",
                                  return_value=False):
            first = autocompact.check(seats=["codex"], post=False,
                                      adapter=FakeAdapter())
            second = autocompact.check(seats=["codex"], post=False,
                                       adapter=FakeAdapter())
        self.assertEqual(len(first["silent"]), 1)
        self.assertEqual(second["silent"], [], "same debt, second announcement")
        self.assertTrue(second["rows"][0].get("hot_latched"))
        self.assertTrue(second["rows"][0].get("silent_non_fire"),
                        "latched is about SPEAKING, not about being fine")


class Ds4proWindowIsProbeBackedTest(unittest.TestCase):
    """ds4pro pins 1000000 off a PUBLISHED reading, and grok alone is left
    unpinned.

    THIS CLASS WAS Ds4proWindowIsUnpinnedTest UNTIL 2026-08-03 and both its
    name and its docstring became lies that day, so both were rewritten rather
    than patched. It used to assert ds4pro pinned nothing, on the reasoning
    that the unmeasured leg governs and an unpinned family lands on the
    conservative default. What that reasoning missed was already written in
    ds4pro's own FAMILIES comment: OpenRouter publishes context_length=1048576
    for deepseek/deepseek-v4-pro on its PUBLIC, no-auth /v1/models (read
    2026-08-02). The family had a measurement the whole time and was wearing
    gemini's evidence gap — gemini's proxy /v1/models returns only {id, object,
    owned_by}, which is TRUE OF GEMINI AND NOT OF THIS FAMILY.

    WHAT DID NOT CHANGE. The 2026-07-29 incident stands (ds4pro hard-down,
    every wake 400ing "Request exceeds the context window", /compact ITSELF
    400ing, only an injected /clear recovering it, while the seat posted
    healthy-looking recaps so the room could not tell) and it recorded NO token
    count, so nothing enforceable came out of it. The direction still is not
    symmetric: understating costs one early compaction, overstating wedges the
    seat with no in-band exit. And pool_default is still opencode-go, whose
    window nobody has read — 1000000 assumes the legs share a window, 4.6%
    under the leg that is published.

    THE CONTROL THIS CLASS EXISTS TO KEEP is not "ds4pro is unpinned"; it is
    that `_window()` does not answer ONE NUMBER FOR EVERYBODY. That control now
    runs off grok, the last unpinned family, plus a pinned family that
    disagrees with it."""

    def test_ds4pro_pins_the_window_its_endpoint_publishes(self):
        # UNCONDITIONAL POSITIVE CONTROL on the same observable, kept from the
        # version that asserted the opposite: a family that pins for a
        # DIFFERENT reason still pins, so a FAMILIES table that had lost
        # max_context everywhere — or a typo'd key name — cannot satisfy this.
        self.assertEqual(seat.FAMILIES["kimi"].get("max_context"), 1000000)
        self.assertEqual(seat.FAMILIES["ds4pro"].get("max_context"), 1000000)
        # …and it is PROBE-backed, not owner-backed. Reading the entry has to
        # tell you which, or the two grades have collapsed into one.
        self.assertGreaterEqual(
            seat.FAMILIES["ds4pro"]["probed_context_length"],
            seat.FAMILIES["ds4pro"]["max_context"],
            "the pin must sit under the context_length the endpoint reports")
        self.assertIsNone(seat.FAMILIES["ds4pro"].get("owner_stated_window"),
                          "ds4pro's number is published, not spoken — filing "
                          "it under the owner hides a real measurement")

    def test_window_does_not_answer_one_number_for_everybody(self):
        """THE CONTROL THAT SURVIVED THE RENAME. Three families must resolve
        three different ways or `_window()` has stopped resolving anything:
        grok falls through _assume_window() to CC's 200k, ds4pro pins its
        published 1M, and codex pins its own smaller ceiling-checked number."""
        win, why = autocompact._window("grok")
        self.assertEqual(win, autocompact.CC_ASSUMED_WINDOW)
        self.assertEqual(why, "cc-assumed-default")
        # GROK CARRIES THE UNPINNED POSTURE ALONE NOW. This arm read
        # `for fam in ("gemini", "grok")`, then ds4pro's own assertion, until
        # 2026-08-03 pinned gemini (owner-stated) and ds4pro (probed). Neither
        # was allowed to simply LEAVE — keeping them as same-answer controls
        # would assert something now false, and deleting them would throw away
        # the only thing distinguishing a working _window() from one that
        # answers the default for everybody. Both changed SIDES instead.
        self.assertNotEqual(autocompact._window("ds4pro"), (win, why),
                            "ds4pro's pinned window vanished — if that was "
                            "deliberate, it is the unpinned control again and "
                            "this arm must become assertEqual")
        self.assertNotEqual(autocompact._window("gemini"), (win, why),
                            "gemini's pinned window vanished — if that was "
                            "deliberate, move it back into the control above")
        # and a THIRD distinct answer, so "not the default" cannot be
        # satisfied by every family sharing one pinned number either.
        self.assertNotEqual(autocompact._window("codex"),
                            autocompact._window("ds4pro"),
                            "codex and ds4pro resolve to the same window — "
                            "_window() is no longer per-family")

    def test_a_measured_family_still_pins_its_own_window(self):
        """The control that keeps the arm above from being satisfied by a
        _window that always answers the default: kimi's 1M is MEASURED (probed
        context_length off /v1/models) and must survive."""
        win, why = autocompact._window("kimi")
        self.assertEqual(why, "FAMILIES.max_context")
        self.assertEqual(win, 1000000)


if __name__ == "__main__":
    unittest.main()


class RefusalLoopEscalatesTest(AutocompactTest):
    """The safety refusal is correct; it just had NO EXIT.

    MEASURED 2026-08-03, 400 rows of the helm chat log: `autocompact` REFUSED 42
    times — gemini 29, codex 6, ds4pro 4, codex-3 3 — and gemini's percentages
    were MONOTONICALLY CLIMBING: 112.4, 115.1, 120.1, 125.9, 172.9, 176.7,
    183.0, 185.5, 190.5, 195.4, 196.3, 200.1%. Every one of those lines ended
    "the next pass will re-measure the pane rather than latch this refusal as
    success", and every pass did exactly that, and nothing ever told anyone the
    loop was not converging.

    Refusing to queue /compact into an open turn is RIGHT (9c59742 added the
    empty-composer proof for a real hazard). The defect is that a seat wedged in
    a turn that never completes never has an empty composer, so the seat that
    most needs compaction is structurally guaranteed never to get it. Bug class:
    watchdogs-correct-composition-holed.

    THE DISCRIMINATOR: busy and wedged are indistinguishable in ONE reading —
    which is why _fire cannot tell them apart — but NOT across many. A busy
    seat's percentage FALLS when its turn lands and it compacts. gemini's only
    ever rose."""

    RUNNING = "work\nesc to interrupt"

    def refuse(self, pct, tail=None):
        """One pass against a seat over threshold whose pane holds an open turn.
        Returns the check() result; the row carries actuation_state=RUNNING."""
        self.plant(int(CODEX_WINDOW * pct))
        return autocompact.check(seats=["codex"], post=False,
                                 adapter=FakeAdapter(tail=tail or self.RUNNING))

    def test_the_fixture_really_refuses_and_never_injects(self):  # noqa: VACUOUS_ASSERTION — the empty ad.sent is controlled unconditionally by the IDLE arm below, which sends /compact through the same adapter
        """The premise this whole class rests on, asserted rather than assumed:
        an over-threshold seat with an open turn refuses, and the refusal is the
        RUNNING one — not staleness, not identity.

        WITH ITS POSITIVE CONTROL ON THE SAME OBSERVABLE, because `ad.sent == []`
        is exactly the assertion a broken fixture satisfies for free: if the seat
        were below threshold, unregistered or unmeasurable, nothing would be sent
        either and this whole class would test nothing. The IDLE arm proves the
        same fixture DOES send when the pane allows it, so the empty list above
        is the open turn's doing."""
        self.plant(int(CODEX_WINDOW * 1.12))
        ad = FakeAdapter(tail=self.RUNNING)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["rows"][0]["actuation_state"], "RUNNING")
        self.assertIn("open turn", res["rows"][0]["actuation_reason"])
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [], "the cure is escalation, never force")
        idle = FakeAdapter(tail="❯ ")
        res = autocompact.check(seats=["codex"], post=False, adapter=idle)
        self.assertEqual(idle.sent, [("h1", "/compact", True)],
                         "the fixture cannot inject at all — every 'never "
                         "injected' assertion in this class is vacuous")
        self.assertEqual([r["seat"] for r in res["fired"]], ["codex"])

    def test_a_climbing_series_escalates_at_the_threshold_and_only_once(self):  # noqa: VACUOUS_ASSERTION — the empty `wedged` lists carry their own unconditional positive control: the same observable on the third pass IS ["codex"]
        """gemini, compressed. Refusals 1 and 2 stay quiet — one long turn is
        not news — and the third, still climbing, is the wedge signal."""
        self.assertEqual(autocompact.WEDGE_REFUSALS, 3)
        first = self.refuse(1.12)
        second = self.refuse(1.15)
        self.assertEqual(first["wedged"], [], "one refusal is not a wedge")
        self.assertEqual(second["wedged"], [], "two refusals is not a wedge")
        third = self.refuse(1.20)
        self.assertEqual([r["seat"] for r in third["wedged"]], ["codex"])
        self.assertEqual(third["rows"][0]["refusal_wedge"]["n"], 3)
        # and it does NOT repeat every sixty seconds while it sits there
        fourth = self.refuse(1.21)
        self.assertEqual(fourth["wedged"], [], "the alarm repeated itself")
        self.assertTrue(fourth["rows"][0].get("wedge_latched"))
        self.assertEqual([r["seat"] for r in fourth["alarms"]], ["codex"])
        self.assertEqual(fourth["rows"][0]["refusal_alarm"]["state"], "active")

    def test_an_ACTIVE_alarm_discharges_on_observed_below_threshold_context(self):
        for pct in (1.12, 1.15, 1.20):
            alarmed = self.refuse(pct)
        self.assertEqual([r["seat"] for r in alarmed["alarms"]], ["codex"])
        self.assertIn("codex", pk.read_json(autocompact._state_path(), {})[
            autocompact._REFUSAL_ALARM_KEY])

        self.plant(int(CODEX_WINDOW * 0.70))
        with mock.patch("helm.chat.post") as post:
            recovered = autocompact.check(
                seats=["codex"], post=True, adapter=FakeAdapter())
        row = recovered["rows"][0]
        self.assertEqual(post.call_count, 1)
        self.assertIn("WATCHDOG DISCHARGED", post.call_args.args[0])
        self.assertIn("OBSERVED", post.call_args.args[0])
        self.assertLess(row["pct"], 80)
        self.assertEqual([r["seat"] for r in recovered["discharged"]],
                         ["codex"])
        self.assertEqual(row["refusal_alarm"]["state"], "discharged")
        self.assertEqual(recovered["alarms"], [])
        self.assertNotIn("codex", pk.read_json(
            autocompact._state_path(), {})[autocompact._REFUSAL_ALARM_KEY])

        for pct in (1.12, 1.15, 1.20):
            rearmed = self.refuse(pct)
        self.assertEqual([r["seat"] for r in rearmed["wedged"]], ["codex"])

    def test_a_quiet_over_threshold_pass_retains_the_ACTIVE_alarm(self):
        for pct in (1.12, 1.15, 1.20):
            self.refuse(pct)
        self.plant(int(CODEX_WINDOW * 0.95), age_s=8 * 3600)
        quiet = autocompact.check(
            seats=["codex"], post=False, adapter=FakeAdapter())
        row = quiet["rows"][0]
        self.assertEqual(row["status"], "stale")
        self.assertGreaterEqual(row["pct"], 80)
        self.assertNotIn("actuation_state", row)
        self.assertEqual([r["seat"] for r in quiet["alarms"]], ["codex"])
        self.assertEqual(quiet["discharged"], [])
        self.assertNotIn("codex", pk.read_json(
            autocompact._state_path(), {})[autocompact._REFUSAL_KEY])

        self.plant(int(CODEX_WINDOW * 0.70))
        recovered = autocompact.check(
            seats=["codex"], post=False, adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in recovered["discharged"]],
                         ["codex"])

    def test_session_mismatch_refusal_remains_ACTIVE_not_DISCHARGED(self):  # noqa: VACUOUS_ASSERTION — exact mismatch status, RUNNING refusal, and a persisted ACTIVE alarm prove the path executed; the empty send/discharge lists assert the prohibited opposite transitions
        live = "22222222-2222-2222-2222-222222222222"
        ad = FakeAdapter(tail=self.RUNNING)
        for pct in (1.12, 1.15, 1.20):
            self.plant(int(CODEX_WINDOW * pct), sid=live)
            res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        row = res["rows"][0]
        self.assertEqual(row["status"], "session-mismatch")
        self.assertEqual(row["actuation_state"], "RUNNING")
        self.assertEqual(ad.sent, [])
        self.assertEqual([r["seat"] for r in res["alarms"]], ["codex"])
        self.assertEqual(res["discharged"], [])
        self.assertEqual(row["refusal_alarm"]["state"], "active")

    def test_a_FLAT_series_still_escalates_because_flat_is_not_falling(self):
        """The wedge signal is NON-FALLING, not strictly rising. A seat pinned at
        one percentage across N refusals has a turn that is not landing either —
        reading only a strict climb would let the quietest wedge through."""
        for _ in range(3):
            res = self.refuse(1.12)
        self.assertEqual([r["seat"] for r in res["wedged"]], ["codex"])
        self.assertEqual(res["rows"][0]["refusal_wedge"]["first_pct"],
                         res["rows"][0]["refusal_wedge"]["last_pct"])

    def test_a_FALLING_percentage_resets_and_NEVER_escalates(self):  # noqa: VACUOUS_ASSERTION — the quiet third pass is controlled by the fifth, which escalates on the same observable
        """THE NEGATIVE CONTROL, and the reason this rung is allowed to exist. A
        busy seat refuses too — its turn is open, so the guard is right to
        decline — but when that turn lands it compacts and its percentage drops.
        That seat must never be called wedged, however many refusals it racks
        up, or the alarm is just a slower version of the noise it replaces."""
        self.refuse(1.12)
        self.refuse(1.15)
        third = self.refuse(0.95)         # turn landed; context SHED
        self.assertEqual(third["wedged"], [], "a recovering seat was called wedged")
        self.assertEqual(third["alarms"], [],
                         "a pre-alarm fall invented an active alarm")
        self.assertEqual(third["discharged"], [],
                         "a pre-alarm fall invented a discharge")
        self.assertEqual(third["rows"][0]["refusal_wedge"] if
                         third["rows"][0].get("refusal_wedge") else None, None)
        fourth = self.refuse(0.96)
        fifth = self.refuse(0.97)
        self.assertEqual(fourth["wedged"], [])
        # the counter restarted AT the drop, so the third pass is the escalation
        self.assertEqual([r["seat"] for r in fifth["wedged"]], ["codex"])
        self.assertAlmostEqual(fifth["rows"][0]["refusal_wedge"]["first_pct"],
                               round(100.0 * int(CODEX_WINDOW * 0.95) /
                                     CODEX_WINDOW, 1), places=1)

    def test_a_seat_that_stops_refusing_re_arms_completely(self):  # noqa: VACUOUS_ASSERTION — the quiet first refusal is controlled unconditionally by the two that follow it, which escalate on the same observable
        """A seat whose pane goes IDLE is compacted by the normal path; its
        series must go with it, or the NEXT episode arrives pre-counted and
        escalates on its first refusal."""
        self.refuse(1.12)
        self.refuse(1.15)
        idle = self.refuse(1.16, tail="❯ ")
        self.assertEqual([r["seat"] for r in idle["fired"]], ["codex"])
        self.assertEqual(idle["wedged"], [])
        self.plant(int(CODEX_WINDOW * 0.60))   # compaction landed
        autocompact.check(seats=["codex"], post=False, adapter=FakeAdapter())
        one = self.refuse(1.20)
        self.assertEqual(one["wedged"], [],
                         "a fresh refusal escalated on its first pass")
        # POSITIVE CONTROL on the same observable: re-armed must mean COUNTING
        # AGAIN FROM ONE, not switched off. Without this arm, a tracker that
        # simply stopped escalating after any recovery would satisfy the
        # assertion above forever — the mute the whole rung exists to prevent.
        self.refuse(1.21)
        third = self.refuse(1.22)
        self.assertEqual([r["seat"] for r in third["wedged"]], ["codex"])
        self.assertEqual(third["rows"][0]["refusal_wedge"]["n"], 3)

    def test_it_speaks_again_only_when_the_wedge_measurably_worsens(self):  # noqa: VACUOUS_ASSERTION — the silent 3-point climb is controlled by the 6-point climb, which re-announces on the same observable
        """watchdog.py's law, in this module's unit of worsening (HOT_BUCKET):
        sitting at 120% forever is one message; climbing another five points is
        new information."""
        for pct in (1.12, 1.15, 1.20):
            res = self.refuse(pct)
        self.assertEqual(len(res["wedged"]), 1)
        self.assertEqual(self.refuse(1.23)["wedged"], [],
                         "3 points of climb is not measurable worsening")
        again = self.refuse(1.26)          # 120.x -> 126.x, past HOT_BUCKET
        self.assertEqual([r["seat"] for r in again["wedged"]], ["codex"],
                         "a wedge climbing past the bucket must re-announce")

    def test_the_alert_names_the_seat_the_count_and_the_whole_trend(self):
        """An alert saying "wedged" sends the reader back to the CLI. What
        decides the next action is HOW MANY refusals, from WHAT percentage to
        WHAT percentage, and that it never fell — that is the evidence the seat
        is wedged rather than busy."""
        for pct in (1.12, 1.15, 1.20):
            res = self.refuse(pct)
        text = autocompact._wedge_text(res["wedged"][0], 80)
        self.assertIn("codex", text)
        self.assertIn("3 consecutive refusals", text)
        self.assertIn("RUNNING", text)
        self.assertIn("CLIMBING", text)
        self.assertIn("112.", text)               # where the episode started
        self.assertIn("120.", text)               # where it is now
        self.assertIn("[watchdogs-correct-composition-holed]", text)

    def test_failed_submission_escalation_names_its_actual_latest_reason(self):
        reason = "DISTINCTIVE SUBMISSION FAILURE"
        row = {"seat": "codex", "window": CODEX_WINDOW,
               "actuation_reason": reason,
               "refusal_wedge": {"n": 3, "state": "FAILED_TO_SUBMIT",
                                  "first_pct": 90.0, "last_pct": 95.0}}
        text = autocompact._wedge_text(row, 80)
        self.assertIn(reason, text)
        self.assertNotIn("metaharness accepted Enter", text)
        self.assertIn("did NOT call or latch", text)

    def test_escalation_posts_to_chat_and_still_never_touches_the_pane(self):  # noqa: VACUOUS_ASSERTION — the empty ad.sent is controlled unconditionally by the IDLE pass below, which sends /compact through the same fixture
        """End to end through the real post path. The cure is escalation: the
        loud line reaches a human and NOTHING is injected, killed or signalled."""
        for pct in (1.12, 1.15):
            self.refuse(pct)
        self.plant(int(CODEX_WINDOW * 1.20))
        ad = FakeAdapter(tail=self.RUNNING)
        with mock.patch("helm.chat.post") as post:
            res = autocompact.check(seats=["codex"], adapter=ad)
        self.assertEqual([r["seat"] for r in res["wedged"]], ["codex"])
        self.assertEqual(ad.sent, [], "escalation actuated a pane")
        bodies = [c.args[0] for c in post.call_args_list]
        self.assertTrue(any("WEDGED BEHIND AUTOCOMPACT'S OWN SAFETY REFUSAL" in b
                            for b in bodies), bodies)
        # POSITIVE CONTROL on the same observable: this adapter CAN be written
        # to. An `ad.sent == []` proved against a pane nothing could ever reach
        # is the assertion that always passes and never means anything.
        idle = FakeAdapter(tail="❯ ")
        with mock.patch("helm.chat.post"):
            autocompact.check(seats=["codex"], adapter=idle)
        self.assertEqual(idle.sent, [("h1", "/compact", True)])

    def test_a_dry_run_neither_counts_the_series_NOR_ERASES_IT(self):  # noqa: VACUOUS_ASSERTION — the empty dry-run results are controlled by the real third refusal, which escalates on the same observable
        """--dry-run is what an operator types to LOOK, and looking must not
        change what the live timer sees — in EITHER direction.

        THE SECOND HALF IS THE ONE WITH TEETH, and a surviving mutation is what
        proved it. Inflation is already impossible on a dry run for an unrelated
        reason: check() skips _fire entirely when fire=False, so the row carries
        no actuation_state and there is nothing to count. Moving the tracker
        outside the `if fire:` guard therefore passed an inflation-only test —
        while doing real damage the other way, because a row with no
        actuation_state RE-ARMS the seat. An operator who ran --dry-run to check
        on a wedging seat would silently wipe its accumulated count and push the
        escalation back to zero, every time they looked."""
        self.plant(int(CODEX_WINDOW * 1.12))
        for _ in range(4):
            dry = autocompact.check(seats=["codex"], fire=False, post=False,
                                    adapter=FakeAdapter(tail=self.RUNNING))
            self.assertEqual(dry["wedged"], [])
        self.assertEqual(self.refuse(1.13)["wedged"], [],
                         "dry runs inflated the consecutive count")
        # two real refusals, an operator's look, then the third real refusal
        self.refuse(1.15)
        for _ in range(3):
            autocompact.check(seats=["codex"], fire=False, post=False,
                              adapter=FakeAdapter(tail=self.RUNNING))
        third = self.refuse(1.20)
        self.assertEqual([r["seat"] for r in third["wedged"]], ["codex"],
                         "looking at the seat reset its wedge counter")
        self.assertEqual(third["rows"][0]["refusal_wedge"]["n"], 3)

    def test_a_CORRUPT_state_file_fails_OPEN_and_never_wedges_the_watchdog(self):
        """House law: a guard that can wedge the fleet is worse than one that
        misses. pk.read_json already fails open on an UNREADABLE file; the other
        half is a READABLE file of the wrong shape — hand-edited, half-written,
        or written by another helm version. It must not raise, and the pass must
        keep working."""
        self.refuse(1.12)
        path = autocompact._state_path()
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        st["_refusals"] = "this is not a dict"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)
        res = self.refuse(1.15)                  # must not raise
        self.assertEqual(res["rows"][0]["actuation_state"], "RUNNING")
        self.assertEqual(res["wedged"], [], "a corrupt blob re-arms, not fires")
        # and the rung is alive again afterwards, not permanently disarmed
        self.refuse(1.16)
        self.assertEqual([r["seat"] for r in self.refuse(1.20)["wedged"]],
                         ["codex"])

    def test_a_garbage_ENTRY_for_one_seat_fails_open_too(self):  # noqa: VACUOUS_ASSERTION — the absent refusal_wedge is controlled unconditionally by the two refusals after it, which escalate on the same observable
        """The narrower shape: the container is fine, one seat's entry is not."""
        self.refuse(1.12)
        path = autocompact._state_path()
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        st["_refusals"]["codex"] = 42
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)
        res = self.refuse(1.15)                  # must not raise
        self.assertIsNone(res["rows"][0].get("refusal_wedge"))
        self.assertEqual(res["wedged"], [])
        # POSITIVE CONTROL on the same observable: failing open must mean the
        # counter RESTARTS, not that the rung is dead for the rest of the
        # process. A tracker that swallowed the bad entry and then never spoke
        # again would pass the two assertions above.
        self.refuse(1.16)
        self.assertEqual([r["seat"] for r in self.refuse(1.20)["wedged"]],
                         ["codex"])

    def test_low_context_from_another_session_cannot_discharge(self):  # noqa: VACUOUS_ASSERTION — the empty mismatch discharge/post arms are controlled by the same test's same-session fall, which must emit the opposite transition
        for pct in (1.12, 1.15, 1.20):
            self.refuse(pct)
        other = "22222222-2222-2222-2222-222222222222"
        self.plant(int(CODEX_WINDOW * 0.70), sid=other)
        with mock.patch("helm.chat.post") as post:
            mismatch = autocompact.check(
                seats=["codex"], post=True, adapter=FakeAdapter())
        alarm = mismatch["alarms"][0]["refusal_alarm"]
        self.assertEqual(mismatch["rows"][0]["status"], "session-mismatch")
        self.assertEqual(alarm["state"], "active")
        self.assertEqual(alarm["observation"], "identity-mismatch")
        self.assertEqual(mismatch["discharged"], [])
        post.assert_not_called()
        durable = pk.read_json(autocompact._state_path(), {})
        self.assertIn("codex", durable[autocompact._REFUSAL_ALARM_KEY])

        os.unlink(os.path.join(self.proj, other + ".jsonl"))
        self.plant(int(CODEX_WINDOW * 0.70), sid=SID)
        with mock.patch("helm.chat.post") as post:
            recovered = autocompact.check(
                seats=["codex"], post=True, adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in recovered["discharged"]],
                         ["codex"])
        self.assertIn("WATCHDOG DISCHARGED", post.call_args.args[0])

    def test_recovery_retires_a_failed_wedge_notice_before_discharge(self):  # noqa: VACUOUS_ASSERTION — retained wedge and delivered discharge positively control the final no-retry assertion on the same durable outbox
        for pct in (1.12, 1.15):
            self.refuse(pct)
        self.plant(int(CODEX_WINDOW * 1.20), sid=SID)
        with mock.patch("helm.chat.post", side_effect=RuntimeError("down")):
            alarmed = autocompact.check(
                seats=["codex"], post=True,
                adapter=FakeAdapter(tail=self.RUNNING))
        self.assertEqual([r["seat"] for r in alarmed["wedged"]], ["codex"])
        state = pk.read_json(autocompact._state_path(), {})
        pending = list(state[autocompact._REFUSAL_NOTICE_KEY].values())
        self.assertEqual([n["kind"] for n in pending], ["wedge"],
                         "the fixture did not retain a failed wedge notice")

        self.plant(int(CODEX_WINDOW * 0.70), sid=SID)
        calls = []

        def post_notice(text, **_kw):
            calls.append(text)
            if "WEDGED BEHIND" in text:
                raise RuntimeError("stale wedge must not retry")

        with mock.patch("helm.chat.post", side_effect=post_notice):
            recovered = autocompact.check(
                seats=["codex"], post=True, adapter=FakeAdapter())
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("WATCHDOG DISCHARGED", calls[0])
        self.assertEqual([r["seat"] for r in recovered["discharged"]],
                         ["codex"])
        self.assertEqual(pk.read_json(autocompact._state_path(), {})[
            autocompact._REFUSAL_NOTICE_KEY], {})

        with mock.patch("helm.chat.post") as post:
            autocompact.check(
                seats=["codex"], post=True, adapter=FakeAdapter())
        post.assert_not_called()

    def test_recovery_cannot_overtake_an_inflight_wedge_notice(self):  # noqa: VACUOUS_ASSERTION — the blocked wedge append positively controls recovery deferral, followed by the same episode's ordered discharge
        for pct in (1.12, 1.15):
            self.refuse(pct)
        self.plant(int(CODEX_WINDOW * 1.20), sid=SID)
        entered, release = threading.Event(), threading.Event()
        calls, errors = [], []

        def post_notice(text, **_kw):
            calls.append(text)
            if "WEDGED BEHIND" in text:
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test did not release wedge append")

        def post_wedge():
            try:
                autocompact.check(
                    seats=["codex"], post=True,
                    adapter=FakeAdapter(tail=self.RUNNING))
            except Exception as exc:
                errors.append(exc)

        with mock.patch("helm.chat.post", side_effect=post_notice):
            worker = threading.Thread(target=post_wedge)
            worker.start()
            self.assertTrue(entered.wait(1), "wedge append never entered")
            try:
                self.plant(int(CODEX_WINDOW * 0.70), sid=SID)
                overlap = autocompact.check(
                    seats=["codex"], post=True, adapter=FakeAdapter())
                self.assertEqual(overlap["discharged"], [])
                self.assertEqual(len(calls), 1, calls)
            finally:
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive(), "wedge append remained blocked")
            self.assertEqual(errors, [])

            recovered = autocompact.check(
                seats=["codex"], post=True, adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in recovered["discharged"]],
                         ["codex"])
        self.assertEqual(len(calls), 2, calls)
        self.assertIn("WEDGED BEHIND", calls[0])
        self.assertIn("WATCHDOG DISCHARGED", calls[1])

    def test_failed_discharge_post_keeps_a_retryable_notice(self):  # noqa: VACUOUS_ASSERTION — failed delivery's empty discharge is controlled by the same stable event succeeding once, disappearing durably, then not reposting
        for pct in (1.12, 1.15, 1.20):
            self.refuse(pct)
        self.plant(int(CODEX_WINDOW * 0.70), sid=SID)
        with mock.patch("helm.chat.post", side_effect=RuntimeError("down")):
            failed = autocompact.check(
                seats=["codex"], post=True, adapter=FakeAdapter())
        self.assertEqual(failed["discharged"], [])
        self.assertEqual(
            failed["alarms"][0]["refusal_alarm"]["state"],
            "discharge-pending")
        state = pk.read_json(autocompact._state_path(), {})
        notices = state[autocompact._REFUSAL_NOTICE_KEY]
        self.assertEqual(len(notices), 1)
        notice = next(iter(notices.values()))
        event_id = notice["id"]
        self.assertEqual(notice["payload"]["evidence"]["n"], 3)
        self.assertLess(notice["payload"]["observed_pct"], 80)

        def unlocked_post(_text, **_kw):
            with open(autocompact._state_lock_path(), "a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        with mock.patch.object(autocompact, "scan", return_value=[]), \
                mock.patch("helm.chat.post", side_effect=unlocked_post) as post:
            retried = autocompact.check(post=True, adapter=FakeAdapter())
        self.assertEqual(retried["rows"], [])
        self.assertEqual(post.call_count, 1)
        self.assertIn(event_id, post.call_args.args[0])
        self.assertEqual(pk.read_json(autocompact._state_path(), {})[
            autocompact._REFUSAL_NOTICE_KEY], {})
        with mock.patch.object(autocompact, "scan", return_value=[]), \
                mock.patch("helm.chat.post") as post:
            autocompact.check(post=True, adapter=FakeAdapter())
        post.assert_not_called()

    def test_crash_after_notice_append_retries_without_duplicate_chat_row(self):  # noqa: VACUOUS_ASSERTION — the first real post and retained outbox notice prove the retry path is populated before the one-row assertion
        from helm import chat
        for pct in (1.12, 1.15, 1.20):
            self.refuse(pct)
        self.plant(int(CODEX_WINDOW * 0.70), sid=SID)
        with mock.patch.object(chat, "_default_post_room", return_value="main"), \
                mock.patch.object(autocompact, "_ack_refusal_notice",
                                  return_value=False):
            autocompact.check(
                seats=["codex"], post=True, adapter=FakeAdapter())
        rows, _ = chat.read("main")
        self.assertEqual(len(rows), 1, "the first append did not land")
        state = pk.read_json(autocompact._state_path(), {})
        notice = next(iter(state[autocompact._REFUSAL_NOTICE_KEY].values()))
        notice["claim_until"] = 0.0
        pk.write_json(autocompact._state_path(), state)

        with mock.patch.object(chat, "_default_post_room", return_value="main"), \
                mock.patch.object(autocompact, "scan", return_value=[]):
            autocompact.check(post=True, adapter=FakeAdapter())
        rows, _ = chat.read("main")
        self.assertEqual(len(rows), 1,
                         "a lost outbox ack appended the notice twice")
        self.assertEqual(pk.read_json(autocompact._state_path(), {})[
            autocompact._REFUSAL_NOTICE_KEY], {})

    def test_unacked_notice_restore_keeps_event_identity(self):  # noqa: VACUOUS_ASSERTION — the original append, durable flush, and restored-row marker positively prove both sides of the no-duplicate transition
        from helm import chat
        for pct in (1.12, 1.15, 1.20):
            self.refuse(pct)
        self.plant(int(CODEX_WINDOW * 0.70), sid=SID)
        with mock.patch.object(chat, "_default_post_room", return_value="main"), \
                mock.patch.object(autocompact, "_ack_refusal_notice",
                                  return_value=False):
            autocompact.check(
                seats=["codex"], post=True, adapter=FakeAdapter())
        rows, _ = chat.read("main")
        self.assertEqual(len(rows), 1)
        event_row_id = rows[0]["id"]
        self.assertEqual(chat.log_flush(["main"]), 1)

        shutil.rmtree(os.environ["HELM_CHAT_DIR"])
        state = pk.read_json(autocompact._state_path(), {})
        notice = next(iter(state[autocompact._REFUSAL_NOTICE_KEY].values()))
        notice["claim_until"] = 0.0
        pk.write_json(autocompact._state_path(), state)
        with mock.patch.object(chat, "_default_post_room", return_value="main"), \
                mock.patch.object(autocompact, "scan", return_value=[]):
            autocompact.check(post=True, adapter=FakeAdapter())

        rows, _ = chat.read("main")
        logical = [row for row in rows if row.get("id") == event_row_id]
        self.assertEqual(len(logical), 1,
                         "journal restore and retry duplicated one notice")
        self.assertTrue(logical[0].get("restored"),
                        "the retry appended instead of reusing restoration")
        self.assertEqual(pk.read_json(autocompact._state_path(), {})[
            autocompact._REFUSAL_NOTICE_KEY], {})

    def test_unacked_notice_rotation_before_flush_survives_ram_loss(self):  # noqa: VACUOUS_ASSERTION — the populated notice, proven rotation, absent journal, removed RAM tree, and expired lease control the no-repost assertion
        from helm import chat
        for pct in (1.12, 1.15, 1.20):
            self.refuse(pct)
        self.plant(int(CODEX_WINDOW * 0.70), sid=SID)
        with mock.patch.object(chat, "_default_post_room", return_value="main"), \
                mock.patch.object(autocompact, "_ack_refusal_notice",
                                  return_value=False):
            autocompact.check(
                seats=["codex"], post=True, adapter=FakeAdapter())
        rows, _ = chat.read("main")
        self.assertEqual(len(rows), 1, "the first append did not land")
        event_row_id = rows[0]["id"]
        receipt_path = chat._event_receipt_path("main")
        self.assertTrue(os.path.exists(receipt_path),
                        "the first append did not record its event receipt")

        with mock.patch.object(chat, "SIZE_CAP", 400):
            for i in range(20):
                chat.post("ordinary-%02d" % i, room="main", who="other",
                          sign=False)
        rows, _ = chat.read("main")
        self.assertNotIn(event_row_id, {row.get("id") for row in rows},
                         "fixture did not rotate the event row before flush")
        self.assertFalse(os.path.exists(chat._flush_state_path()),
                         "fixture unexpectedly ran the periodic log flush")
        self.assertEqual(chat._journal_records()[1], "absent")

        ram_dir = os.environ["HELM_CHAT_DIR"]
        shutil.rmtree(ram_dir)
        self.assertFalse(os.path.exists(ram_dir),
                         "fixture did not remove the RAM room and receipts")
        self.assertEqual(chat.restore_journal(apply=True)["state"], "absent")

        state = pk.read_json(autocompact._state_path(), {})
        notice = next(iter(state[autocompact._REFUSAL_NOTICE_KEY].values()))
        notice["claim_until"] = 0.0
        pk.write_json(autocompact._state_path(), state)
        with mock.patch.object(chat, "_default_post_room", return_value="main"), \
                mock.patch.object(autocompact, "scan", return_value=[]):
            autocompact.check(post=True, adapter=FakeAdapter())

        self.assertEqual(chat.read("main"), ([], 0),
                         "the expired-lease retry reposted the rotated event")
        self.assertTrue(os.path.exists(receipt_path),
                        "the durable event proof died with the RAM room")
        self.assertNotEqual(
            os.path.commonpath([os.path.realpath(receipt_path),
                                os.path.realpath(ram_dir)]),
            os.path.realpath(ram_dir),
            "the event receipt is still owned by volatile chat storage")
        self.assertEqual(pk.read_json(autocompact._state_path(), {})[
            autocompact._REFUSAL_NOTICE_KEY], {})

    def test_expired_claim_concurrent_retry_appends_one_notice_row(self):  # noqa: VACUOUS_ASSERTION — two proven post calls plus the first append event positively control the final one-row concurrency assertion
        from helm import chat
        for pct in (1.12, 1.15, 1.20):
            self.refuse(pct)
        self.plant(int(CODEX_WINDOW * 0.70), sid=SID)
        with mock.patch("helm.chat.post", side_effect=RuntimeError("down")):
            autocompact.check(
                seats=["codex"], post=True, adapter=FakeAdapter())

        real_post = chat.post
        appended, release = threading.Event(), threading.Event()
        call_lock, calls, errors = threading.Lock(), [], []

        def slow_first(*args, **kwargs):
            with call_lock:
                calls.append(1)
                first = len(calls) == 1
            row = real_post(*args, **kwargs)
            if first:
                appended.set()
                release.wait(2)
            return row

        def run():
            try:
                autocompact.check(post=True, adapter=FakeAdapter())
            except Exception as exc:  # test thread must report, never vanish
                errors.append(exc)

        with mock.patch.object(chat, "_default_post_room", return_value="main"), \
                mock.patch.object(autocompact, "scan", return_value=[]), \
                mock.patch("helm.chat.post", side_effect=slow_first):
            first = threading.Thread(target=run)
            first.start()
            self.assertTrue(appended.wait(1), "first claim never appended")
            with open(autocompact._state_lock_path(), "a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                state = pk.read_json(autocompact._state_path(), {})
                notice = next(iter(
                    state[autocompact._REFUSAL_NOTICE_KEY].values()))
                notice["claim_until"] = 0.0
                pk.write_json(autocompact._state_path(), state)
            retry = threading.Thread(target=run)
            retry.start()
            retry.join(0.1)
            self.assertTrue(retry.is_alive(),
                            "expired retry overtook an in-flight delivery")
            release.set()
            first.join(2)
            retry.join(2)
            self.assertFalse(first.is_alive(), "first delivery remained blocked")
            self.assertFalse(retry.is_alive(), "serialized retry remained blocked")
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 2, "the expired claim was not retried")
        rows, _ = chat.read("main")
        self.assertEqual(len(rows), 1,
                         "concurrent claimants appended duplicate notices")
        self.assertEqual(pk.read_json(autocompact._state_path(), {})[
            autocompact._REFUSAL_NOTICE_KEY], {})

    def test_wedge_notice_ignores_a_malformed_discharge_observation(self):  # noqa: VACUOUS_ASSERTION — the emitted wedge row proves the malformed notice survived normalization before its successful ack empties the outbox
        notice_id = "wedge-with-irrelevant-observation"
        episode = "session:" + SID
        evidence = {
            "v": 1, "episode": episode, "n": 3,
            "first_pct": 112.0, "last_pct": 120.0,
            "state": "RUNNING", "since": time.time() - 120,
            "alerted_pct": None,
        }
        pk.write_json(autocompact._state_path(), {
            autocompact._REFUSAL_NOTICE_KEY: {
                notice_id: {
                    "v": 1, "id": notice_id, "kind": "wedge",
                    "seat": "codex", "episode": episode,
                    "payload": {
                        "threshold": 80.0, "evidence": evidence,
                        "observed_pct": "not-a-number",
                        "window": CODEX_WINDOW,
                        "actuation_reason": "open turn",
                    },
                    "claim_token": None, "claim_until": 0.0,
                },
            },
        })
        with mock.patch.object(autocompact, "scan", return_value=[]), \
                mock.patch("helm.chat.post") as post:
            res = autocompact.check(post=True, adapter=FakeAdapter())
        self.assertEqual(res["rows"], [])
        self.assertEqual(post.call_count, 1)
        self.assertIn("WEDGED BEHIND", post.call_args.args[0])
        self.assertEqual(pk.read_json(autocompact._state_path(), {})[
            autocompact._REFUSAL_NOTICE_KEY], {})

    def test_malformed_alarm_evidence_fails_open_without_formatting(self):  # noqa: VACUOUS_ASSERTION — non-empty malformed alarm/notice controls are durably rewritten to empty normalized owners, while subsequent refusal tests prove the detector remains live
        path = autocompact._state_path()
        pk.write_json(path, {
            autocompact._REFUSAL_ALARM_KEY: {
                "codex": {"evidence": {"n": "3", "first_pct": [],
                                         "last_pct": float("nan")},
                          "threshold": "80"}},
            autocompact._REFUSAL_NOTICE_KEY: {"bad": ["not", "a", "notice"]},
        })
        self.plant(int(CODEX_WINDOW * 0.95), age_s=8 * 3600)
        res = autocompact.check(
            seats=["codex"], post=False, adapter=FakeAdapter())
        self.assertEqual(res["alarms"], [])
        self.assertIsNone(autocompact._refusal_alarm_line(res["rows"][0]))
        repaired = pk.read_json(path, {})
        self.assertEqual(repaired[autocompact._REFUSAL_ALARM_KEY], {})
        self.assertEqual(repaired[autocompact._REFUSAL_NOTICE_KEY], {})

    def test_dry_run_projects_alarm_without_changing_state_bytes(self):
        for pct in (1.12, 1.15, 1.20):
            self.refuse(pct)
        path = autocompact._state_path()
        with open(path, "rb") as f:
            before = f.read()
        stamp = os.stat(path).st_mtime_ns
        dry = autocompact.check(
            seats=["codex"], fire=False, post=False, adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in dry["alarms"]], ["codex"])
        self.assertIn("REFUSAL ALARM [ACTIVE]",
                      autocompact._refusal_alarm_line(dry["alarms"][0]))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.stat(path).st_mtime_ns, stamp)

    def test_quiet_active_alarm_suppresses_lower_severity_surfaces(self):
        for pct in (1.12, 1.15, 1.20):
            self.refuse(pct)
        self.plant(int(CODEX_WINDOW * 0.95), age_s=8 * 3600)
        quiet = autocompact.check(
            seats=["codex"], post=False, adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in quiet["alarms"]], ["codex"])
        self.assertEqual(quiet["hot"], [])
        self.assertEqual(quiet["dead"], [])
        self.assertEqual(quiet["silent"], [])
        self.assertNotIn("codex", pk.read_json(
            autocompact._state_path(), {}).get("_hot", {}))

    def test_missing_scan_row_still_projects_durable_alarm(self):
        for pct in (1.12, 1.15, 1.20):
            self.refuse(pct)
        with mock.patch.object(autocompact, "scan", return_value=[]):
            missing = autocompact.check(fire=False, post=False)
        self.assertEqual(missing["rows"], [])
        self.assertEqual([r["seat"] for r in missing["alarms"]], ["codex"])
        alarm = missing["alarms"][0]["refusal_alarm"]
        self.assertEqual(alarm["observation"], "missing")
        self.assertIsNone(alarm["observed_pct"])
        with mock.patch.object(autocompact, "check", return_value=missing):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(autocompact.cmd_autocompact(["--dry-run"]), 0)
        self.assertIn("durable refusal state", buf.getvalue())
        self.assertNotIn("no proxy seats minted", buf.getvalue())

    def test_parent_mature_refusal_rearms_without_false_discharge_or_blackout(self):  # noqa: VACUOUS_ASSERTION — the same test ends with three fresh identified refusals producing one bound wedge, proving retirement re-arms rather than disabling the detector
        path = autocompact._state_path()
        legacy = {"n": 21, "first_pct": 112.0, "last_pct": 120.0,
                  "state": "RUNNING", "since": time.time() - 1200,
                  "alerted_pct": 120.0}
        pk.write_json(path, {autocompact._REFUSAL_KEY: {"codex": legacy}})
        self.plant(int(CODEX_WINDOW * 0.70), sid=SID)
        with open(path, "rb") as f:
            before = f.read()
        dry = autocompact.check(
            seats=["codex"], fire=False, post=False, adapter=FakeAdapter())
        self.assertEqual(dry["alarms"], [])
        self.assertEqual(dry["discharged"], [])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)

        live = autocompact.check(
            seats=["codex"], post=False, adapter=FakeAdapter())
        self.assertEqual(live["alarms"], [])
        self.assertEqual(live["discharged"], [])
        state = pk.read_json(path, {})
        self.assertNotIn("codex", state[autocompact._REFUSAL_ALARM_KEY])
        self.assertNotIn("codex", state[autocompact._REFUSAL_KEY])

        first, second, third = (self.refuse(pct)
                                for pct in (1.12, 1.15, 1.20))
        self.assertEqual(first["wedged"], [])
        self.assertEqual(second["wedged"], [])
        self.assertEqual([r["seat"] for r in third["wedged"]], ["codex"])
        alarm = pk.read_json(path, {})[autocompact._REFUSAL_ALARM_KEY]["codex"]
        self.assertEqual(alarm["episode"], "session:%s" % SID)

    def test_unbound_legacy_alarm_binds_only_to_a_current_high_episode(self):
        path = autocompact._state_path()
        evidence = {"v": 1, "episode": None, "n": 21,
                    "first_pct": 112.0, "last_pct": 120.0,
                    "state": "RUNNING", "since": time.time() - 1200,
                    "alerted_pct": 120.0}
        pk.write_json(path, {autocompact._REFUSAL_ALARM_KEY: {
            "codex": {"v": 1, "episode": None, "threshold": 80.0,
                      "evidence": evidence, "alerted_pct": 120.0}}})

        rebound = self.refuse(1.21)
        self.assertEqual([r["seat"] for r in rebound["alarms"]], ["codex"])
        alarm = pk.read_json(path, {})[autocompact._REFUSAL_ALARM_KEY]["codex"]
        self.assertEqual(alarm["episode"], "session:%s" % SID)
        self.assertEqual(alarm["threshold"], 80.0)
        self.assertGreaterEqual(alarm["evidence"]["n"], 21)

        self.plant(int(CODEX_WINDOW * 0.70), sid=SID)
        recovered = autocompact.check(
            seats=["codex"], post=False, adapter=FakeAdapter())
        self.assertEqual([r["seat"] for r in recovered["discharged"]],
                         ["codex"])
        self.assertEqual(recovered["alarms"], [])

    def test_bound_series_owns_threshold_and_alert_state_over_unbound_alarm(self):
        path = autocompact._state_path()
        bound = {"v": 1, "episode": "session:%s" % SID, "n": 3,
                 "first_pct": 82.0, "last_pct": 84.0,
                 "state": "RUNNING", "since": time.time() - 120,
                 "alerted_pct": None}
        unbound = dict(bound, episode=None, n=21, last_pct=120.0,
                       alerted_pct=120.0)
        pk.write_json(path, {
            autocompact._REFUSAL_KEY: {"codex": bound},
            autocompact._REFUSAL_ALARM_KEY: {
                "codex": {"v": 1, "episode": None, "threshold": 95.0,
                          "evidence": unbound, "alerted_pct": 120.0}},
        })
        result = self.refuse(0.84)
        self.assertEqual(result["discharged"], [])
        self.assertEqual([r["seat"] for r in result["wedged"]], ["codex"])
        alarm = pk.read_json(path, {})[autocompact._REFUSAL_ALARM_KEY]["codex"]
        self.assertEqual(alarm["episode"], "session:%s" % SID)
        self.assertEqual(alarm["threshold"], 80.0)
        self.assertEqual(alarm["alerted_pct"], 84.0)
        self.assertEqual(alarm["evidence"]["first_pct"], 82.0)

    def test_immature_bound_series_survives_coexisting_unbound_alarm(self):
        path = autocompact._state_path()
        bound = {"v": 1, "episode": "session:%s" % SID, "n": 2,
                 "first_pct": 82.0, "last_pct": 83.0,
                 "state": "RUNNING", "since": time.time() - 60,
                 "alerted_pct": None}
        unbound = dict(bound, episode=None, n=21, last_pct=120.0,
                       alerted_pct=120.0)
        pk.write_json(path, {
            autocompact._REFUSAL_KEY: {"codex": bound},
            autocompact._REFUSAL_ALARM_KEY: {
                "codex": {"v": 1, "episode": None, "threshold": 95.0,
                          "evidence": unbound, "alerted_pct": 120.0}},
        })
        result = self.refuse(0.84)
        self.assertEqual([r["seat"] for r in result["wedged"]], ["codex"])
        alarm = pk.read_json(path, {})[autocompact._REFUSAL_ALARM_KEY]["codex"]
        self.assertEqual(alarm["episode"], "session:%s" % SID)
        self.assertEqual(alarm["threshold"], 80.0)
        self.assertEqual(alarm["evidence"]["n"], 3)

    def test_missing_row_retires_unbound_alarm_instead_of_suppressing_forever(self):  # noqa: VACUOUS_ASSERTION — the non-empty persisted alarm is asserted absent after one live pass, while the high-row control above proves the same evidence binds when identity is available
        path = autocompact._state_path()
        evidence = {"v": 1, "episode": None, "n": 8,
                    "first_pct": 90.0, "last_pct": 120.0,
                    "state": "RUNNING", "since": time.time() - 600,
                    "alerted_pct": 120.0}
        pk.write_json(path, {autocompact._REFUSAL_ALARM_KEY: {
            "codex": {"v": 1, "episode": None, "threshold": 80.0,
                      "evidence": evidence, "alerted_pct": 120.0}}})
        with mock.patch.object(autocompact, "scan", return_value=[]):
            result = autocompact.check(post=False, adapter=FakeAdapter())
        self.assertEqual(result["alarms"], [])
        state = pk.read_json(path, {})
        self.assertNotIn("codex", state[autocompact._REFUSAL_ALARM_KEY])
        self.assertNotIn("codex", state[autocompact._REFUSAL_KEY])

    def test_unbound_legacy_notice_is_retired_without_chat_delivery(self):  # noqa: VACUOUS_ASSERTION — the non-empty input notice is valid in every field except identity, while bound outbox controls above prove the same delivery path posts and clears valid notices
        path = autocompact._state_path()
        notice_id = "legacy-unbound-wedge"
        evidence = {"v": 1, "episode": None, "n": 3,
                    "first_pct": 112.0, "last_pct": 120.0,
                    "state": "RUNNING", "since": time.time() - 120,
                    "alerted_pct": None}
        pk.write_json(path, {autocompact._REFUSAL_NOTICE_KEY: {
            notice_id: {"v": 1, "id": notice_id, "kind": "wedge",
                        "seat": "codex", "episode": None,
                        "payload": {"threshold": 80.0,
                                    "evidence": evidence,
                                    "observed_pct": None,
                                    "window": CODEX_WINDOW,
                                    "actuation_reason": "open turn"},
                        "claim_token": None, "claim_until": 0.0}}})
        with mock.patch.object(autocompact, "scan", return_value=[]), \
                mock.patch("helm.chat.post") as post:
            result = autocompact.check(post=True, adapter=FakeAdapter())
        self.assertEqual(result["alarms"], [])
        self.assertEqual(post.call_count, 0)
        self.assertEqual(pk.read_json(path, {})[
            autocompact._REFUSAL_NOTICE_KEY], {})


FIXTURE_TAIL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "codex3-100pct-tail.txt")


def _fixture_tail(composer="❯"):
    """The VERBATIM codex-3 pane tail (saved live 2026-08-04): 100%-context
    banner, task-list chrome, a live background-subagent row — and a bare
    empty composer, swappable per case for the draft/pending variants."""
    with open(FIXTURE_TAIL, encoding="utf-8") as f:
        tail = f.read()
    return tail.replace("\n❯\n", "\n%s\n" % composer)


class Codex3HundredPercentPaneTest(AutocompactTest):
    """The 100%-context pane shape that blinded the watchdog at exactly its
    target state (measured live 2026-08-04, owner-escalated). The composer was
    VERIFIED EMPTY (bare ❯, pane idle at the wall), yet _fire refused "composer
    contains unsent input": the agents-strip footer below the composer
    (`● main`, `◯ claude  <desc>   12m 48s`) was not recognized as chrome, so
    _current_prompt_line reported newer semantic content. The corroborating
    store premise counted 89 refusals vs THREE actuations in one night."""

    def test_the_live_fixture_empty_composer_classifies_injectable(self):
        tail = _fixture_tail()
        # MUST-HIT seeds: the swap really found the bare composer, and the
        # footer rows that caused the blindness are really in the fixture.
        self.assertIn("\n❯\n", tail, "fixture lost its bare composer line")
        self.assertIn("● main", tail)
        self.assertIn("◯ claude", tail)
        self.assertEqual(seat._current_prompt_line(tail), "❯")
        self.assertEqual(seat._classify_pane_tail(tail)[0], "CONTEXT_FULL")
        self.plant(int(CODEX_WINDOW * 1.00))
        ad = FakeAdapter(tail=tail)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_a_real_draft_behind_the_banner_still_refuses(self):  # noqa: VACUOUS_ASSERTION — exact UNKNOWN refusal state + unsent-input reason are positive controls on the same pass
        tail = _fixture_tail("❯ keep this draft")
        self.assertEqual(seat._current_prompt_line(tail), "❯ keep this draft")
        self.plant(int(CODEX_WINDOW * 1.00))
        ad = FakeAdapter(tail=tail)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [])
        row = res["rows"][0]
        self.assertEqual(row["actuation_state"], "UNKNOWN")
        self.assertIn("composer contains unsent input",
                      row["actuation_reason"])

    def test_an_exact_pending_compact_behind_the_banner_submits_enter(self):
        tail = _fixture_tail("❯ /compact")
        self.plant(int(CODEX_WINDOW * 1.00))
        ad = FakeAdapter(tail=tail)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "submitted")
        self.assertEqual(ad.sent, [("h1", "", True)])

    def test_the_live_subagent_row_is_not_the_panes_own_open_turn(self):
        """Owner canon 2026-08-04: "compaction can definitely happen while a
        sub agent is running and in fact it's the best time". RUNNING means the
        pane's OWN open turn (`esc to interrupt`, rendered only mid-turn); the
        task footer's live agent row and ctrl+t chrome carry no such claim."""
        tail = _fixture_tail()
        self.assertIn("◯ claude", tail)          # the live subagent row IS here
        self.assertIn("ctrl+t to hide tasks", tail)
        self.assertNotIn("esc to interrupt", tail)
        self.assertEqual(seat._classify_pane_tail(tail)[0], "CONTEXT_FULL",
                         "the subagent row must never read as RUNNING")

    def test_transcript_bullets_are_never_footer_chrome(self):  # noqa: VACUOUS_ASSERTION — the fixture test positively matches the SAME _PANE_CHROME on the real strip rows
        """The REVERSE defect, pinned shut: over-matching the agents strip
        would let a historical ❯ impersonate the live composer. Single-spaced
        prose bullets stay semantic content."""
        for line in ("● API Error: 400 Your input exceeds the context window",
                     '● Monitor event: "codex-3 Helm inbox"',
                     "✻ Brewed for 21m 19s · 1 monitor still running"):
            with self.subTest(line=line):
                self.assertIsNone(seat._PANE_CHROME.match(line))
                self.assertIsNone(
                    seat._current_prompt_line("❯ old submitted\n%s" % line))


class RefusalTextSurfaceTest(unittest.TestCase):
    """DEFECT (measured 2026-08-04): _fire's precise refusal tuple reached the
    row as actuation_state/actuation_reason — visible in --json — while the
    TEXT path printed only `-> would fire`. The operator watched the threshold
    cross with no line saying why nothing happened. The row-line format stays
    stable for parsers; the refusal is an appended continuation line."""

    def _res(self, **over):
        row = {"seat": "codex", "family": "codex", "status": "ok",
               "ctx_tokens": 400000, "pct": 100.0, "window": 400000,
               "window_src": "FAMILIES.max_context", "source": "transcript",
               "headroom_tokens": 0, "age_s": 60.0, "would_fire": True,
               "actuation_state": "RUNNING",
               "actuation_reason": "pane h1 has an open turn (esc to "
                                   "interrupt); /compact was not queued "
                                   "mid-tool-call"}
        row.update(over)
        return {"rows": [row], "fired": [], "hot": [], "dead": [],
                "silent": [], "wedged": [], "alarms": [], "discharged": []}

    def _run(self, argv, res):
        with mock.patch.object(autocompact, "check", return_value=res):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = autocompact.cmd_autocompact(argv)
        return rc, buf.getvalue()

    def test_text_surface_prints_the_refusal_class_and_reason(self):
        rc, out = self._run(["--once", "--quiet"], self._res())
        self.assertEqual(rc, 0)
        self.assertIn("-> would fire", out)      # row-line format untouched
        self.assertIn("REFUSED [RUNNING]", out)
        self.assertIn("open turn", out)

    def test_json_surface_is_unchanged_by_the_refusal_line(self):  # noqa: VACUOUS_ASSERTION — json.loads(out) == res is the unconditional positive control on the same output
        res = self._res()
        rc, out = self._run(["--once", "--quiet", "--json"], res)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), res)
        self.assertNotIn("REFUSED [", out)

    def test_a_fired_or_unrefused_row_prints_no_refusal_line(self):  # noqa: VACUOUS_ASSERTION — the render test prints REFUSED through the same _run on the same observable
        fired = self._res(actuation_state=None, actuation_reason=None,
                          mode="injected", detail="d")
        fired["fired"] = list(fired["rows"])
        for label, res in (("fired", fired),
                           ("dry-run would-fire",
                            self._res(actuation_state=None,
                                      actuation_reason=None))):
            with self.subTest(label=label):
                rc, out = self._run(["--once", "--quiet"], res)
                self.assertEqual(rc, 0)
                self.assertNotIn("REFUSED", out)

    def test_a_reasonless_refusal_still_renders_honestly(self):
        rc, out = self._run(["--once", "--quiet"],
                            self._res(actuation_reason=None))
        self.assertEqual(rc, 0)
        self.assertIn("REFUSED [RUNNING] no actionable pane evidence", out)

    def test_session_mismatch_alarm_and_discharge_render_as_opposites(self):
        evidence = {"n": 3, "first_pct": 82.0, "last_pct": 91.0,
                    "state": "UNKNOWN"}
        active = self._res(
            status="session-mismatch", actuation_state="UNKNOWN",
            actuation_reason="registered session differs from transcript",
            refusal_alarm={"state": "active", "observed_pct": 95.0,
                           "threshold": 80, "evidence": evidence})
        active["alarms"] = list(active["rows"])
        rc, out = self._run(["--once", "--quiet"], active)
        self.assertEqual(rc, 0)
        self.assertIn("session-mismatch", out)
        self.assertIn("REFUSED [UNKNOWN]", out)
        self.assertIn("REFUSAL ALARM [ACTIVE]", out)
        self.assertNotIn("DISCHARGED", out)

        discharged = self._res(
            pct=70.0, actuation_state=None, actuation_reason=None,
            refusal_alarm={"state": "discharged", "observed_pct": 70.0,
                           "threshold": 80, "evidence": evidence})
        discharged["discharged"] = list(discharged["rows"])
        rc, out = self._run(["--once", "--quiet"], discharged)
        self.assertEqual(rc, 0)
        self.assertIn("DISCHARGED refusal alarm", out)
        self.assertIn("70.0% below 80%", out)
        self.assertNotIn("REFUSED [UNKNOWN]", out)
        self.assertNotIn("REFUSAL ALARM [ACTIVE]", out)


