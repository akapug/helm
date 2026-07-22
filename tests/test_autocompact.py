"""Hermetic tests for helm.autocompact — the proxy-seat /compact watchdog.
HELM_HOME points at a tmp dir; planted transcripts/proxy.logs drive the read;
a fake adapter records injections. No chat posts (post=False), no real panes."""
import json
import os
import shutil
import tempfile
import time
import unittest

from helm import autocompact, seat

SID = "11111111-1111-1111-1111-111111111111"
_ENV = ("HELM_AUTOCOMPACT_THRESHOLD", "HELM_AUTOCOMPACT_ASSUME_WINDOW",
        "HELM_AUTOCOMPACT_FRESH_S", "HELM_AUTOCOMPACT_LATCH_TTL")


class FakeAdapter:
    name = "fake"

    def __init__(self, panes=({"handle": "h1", "title": "codex",
                               "status": "connected"},)):
        self.panes, self.sent = list(panes), []

    def list(self):
        return self.panes

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
        self.proj = os.path.join(seat.seat_dir("codex"), "claude", "projects",
                                 "-tmp-proj")
        os.makedirs(self.proj, exist_ok=True)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

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
            f.write('{"usage":{"input_tokens":300000,'
                    '"cache_read_input_tokens":30000,'
                    '"cache_creation_input_tokens":0}}\n')
        row = autocompact.read("codex")
        self.assertEqual(row["source"], "proxy.log")
        self.assertEqual(row["ctx_tokens"], 330000)
        self.assertEqual(row["status"], "ok")

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

    def test_fires_at_90_and_injects_compact(self):
        self.plant(int(360000 * 0.91))
        ad = FakeAdapter()
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual([r["seat"] for r in res["fired"]], ["codex"])
        self.assertEqual(res["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h1", "/compact", True)])

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

    def test_latch_ttl_retries_a_fire_that_never_landed(self):
        os.environ["HELM_AUTOCOMPACT_LATCH_TTL"] = "1"
        self.plant(int(360000 * 0.92))
        ad = FakeAdapter()
        autocompact.check(seats=["codex"], post=False, adapter=ad)
        time.sleep(1.1)
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(len(res["fired"]), 1)
        self.assertEqual(len(ad.sent), 2)

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

    def test_preview_fallback_finds_untitled_seat_pane(self):
        # manually-launched panes carry auto-summary titles; the launch line's
        # HELM_CHAT_NAME=<seat> in the visible tail identifies the pane. The
        # trailing space keeps 'codex' from matching a codex-2 pane.
        self.plant(int(360000 * 0.91))
        ad = FakeAdapter(panes=(
            {"handle": "h9", "title": "some auto summary",
             "preview": "... HELM_CHAT_NAME=codex-2 HELM_CELL_BIN=..."},
            {"handle": "h8", "title": "other summary",
             "preview": "... HELM_CHAT_NAME=codex HELM_CELL_BIN=..."}))
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "injected")
        self.assertEqual(ad.sent, [("h8", "/compact", True)])

    def test_no_pane_goes_manual_loud(self):
        self.plant(int(360000 * 0.91))
        ad = FakeAdapter(panes=())
        res = autocompact.check(seats=["codex"], post=False, adapter=ad)
        self.assertEqual(res["fired"][0]["mode"], "manual")
        self.assertEqual(ad.sent, [])

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


if __name__ == "__main__":
    unittest.main()
