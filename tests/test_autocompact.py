"""Hermetic tests for helm.autocompact — the proxy-seat /compact watchdog.
HELM_HOME points at a tmp dir; planted transcripts/proxy.logs drive the read;
a fake adapter records injections. No chat posts (post=False), no real panes."""
import contextlib
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

from helm import autocompact, seat

SID = "11111111-1111-1111-1111-111111111111"
_ENV = ("HELM_AUTOCOMPACT_THRESHOLD", "HELM_AUTOCOMPACT_ASSUME_WINDOW",
        "HELM_AUTOCOMPACT_FRESH_S", "HELM_AUTOCOMPACT_LATCH_TTL")


class FakeAdapter:
    name = "fake"

    def __init__(self, panes=({"handle": "h1", "title": "dynamic summary",
                               "status": "connected"},), tail=""):
        self.panes, self.sent, self.tail = list(panes), [], tail

    def list(self):
        return self.panes

    def read(self, handle, limit=3000):
        return self.tail

    def send(self, handle, text, enter=True):
        self.sent.append((handle, text, enter))


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
        self._env = {k: os.environ.pop(k, None) for k in _ENV + ("HELM_HOME",)}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        self.d = seat.seat_dir("codex")
        self.proj = os.path.join(self.d, "claude", "projects", "-tmp-proj")
        os.makedirs(self.proj, exist_ok=True)
        self.spawn("h1")

    def tearDown(self):
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
                       "launch_sh": os.path.join(d, "launch.sh")}, f)

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
        self.plant(180000)                       # 50% of codex's 360k window
        row = autocompact.read("codex")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["source"], "transcript")
        self.assertEqual(row["ctx_tokens"], 180000)
        self.assertEqual(row["window"], 360000)
        self.assertAlmostEqual(row["pct"], 50.0)
        self.assertEqual(row["session"], SID)

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
        os.environ["HELM_AUTOCOMPACT_ASSUME_WINDOW"] = "0"
        os.makedirs(seat.seat_dir("kimi"), exist_ok=True)
        row = autocompact.read("kimi")
        self.assertEqual(row["status"], "window-unset")
        res = autocompact.check(seats=["kimi"], post=False,
                                adapter=FakeAdapter())
        self.assertEqual(res["fired"], [])

    def test_window_unset_mirrors_cc_default(self):
        # no FAMILIES max_context for kimi -> CC's assumed 200k is the gauge
        proj = os.path.join(seat.seat_dir("kimi"), "claude", "projects", "-p")
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, SID + ".jsonl"), "w") as f:
            f.write(usage_line(100000, model="kimi-k3") + "\n")
        row = autocompact.read("kimi")
        self.assertEqual(row["window"], 200000)
        self.assertEqual(row["window_src"], "cc-assumed-default")
        self.assertAlmostEqual(row["pct"], 50.0)

    # -- the trigger -------------------------------------------------------

    def test_no_fire_at_50(self):
        self.plant(180000)
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [])

    def test_repeated_400_overflow_waits_for_clear_then_reinjects_onboarding(self):
        self.plant(180000)  # overflow recovery is pane-tail driven, below 90%
        tail = ("API Error: 400 prompt is too long: context exceeds maximum\n"
                "API Error: 400 prompt is too long: context exceeds maximum")
        ad = FakeAdapter(tail=tail)
        first = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(first["fired"][0]["mode"], "clear-pending")
        self.assertEqual(ad.sent, [("h1", "/clear", True)])
        again = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(again["fired"], [])
        self.assertTrue(again["rows"][0].get("latched"))
        self.assertEqual(len(ad.sent), 1)       # never queues duplicate /clear

        # SessionStart is the completion evidence. It may bind the fresh session
        # before that session has persisted a transcript, so the register alone
        # must release the onboarding rebrief.
        with open(os.path.join(self.d, "spawn.json")) as f:
            rec = json.load(f)
        rec["session"] = "22222222-2222-2222-2222-222222222222"
        with open(os.path.join(self.d, "spawn.json"), "w") as f:
            json.dump(rec, f)
        os.unlink(os.path.join(self.proj, SID + ".jsonl"))
        ad.tail = ""
        third = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(third["fired"][0]["mode"], "cleared")
        self.assertEqual(len(ad.sent), 2)
        self.assertIn("helm chat wait --seat codex --follow", ad.sent[1][1])
        self.assertIn("home room main", ad.sent[1][1])
        with open(autocompact._state_path()) as f:
            self.assertNotIn("codex", json.load(f))

        sid = "22222222-2222-2222-2222-222222222222"
        self.plant(int(360000 * 0.97), sid=sid)
        fourth = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(fourth["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent[-1], ("h1", "/compact", True))

    def test_one_400_or_discussion_text_never_clears(self):
        self.plant(180000)
        for tail in (
                "API Error: 400 prompt is too long: context exceeds maximum",
                "discussion: API Error: 400 prompt is too long\n"
                "discussion: API Error: 400 prompt is too long"):
            with self.subTest(tail=tail):
                ad = FakeAdapter(tail=tail)
                res = autocompact.check(seats=["codex"], post=False, adapter=ad)
                self.assertEqual(res["fired"], [])
                self.assertEqual(ad.sent, [])

    def test_pending_clear_rebriefs_only_after_session_changes(self):
        self.plant(180000)
        tail = ("API Error: 400 prompt is too long: context exceeds maximum\n"
                "API Error: 400 prompt is too long: context exceeds maximum\n"
                "❯ /clear")
        ad = FakeAdapter(tail=tail)
        first = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(first["fired"][0]["mode"], "clear-pending")
        self.assertEqual(ad.sent, [])
        second = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(second["fired"], [])
        sid = "22222222-2222-2222-2222-222222222222"
        self.plant(1000, sid=sid)
        with open(os.path.join(self.d, "spawn.json")) as f:
            rec = json.load(f)
        rec["session"] = sid
        with open(os.path.join(self.d, "spawn.json"), "w") as f:
            json.dump(rec, f)
        ad.tail = ""
        third = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(third["fired"][0]["mode"], "cleared")
        self.assertEqual(len(ad.sent), 1)
        self.assertIn("helm chat wait --seat codex --follow", ad.sent[0][1])

    def test_fires_at_90_and_injects_compact(self):
        self.plant(int(360000 * 0.91))
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual([r["seat"] for r in res["fired"]], ["codex"])
        self.assertEqual(res["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_unsent_compact_blocks_duplicate_and_latches_until_drop(self):
        self.plant(int(360000 * 0.96))
        ad = FakeAdapter(tail="\x1b[36m❯ /compact\x1b[0m")
        first = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(first["fired"][0]["mode"], "pending")
        self.assertEqual(ad.sent, [])
        ad.tail = ""                 # command was submitted or composer cleared
        second = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(second["fired"], [])
        self.assertTrue(second["rows"][0].get("latched"))
        self.assertEqual(ad.sent, [])  # never double-inject while completion unknown
        self.plant(int(360000 * 0.60))
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.plant(int(360000 * 0.94))
        third = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(third["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_rendered_compact_text_is_not_composer_identity(self):
        self.plant(int(360000 * 0.96))
        for tail in ("assistant markdown:\n> /compact",
                     "❯ /compact\nassistant output continued"):
            with self.subTest(tail=tail):
                ad = FakeAdapter(tail=tail)
                res = autocompact.check(seats=["codex"], post=False, adapter=ad)
                self.assertEqual(res["fired"][0]["mode"], "injected")
                self.assertEqual(ad.sent, [("h1", "/compact", True)])
                os.unlink(autocompact._state_path())

    def test_latch_blocks_refire_then_rearms(self):
        self.plant(int(360000 * 0.91))
        ad = FakeAdapter()
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        res2 = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res2["fired"], [])          # mid-compaction: latched
        self.assertTrue(res2["rows"][0].get("latched"))
        self.assertEqual(len(ad.sent), 1)
        self.plant(50000)                            # compaction landed
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.plant(int(360000 * 0.95))               # next episode
        res4 = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(len(res4["fired"]), 1)
        self.assertEqual(len(ad.sent), 2)

    def test_injected_latch_never_ttl_retries_without_context_drop(self):
        os.environ["HELM_AUTOCOMPACT_LATCH_TTL"] = "1"
        self.plant(int(360000 * 0.92))
        ad = FakeAdapter()
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        time.sleep(1.1)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.assertTrue(res["rows"][0].get("latched"))
        self.assertEqual(len(ad.sent), 1)

    def test_any_observed_context_drop_completes_the_episode(self):
        self.plant(int(360000 * 0.98))
        ad = FakeAdapter()
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.plant(int(360000 * 0.82))   # dropped, but above the old 75% gate
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"], [])
        self.plant(int(360000 * 0.95))
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(len(res["fired"]), 1)
        self.assertEqual(len(ad.sent), 2)

    def test_session_change_rearms_even_while_context_stays_high(self):
        self.plant(int(360000 * 0.92))
        ad = FakeAdapter()
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        sid = "22222222-2222-2222-2222-222222222222"
        self.plant(int(360000 * 0.93), sid=sid)
        self.spawn("h1")
        with open(os.path.join(self.d, "spawn.json")) as f:
            rec = json.load(f)
        rec["session"] = sid
        with open(os.path.join(self.d, "spawn.json"), "w") as f:
            json.dump(rec, f)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(len(res["fired"]), 1)
        self.assertEqual(len(ad.sent), 2)

    def test_transcript_session_must_match_registered_pane_session(self):
        old = SID
        new = "22222222-2222-2222-2222-222222222222"
        self.plant(int(360000 * 0.97), sid=old)
        with open(os.path.join(self.d, "spawn.json")) as f:
            rec = json.load(f)
        rec["session"] = new
        with open(os.path.join(self.d, "spawn.json"), "w") as f:
            json.dump(rec, f)
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["rows"][0]["status"], "session-mismatch")
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [])

    def test_session_change_between_scan_and_send_never_actuates_new_pane(self):
        self.plant(int(360000 * 0.97))
        outer = self

        class ReplacingAdapter(FakeAdapter):
            def read(self, handle, limit=3000):
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
        self.assertEqual(res["fired"][0]["mode"], "manual")
        self.assertIn("session changed", res["fired"][0]["detail"])
        self.assertEqual(ad.sent, [])

    def test_missing_context_does_not_clear_injected_latch(self):
        p = self.plant(int(360000 * 0.94))
        ad = FakeAdapter()
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        os.unlink(p)
        gap = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(gap["rows"][0]["status"], "no-context-data")
        self.plant(int(360000 * 0.95))
        again = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(again["fired"], [])
        self.assertTrue(again["rows"][0].get("latched"))
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_claude_model_never_fires(self):
        self.plant(int(360000 * 0.95), model="claude-opus-4")
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["rows"][0]["status"], "claude-model")
        self.assertEqual(res["fired"], [])
        self.assertEqual(ad.sent, [])

    def test_stale_transcript_never_fires(self):
        self.plant(int(360000 * 0.95), age_s=8 * 3600)
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
        self.plant(int(360000 * 0.91))
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
        self.plant(int(360000 * 0.91))
        self.spawn("old", harness="orca")

        class OrcaAdapter(FakeAdapter):
            name = "orca"

            def resolve_pane(self, pane_key):
                return {"handle": "new", "pty_id": "pty-1"}

        ad = OrcaAdapter(panes=({
            "handle": "new", "title": "dynamic", "status": "connected",
            "writable": True, "pty_id": "pty-1",
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

    def test_registered_handle_ignores_dynamic_title(self):
        self.plant(int(360000 * 0.91))
        ad = FakeAdapter(panes=({"handle": "h1", "title": "changed by CC"},))
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

    def test_register_without_exact_seat_never_authorizes_handle(self):
        self.plant(int(360000 * 0.91))
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

    def test_unregistered_content_and_title_never_become_identity(self):
        # Launch text is copyable and titles are mutable; neither authorizes a
        # write to a live pane without the spawn register.
        os.unlink(os.path.join(self.d, "spawn.json"))
        self.plant(int(360000 * 0.91))
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
        self.assertEqual(ad.sent, [])

    def test_stale_handle_never_falls_back_to_matching_title(self):
        self.spawn("gone")
        self.plant(int(360000 * 0.91))
        ad = FakeAdapter(panes=({"handle": "wrong", "title": "codex",
                                 "preview": ""},))
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "manual")
        self.assertEqual(ad.sent, [])
        self.assertIn("registered handle gone is not live",
                      res["fired"][0]["detail"])

    def test_disconnected_registered_handle_is_not_live(self):
        self.plant(int(360000 * 0.91))
        ad = FakeAdapter(panes=({"handle": "h1", "title": "codex",
                                 "status": "disconnected"},))
        got, handle, detail = autocompact.resolve_pane("codex", ad)
        self.assertIs(got, ad)
        self.assertIsNone(handle)
        self.assertIn("disconnected", detail)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "manual")
        self.assertEqual(ad.sent, [])

    def test_headless_record_goes_manual_loud(self):
        self.spawn(None, harness="headless")
        self.plant(int(360000 * 0.91))
        res = autocompact.check(seats=["codex"], post=False,
                                adapter=FakeAdapter())
        self.assertEqual(res["fired"][0]["mode"], "manual")
        self.assertIn("headless", res["fired"][0]["detail"])

    def test_no_pane_goes_manual_loud(self):
        self.plant(int(360000 * 0.91))
        ad = FakeAdapter(panes=())
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "manual")
        self.assertEqual(ad.sent, [])

    def test_manual_path_posts_actionable_chat_alert(self):
        self.plant(int(360000 * 0.91))
        with mock.patch("helm.chat.post") as post:
            autocompact.check(seats=["codex"], adapter=FakeAdapter(panes=()))
        body = post.call_args.args[0]
        self.assertIn("needs /compact NOW", body)
        self.assertIn("paste it into the pane", body)
        self.assertEqual(post.call_args.kwargs["who"], "autocompact")

    def test_overlapping_checks_inject_once(self):
        self.plant(int(360000 * 0.91))
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
        self.plant(int(360000 * 0.65))
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(len(res["fired"]), 1)

    # -- surfaces ----------------------------------------------------------

    def test_report_lines_read_only(self):
        self.plant(180000)
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


if __name__ == "__main__":
    unittest.main()
