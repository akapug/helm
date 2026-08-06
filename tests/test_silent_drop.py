"""Hermetic tests for helm.silent_drop — the empty-completion loud-fail.
HELM_HOME points at a tmp dir; planted transcripts drive the detector. No real
chat posts (post=False / quiet) and no pane injection ever (the rung is
read-only). The detector is validated against the drop-after-generate
signature proven on the live codex transcript (75/75 drops, 0 false
positives on a healthy seat)."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import seat, silent_drop

SID = "11111111-1111-1111-1111-111111111111"


def _rts(mins_ago=0):
    """A RECENT iso timestamp — drops model 'just happened', so they must fall
    inside silent_drop.RECENT_ALERT_WINDOW_S or the recency guard suppresses them."""
    import datetime
    t = (datetime.datetime.now(datetime.timezone.utc)
         - datetime.timedelta(minutes=mins_ago))
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def asst(content, stop="end_turn", ot=50, ts=None, sidechain=False):
    return json.dumps({
        "type": "assistant", "isSidechain": sidechain,
        "timestamp": ts if ts is not None else _rts(),
        "message": {"role": "assistant", "stop_reason": stop,
                    "content": content, "usage": {"output_tokens": ot}}})


def nudge():
    """claude-code's own 'no visible output' recovery nudge — the user-row it
    injects when a turn ends with no visible text. A REAL silent-drop is
    proven by this nudge following the thinking-only prefix row (the mislabel
    fix below): text that arrives WITHOUT a nudge is a normal continuation."""
    return json.dumps({
        "type": "user", "timestamp": _rts(),
        "message": {"role": "user", "content":
                    "[Your previous response had no visible output. Please "
                    "continue and produce a user-visible response.]"}})


class SilentDropTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ss-")
        self._home = os.environ.pop("HELM_HOME", None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        self.d = seat.seat_dir("codex")
        self.proj = os.path.join(self.d, "claude", "projects", "-tmp-proj")
        os.makedirs(self.proj, exist_ok=True)

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, lines, sid=SID):
        p = os.path.join(self.proj, sid + ".jsonl")
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        return p

    # -- the detector ------------------------------------------------------

    def test_drop_signature_detected(self):
        # a REAL drop: the thinking-only prefix row + CC's no-visible-output
        # nudge (the discriminator — an isolated candidate is not enough)
        self.plant([asst([], ot=103), nudge()])
        f = silent_drop.scan_seat("codex")
        self.assertIsNotNone(f)
        self.assertEqual(f["output_tokens"], 103)
        self.assertEqual(f["session"], SID)

    def test_empty_text_block_is_a_drop(self):
        # the proxy translates a drop into an empty text block + end_turn,
        # then CC nudges the silence
        self.plant([asst([{"type": "text", "text": ""}], ot=49), nudge()])
        self.assertIsNotNone(silent_drop.scan_seat("codex"))

    def test_empty_thinking_wrapper_is_a_drop(self):
        # the line464 form: empty thinking wrapper, then the nudge
        self.plant([asst([{"type": "thinking", "thinking": "",
                            "signature": "gAAAA"}], ot=57), nudge()])
        self.assertIsNotNone(silent_drop.scan_seat("codex"))

    def test_thinking_prefix_continuation_is_NOT_a_drop(self):
        # the mislabel: claude-code records a thinking-only
        # end_turn row, then the text as a CONTINUATION — a normal turn. 73 of
        # these on the founding transcript were false drops. The discriminator:
        # text arrives with NO nudge -> never fire.
        self.plant([asst([{"type": "thinking", "thinking": "",
                            "signature": "gAAAA"}], ot=103),
                    asst([{"type": "text", "text": "the real answer"}], ot=103)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_isolated_candidate_without_nudge_is_NOT_a_drop(self):
        # a thinking-only end_turn row with NO following text AND NO nudge is
        # an in-flight prefix (the text may still be coming) — firing here is
        # the mislabel. Only the nudge proves the silence was real.
        self.plant([asst([], ot=103)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    # -- non-drops (false-positive guards) ------------------------------

    def test_real_text_is_not_a_drop(self):
        self.plant([asst([{"type": "text", "text": "an answer"}], ot=50)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_tool_use_is_not_a_drop(self):
        self.plant([asst([{"type": "tool_use", "name": "Bash"}], ot=50)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_deliberate_thinking_park_is_not_a_drop(self):
        self.plant([asst([{"type": "thinking", "thinking": "no action"}],
                         ot=30)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_zero_output_tokens_is_not_a_drop(self):
        # a genuine refusal/classifier produces ~0 output — not our class
        self.plant([asst([], ot=0)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_tool_use_stop_reason_is_not_a_drop(self):
        self.plant([asst([], stop="tool_use", ot=50)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_sidechain_never_counts(self):
        self.plant([asst([], ot=99, sidechain=True)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_reports_the_newest_drop(self):
        old_ts, new_ts = _rts(10), _rts(2)
        self.plant([asst([], ot=10, ts=old_ts), nudge(),
                    asst([{"type": "text", "text": "healthy"}], ot=200),
                    asst([], ot=77, ts=new_ts), nudge()])
        f = silent_drop.scan_seat("codex")
        self.assertEqual(f["output_tokens"], 77)
        self.assertEqual(f["ts"], new_ts)

    # -- the latch ----------------------------------------------------------

    def test_latch_suppresses_repeat_same_ts(self):
        self.plant([asst([], ot=50, ts=_rts(1)), nudge()])
        r1 = silent_drop.check(seats=["codex"], post=False)
        self.assertEqual(len(r1["alerted"]), 1)
        r2 = silent_drop.check(seats=["codex"], post=False)
        self.assertEqual(len(r2["alerted"]), 0)
        self.assertTrue(r2["findings"][0]["latched"])

    def test_storm_suppresses_distinct_drops_per_seat(self):
        # attention-budget: a drop-STORM (distinct-ts drops within LATCH_TTL)
        # alerts ONCE per seat, not once per drop — a NEW distinct drop within
        # the window is latched, and its count rides the NEXT alert.
        self.plant([asst([], ot=50, ts=_rts(3)), nudge()])
        r1 = silent_drop.check(seats=["codex"], post=False)
        self.assertEqual(len(r1["alerted"]), 1)
        # a genuinely different drop (new ts) still in the window -> suppressed
        self.plant([asst([], ot=61, ts=_rts(2)), nudge()])
        r2 = silent_drop.check(seats=["codex"], post=False)
        self.assertEqual(len(r2["alerted"]), 0)
        self.assertTrue(r2["findings"][0]["latched"])

    def test_dry_run_is_genuinely_read_only(self):
        """A dry pass classifies would-alert against current state and
        writes NOTHING — no state file, no alerted_at, no latch."""
        self.plant([asst([], ot=50, ts=_rts(1)), nudge()])
        r = silent_drop.check(seats=["codex"], dry=True)
        self.assertEqual(len(r["alerted"]), 1)          # would alert
        self.assertFalse(r["findings"][0]["latched"])
        st = silent_drop._state_path()
        self.assertFalse(os.path.exists(st) and
                         json.load(open(st)).get("codex"),
                         "a dry run wrote the latch")

    def test_a_real_alert_still_fires_after_a_dry_run(self):
        """THE FILED BUG, from an independent QC pass: a --dry-run stamped
        alerted_at, so the next REAL drop inside LATCH_TTL_S was silently
        suppressed — a simulation consumed the alert budget and told no
        one. The pair below latched before the fix."""
        self.plant([asst([], ot=50, ts=_rts(2)), nudge()])
        silent_drop.check(seats=["codex"], dry=True)
        r = silent_drop.check(seats=["codex"], post=False)
        self.assertEqual(len(r["alerted"]), 1,
                         "the dry run consumed the real alert")
        self.assertFalse(r["findings"][0]["latched"])

    def test_dry_run_spends_no_suppressed_counter_on_a_latched_seat(self):
        """Dry passes over an already-latched seat must not inflate the
        storm count the NEXT real alert reports."""
        self.plant([asst([], ot=50, ts=_rts(3)), nudge()])
        silent_drop.check(seats=["codex"], post=False)   # real latch
        silent_drop.check(seats=["codex"], dry=True)
        silent_drop.check(seats=["codex"], dry=True)
        st = json.load(open(silent_drop._state_path()))
        self.assertEqual(st["codex"].get("suppressed", 0), 0,
                         "dry passes inflated the storm count")

    def test_cli_dry_run_flag_carries_the_read_only_contract(self):
        """The wiring half: cmd_silent_drop --dry-run must reach check()
        as dry=True — a correct check() behind a CLI that never passes the
        flag is the documented-exclusion-untested shape."""
        self.plant([asst([], ot=50, ts=_rts(1)), nudge()])
        with mock.patch("sys.stdout"):
            silent_drop.cmd_silent_drop(["--seat", "codex", "--dry-run"])
        r = silent_drop.check(seats=["codex"], post=False)
        self.assertEqual(len(r["alerted"]), 1,
                         "the CLI dry run consumed the real alert")

    def test_storm_count_rides_the_next_alert(self):
        # the suppressed count is carried into the alert text so ONE message
        # conveys the storm size instead of N wakes.
        f = {"seat": "codex", "output_tokens": 40, "session": SID,
             "ts": "2026-07-23T10:00:00Z", "suppressed_since_last": 7}
        self.assertIn("+7 more drops", silent_drop._alert_text(f))

    def test_alert_text_names_seat_tokens_and_coordinator(self):
        os.environ["HELM_COORDINATOR_SEAT"] = "coordinator"
        self.addCleanup(os.environ.pop, "HELM_COORDINATOR_SEAT", None)
        txt = silent_drop._alert_text(
            {"seat": "codex", "output_tokens": 103, "session": SID,
             "ts": "2026-07-23T10:00:00Z"})
        self.assertIn("@codex", txt)
        self.assertIn("@coordinator", txt)
        self.assertIn("103", txt)
        self.assertIn("SILENT-DROP", txt)
        del os.environ["HELM_COORDINATOR_SEAT"]
        bare = silent_drop._alert_text(
            {"seat": "codex", "output_tokens": 103, "session": SID,
             "ts": "2026-07-23T10:00:00Z"})
        self.assertNotIn("@coordinator", bare)   # unset = no phantom mention
        self.assertIn("@codex", bare)

    def test_alerts_post_to_helm_room_not_main(self):
        # owner ruling: silent-drop alerts in #main woke every #main-homed
        # seat on a row addressed to codex/integrator. They belong in #helm
        # (fleet ops). Pin the room so a refactor can't drop it back to the
        # default #main.
        self.plant([asst([], ot=50, ts=_rts()), nudge()])
        posts = []
        import helm.chat as _chat
        orig = _chat.post
        _chat.post = lambda text, **kw: posts.append((text, kw)) or {}
        try:
            silent_drop.check(seats=["codex"], post=True)
        finally:
            _chat.post = orig
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1].get("room"), "helm")



class UnscannableTest(unittest.TestCase):
    """"no drops detected" must never be printed over a seat nobody read.

    `scan_seat` returns None for three unrelated reasons — an unknown seat
    family, no transcript to read, and a genuinely quiet seat — and `scan`
    filtered all three into the same empty list. The watchdog then reported a
    clean bill. That is the cannot-look-is-not-a-fact law, which this module
    enforces on its own findings (a stale drop is not a finding) while
    violating it on its own coverage.

    MEASURED live: one seat's newest transcript was 141 HOURS old and the
    watchdog called it clean every ninety seconds, straight through the week
    the owner kept reporting that the seat was still dropping. Quiet and healthy
    render identically and mean opposite things.
    """

    def _seat(self, name, age_s=None):
        """A patched world where `name` is a proxy seat whose newest transcript
        is `age_s` old (None = no transcript at all)."""
        import os as _o, tempfile, time as _t
        d = tempfile.mkdtemp(prefix="helm-test-uns-")
        tp = None
        if age_s is not None:
            tp = _o.path.join(d, "t.jsonl")
            open(tp, "w").close()
            _o.utime(tp, (_t.time() - age_s, _t.time() - age_s))
        return d, tp

    def test_a_seat_with_NO_transcript_is_unscannable(self):
        d, _ = self._seat("codex-9")
        with mock.patch.object(silent_drop.autocompact, "proxy_seats",
                               return_value=["codex-9"]), \
                mock.patch.object(seat, "_seat_family",
                                  return_value=("codex", None)), \
                mock.patch.object(seat, "_instance_dir", return_value=d), \
                mock.patch.object(silent_drop.autocompact, "_newest_transcript",
                                  return_value=None):
            out = silent_drop.unscannable()
        self.assertEqual(len(out), 1)
        self.assertIn("cannot be seen", out[0][1])

    def test_a_seat_SILENT_LONGER_THAN_THE_WINDOW_is_unscannable(self):
        """The live case. A finding is only reported inside
        RECENT_ALERT_WINDOW_S, so a seat quieter than that can never produce
        one — counting it toward a clean bill counts a seat nobody asked."""
        d, tp = self._seat("codex-2", age_s=silent_drop.RECENT_ALERT_WINDOW_S + 3600)
        with mock.patch.object(silent_drop.autocompact, "proxy_seats",
                               return_value=["codex-2"]), \
                mock.patch.object(seat, "_seat_family",
                                  return_value=("codex", None)), \
                mock.patch.object(seat, "_instance_dir", return_value=d), \
                mock.patch.object(silent_drop.autocompact, "_newest_transcript",
                                  return_value=tp):
            out = silent_drop.unscannable()
        self.assertEqual(len(out), 1)
        self.assertIn("quiet, not clean", out[0][1])

    def test_a_LIVE_seat_is_NOT_reported_unscannable(self):
        """The negative control. If a working seat reported unscannable, the
        qualifier would appear on every pass and stop meaning anything —
        which is how an honest warning becomes noise and then wallpaper."""
        d, tp = self._seat("codex", age_s=5)
        with mock.patch.object(silent_drop.autocompact, "proxy_seats",
                               return_value=["codex"]), \
                mock.patch.object(seat, "_seat_family",
                                  return_value=("codex", None)), \
                mock.patch.object(seat, "_instance_dir", return_value=d), \
                mock.patch.object(silent_drop.autocompact, "_newest_transcript",
                                  return_value=tp):
            self.assertEqual(silent_drop.unscannable(), [])

    def test_an_unknown_family_is_unscannable_not_clean(self):
        with mock.patch.object(silent_drop.autocompact, "proxy_seats",
                               return_value=["not-a-family"]):
            out = silent_drop.unscannable()
        self.assertEqual(len(out), 1)
        self.assertIn("not a known seat family", out[0][1])

if __name__ == "__main__":
    unittest.main()
