import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import chat, home, pk, record, reflex  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM")


class ReflexTest(unittest.TestCase):
    def setUp(self):
        home.scaffold_global()
        for n in os.listdir(os.path.join(home.global_dir(), "reflexes")):
            os.remove(os.path.join(home.global_dir(), "reflexes", n))

    def test_write_load_roundtrip(self):
        reflex.write({"id": "checkpoint-green", "steer": "checkpoint the green slice",
                      "signal": "prompt", "pattern": r"\bcommit\b"})
        es = reflex.load_all()
        self.assertEqual(len(es), 1)
        self.assertEqual(es[0]["id"], "checkpoint-green")

    def test_fire_prompt_signal(self):
        reflex.write({"id": "r1", "steer": "s1", "signal": "prompt", "pattern": r"\bdeploy\b"})
        self.assertEqual(len(reflex.fire("time to deploy this")), 1)
        self.assertEqual(reflex.fire("nothing relevant"), [])  # salience law

    def test_fire_marker_signal(self):
        marker = tempfile.mktemp()
        reflex.write({"id": "afk", "steer": "operator is away", "signal": "marker-file",
                      "marker": marker})
        self.assertEqual(reflex.fire("anything"), [])
        with open(marker, "w") as f:
            f.write("x")
        self.assertEqual(len(reflex.fire("anything")), 1)
        os.remove(marker)

    def test_every_turn_budget(self):
        for i in range(5):
            reflex.write({"id": "c%d" % i, "steer": "s", "signal": "every-turn"})
        self.assertEqual(len(reflex.fire("x")), reflex.EVERY_TURN_BUDGET)

    def test_bad_regex_fails_open(self):
        reflex.write({"id": "bad", "steer": "s", "signal": "prompt", "pattern": "("})
        self.assertEqual(reflex.fire("anything ("), [])

    def test_retired_silent(self):
        reflex.write({"id": "r2", "steer": "s2", "signal": "every-turn", "status": "retired"})
        self.assertEqual(reflex.fire("x"), [])


class SeedBase(unittest.TestCase):
    """Fresh HELM_HOME per test — the default pack must never land in (or read
    from) the module-level home the ReflexTest wipes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seed-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def by_id(self, **kw):
        return {e["id"]: e for e in reflex.load_all(**kw)}


class SeedDefaultsTest(SeedBase):
    def test_seed_installs_eight_marked_defaults(self):
        wrote = reflex.seed_defaults()
        self.assertEqual(len(wrote), 8)
        es = self.by_id()
        self.assertEqual(sorted(es), [
            "compaction-continuity", "correction-language", "loop-thrash",
            "owner-chat-unread", "punt-tell", "stalled-driver",
            "stuck-commonsense", "uncommitted-drift"])
        for e in es.values():
            self.assertEqual(e["source"], "helm-default")  # shipped, legibly
        for rid in ("compaction-continuity", "correction-language", "punt-tell"):
            self.assertEqual(es[rid]["signal"], "prompt")
            self.assertTrue(es[rid]["pattern"])
        # the chat notify reflex: marker-file on the room's owner-unread flag,
        # path resolved at SEED time from HELM_CHAT_DIR (env-respecting)
        e = es["owner-chat-unread"]
        self.assertEqual(e["signal"], "marker-file")
        self.assertEqual(e["marker"], chat.marker_path("main"))
        self.assertTrue(e["marker"].startswith(os.environ["HELM_CHAT_DIR"]))

    def test_seed_marker_tracks_a_homed_seats_room(self):
        """Team-room homing (slice 3): a seed under HELM_CHAT_ROOM points the
        owner-chat-unread marker at the seat's OWN room, not main."""
        os.environ["HELM_CHAT_ROOM"] = "team-x"
        reflex.seed_defaults()
        e = self.by_id()["owner-chat-unread"]
        self.assertEqual(e["marker"], chat.marker_path("team-x"))
        self.assertNotEqual(e["marker"], chat.marker_path("main"))

    def test_seeded_counter_pack_has_field_tested_defaults(self):
        reflex.seed_defaults()
        es = self.by_id()
        for rid, sig, cn, th in (("stalled-driver", "stalled", "passive-streak", 6),
                                 ("loop-thrash", "thrash", "loop-streak", 3),
                                 ("uncommitted-drift", "drift", "dirty-streak", 8),
                                 ("stuck-commonsense", "stuck", "stuck-streak", 3)):
            e = es[rid]
            self.assertEqual(e["signal"], sig)
            self.assertEqual(reflex.counter_spec(e)[:2], (cn, th))  # (counter, threshold)
            self.assertTrue(reflex._truthy(e["latch"]), rid)        # every seeded counter latches
        self.assertEqual(reflex.counter_spec(es["stuck-commonsense"])[2], 3)  # escalating

    def test_reseed_is_a_byte_identical_noop(self):
        reflex.seed_defaults()
        def snap():
            out = {}
            for e in reflex.load_all():
                with open(e["path"]) as f:
                    out[e["id"]] = f.read()
            return out
        before = snap()
        self.assertEqual(reflex.seed_defaults(), [])
        self.assertEqual(snap(), before)

    def test_operator_edit_survives_reseed(self):
        reflex.seed_defaults()
        reflex.write({"id": "punt-tell", "steer": "my own wording",
                      "signal": "prompt", "pattern": r"\blater\b"})
        self.assertEqual(reflex.seed_defaults(), [])
        e = self.by_id()["punt-tell"]
        self.assertEqual(e["steer"], "my own wording")
        self.assertNotEqual(e["source"], "helm-default")  # authored now

    def test_retired_default_stays_retired(self):
        reflex.seed_defaults()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reflex.cmd_reflex(["retire", "punt-tell"]), 0)
        self.assertEqual(reflex.seed_defaults(), [])
        self.assertNotIn("punt-tell", self.by_id())
        e = self.by_id(include_retired=True)["punt-tell"]
        self.assertEqual(e["status"], "retired")
        self.assertEqual(reflex.fire("do it later"), [])

    def test_scaffold_global_seeds_and_stays_idempotent(self):
        home.scaffold_global()
        self.assertEqual(len(reflex.load_all()), 8)
        home.scaffold_global()
        self.assertEqual(len(reflex.load_all()), 8)


class DefaultPackFiringTest(SeedBase):
    def setUp(self):
        super().setUp()
        reflex.seed_defaults()

    def fired(self, text):
        return sorted(e["id"] for e in reflex.fire(text))

    def test_correction_language_fires(self):
        for t in ("no, actually the store is HOME-anchored",
                  "that's wrong, re-read the spec",
                  "i said use the typed store",
                  "stop doing per-commit pushes"):
            self.assertEqual(self.fired(t), ["correction-language"], t)

    def test_punt_tell_fires(self):
        for t in ("let's wire the guard later",
                  "leave a TODO by the cache path",
                  "good enough for now",
                  "park it until next session"):
            self.assertEqual(self.fired(t), ["punt-tell"], t)

    def test_owner_chat_unread_fires_on_marker_only(self):
        # the chat notify loop: marker present -> fires on ANY turn text;
        # consumed (cleared) -> silent again
        chat.post("agents, status?", who="owner")
        chat.mark_owner_unread()
        self.assertEqual(self.fired("totally unrelated turn"), ["owner-chat-unread"])
        chat.consume(total=chat.read()[1])
        self.assertEqual(reflex.fire("totally unrelated turn"), [])

    def test_compaction_continuity_fires(self):
        for t in ("This session is being continued from a previous conversation.",
                  "the conversation was summarized to fit the window",
                  "context was compacted; resuming the build"):
            self.assertEqual(self.fired(t), ["compaction-continuity"], t)

    def test_generic_prompts_fire_none(self):
        for t in ("please refactor the auth module and add coverage",
                  "what does the registry sync actually write?",
                  "run the suite and show the failures",
                  # narrative "later"/"todo" is not a punt — the tightened
                  # pattern demands punt-SHAPED phrasing
                  "3 days later the bug reappeared in the todo list view",
                  "the changelog was updated two hours later",
                  ""):
            self.assertEqual(reflex.fire(t), [], t)  # specificity guard

    def test_seeded_counter_reflexes_silent_without_counters(self):
        # fail-open: the seeded counter pack is live but no session/counters are
        # threaded -> every counter signal is ABSENT (v1 degrade), never a raise
        self.assertEqual(reflex.fire("run the suite"), [])
        self.assertEqual(reflex.fire("run the suite", session="s", counters={}), [])

    def test_nudge_lines_short_and_single(self):
        for e in reflex.load_all():
            line = "REFLEX: " + e["steer"]  # exactly what the lane emits
            self.assertNotIn("\n", line)
            self.assertLessEqual(len(line), 160, e["id"])


class LiveLaneTest(SeedBase):
    """The seeded pack must reach the per-turn surface through the same public
    read path inject uses (reflex.fire -> the reflex lane) — no inject edits."""

    def test_lane_carries_the_nudge_through_gather(self):
        home.scaffold_global()  # first-use scaffolding seeds the pack
        from helm import inject
        steer = next(d["steer"] for d in reflex.DEFAULT_PACK
                     if d["id"] == "correction-language")
        sections = inject.gather("no, actually keep the store HOME-anchored")
        self.assertEqual(sections["reflex"], ["REFLEX: " + steer])
        self.assertEqual(inject.gather("refactor the parser")["reflex"], [])


class CounterLatchTest(SeedBase):
    """v2 counter/latch, hermetic: planted counters + a real session state dir.
    text="" keeps every prompt/marker reflex silent, so the assertions see the
    counter lane alone."""

    def setUp(self):
        super().setUp()
        reflex.seed_defaults()

    def fired(self, sid, counters, text=""):
        return sorted(e["id"] for e in reflex.fire(text, session=sid, counters=counters))

    def test_threshold_gate_and_latch_lifecycle(self):
        sid = "sess-stall"
        self.assertEqual(self.fired(sid, {"passive-streak": 5}), [])         # under the line
        self.assertEqual(self.fired(sid, {"passive-streak": 6}), ["stalled-driver"])  # crosses
        self.assertEqual(self.fired(sid, {"passive-streak": 9}), [])         # latched: once per episode
        self.assertEqual(self.fired(sid, {"passive-streak": 0}), [])         # inverse event clears it
        self.assertEqual(self.fired(sid, {"passive-streak": 6}), ["stalled-driver"])  # re-armed, re-fires

    def test_stuck_escalates_the_rest_fire_once(self):
        sid = "sess-stuck"
        self.assertEqual(self.fired(sid, {"stuck-streak": 3}), ["stuck-commonsense"])  # first cross
        self.assertEqual(self.fired(sid, {"stuck-streak": 4}), [])            # < last(3)+escalate(3)
        self.assertEqual(self.fired(sid, {"stuck-streak": 5}), [])            # still < 6
        self.assertEqual(self.fired(sid, {"stuck-streak": 6}), ["stuck-commonsense"])  # escalates
        self.assertEqual(self.fired(sid, {"stuck-streak": 0}), [])            # recovery clears

    def test_missing_counter_key_fails_open(self):
        # counters present but not the one this reflex watches -> absent, no raise
        self.assertEqual(reflex.fire("", session="s", counters={"other-streak": 99}), [])

    def test_reads_live_record_counters_when_not_planted(self):
        sid = "live-sess"
        pk.write_json(os.path.join(record.session_dir(sid), "counters.json"),
                      {"dirty-streak": 8})
        got = sorted(e["id"] for e in reflex.fire("", session=sid))  # counters=None -> live read
        self.assertEqual(got, ["uncommitted-drift"])

    def test_level_triggered_without_latch_fires_every_turn(self):
        reflex.write({"id": "lvl", "steer": "level fire", "signal": "counter",
                      "counter": "passive-streak", "threshold": "2"})
        sid = "lvl-sess"
        self.assertEqual(self.fired(sid, {"passive-streak": 3}), ["lvl"])
        self.assertEqual(self.fired(sid, {"passive-streak": 3}), ["lvl"])  # again — no latch

    def test_counter_prompt_gate_is_prompt_text_only(self):
        reflex.write({"id": "gated", "steer": "s", "signal": "counter",
                      "counter": "passive-streak", "threshold": "2",
                      "pattern": r"\bdeploy\b", "latch": "true"})
        sid = "gate-sess"
        self.assertNotIn("gated", self.fired(sid, {"passive-streak": 5}, "just reading"))
        self.assertIn("gated", self.fired(sid, {"passive-streak": 5}, "time to deploy"))

    def test_window_freshness_gate(self):
        reflex.write({"id": "win", "steer": "s", "signal": "counter",
                      "counter": "passive-streak", "threshold": "2",
                      "window": "60", "latch": "true"})
        self.assertIn("win", self.fired("fresh-sess",
                                        {"passive-streak": 5, "ts": pk.now_ts()}))
        self.assertNotIn("win", self.fired("stale-sess",
                                           {"passive-streak": 5, "ts": "2000-01-01T00:00:00Z"}))

    def test_latch_file_is_pruned_on_read(self):
        sid = "prune-sess"
        reflex.fire("", session=sid, counters={"passive-streak": 6})   # latches
        self.assertIn("stalled-driver", pk.read_json(reflex._latch_path(sid), {}))
        reflex.fire("", session=sid, counters={"passive-streak": 0})   # inverse event
        self.assertNotIn("stalled-driver", pk.read_json(reflex._latch_path(sid), {}))  # pruned

    def test_persist_false_never_mutates_latch(self):
        sid = "ro-sess"
        reflex.fire("", session=sid, counters={"passive-streak": 6}, persist=False)
        self.assertFalse(os.path.exists(reflex._latch_path(sid)))  # a dry look writes nothing

    def test_latch_state_is_session_scoped(self):
        # a fresh session re-arms latches (documented: correct behavior)
        self.assertEqual(self.fired("sA", {"passive-streak": 6}), ["stalled-driver"])
        self.assertEqual(self.fired("sA", {"passive-streak": 6}), [])            # latched in sA
        self.assertEqual(self.fired("sB", {"passive-streak": 6}), ["stalled-driver"])  # sB re-arms


class AddCounterTest(SeedBase):
    def test_add_counter_reflex_roundtrips(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = reflex.cmd_reflex(["add", "watch-stuck | do not spin",
                                    "--signal", "stuck", "--threshold", "2", "--latch"])
        self.assertEqual(rc, 0)
        e = {x["id"]: x for x in reflex.load_all()}["watch-stuck"]
        self.assertEqual(e["signal"], "stuck")
        self.assertEqual(reflex.counter_spec(e)[:2], ("stuck-streak", 2))
        self.assertTrue(reflex._truthy(e["latch"]))
        # and it fires against a planted counter
        self.assertIn("watch-stuck", [x["id"] for x in reflex.fire(
            "", session="s", counters={"stuck-streak": 2})])

    def test_add_named_counter_uses_field_tested_default(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = reflex.cmd_reflex(["add", "my-stall | move", "--signal", "stalled"])
        self.assertEqual(rc, 0)
        e = {x["id"]: x for x in reflex.load_all()}["my-stall"]
        self.assertEqual(reflex.counter_spec(e)[:2], ("passive-streak", 6))  # default

    def test_add_generic_counter_needs_a_name(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = reflex.cmd_reflex(["add", "bare | x", "--signal", "counter",
                                    "--threshold", "3"])
        self.assertEqual(rc, 2)  # specificity law: no counter name, no reflex

    def test_add_counter_rejects_nonpositive_threshold(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = reflex.cmd_reflex(["add", "z | x", "--signal", "stalled",
                                    "--threshold", "0"])
        self.assertEqual(rc, 2)


class SmokeTest(SeedBase):
    """The LIVE read-only smoke: `helm reflex smoke` reads the real record.py
    counters and reports fires WITHOUT mutating any latch."""

    def setUp(self):
        super().setUp()
        reflex.seed_defaults()

    def test_smoke_reads_live_counters_read_only(self):
        sid = "smoke-sess"
        pk.write_json(os.path.join(record.session_dir(sid), "counters.json"),
                      {"stuck-streak": 4})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = reflex.cmd_reflex(["smoke", "--session", sid])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("stuck-commonsense", out)
        self.assertIn("FIRE", out)
        self.assertFalse(os.path.exists(reflex._latch_path(sid)))  # read-only

    def test_smoke_with_no_sessions_is_graceful(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = reflex.cmd_reflex(["smoke"])
        self.assertEqual(rc, 0)
        self.assertIn("no recorded sessions", buf.getvalue())


class CounterEndToEndTest(ReflexTest):
    """The UNPLANTED path: real hook events through record.record() → real
    counters.json → reflex.fire(session=) with NO planted counters dict.
    Every signal kind (thrash/stalled/drift/stuck/generic counter) proven to
    fire off the recorder itself — the live wiring, hermetically."""

    def ev(self, sid, tool="Read", cmd=None, resp=None, failed=False):
        e = {"session_id": sid, "tool_name": tool, "cwd": "/tmp/p",
             "hook_event_name": "PostToolUseFailure" if failed else "PostToolUse"}
        if cmd is not None:
            e["tool_input"] = {"command": cmd}
        if resp is not None:
            e["tool_response"] = resp
        return e

    def test_thrash_latch_fire_clear_refire(self):
        reflex.write({"id": "loop-thrash", "steer": "loop detected",
                      "signal": "thrash", "threshold": "3", "latch": "true"})
        sid = "e2e-thrash"
        for _ in range(4):
            record.record(self.ev(sid, tool="Bash", cmd="pytest -x tests"))
        self.assertEqual(record.counters(sid).get("loop-streak"), 3)
        fired = reflex.fire("", session=sid)
        self.assertEqual([e["id"] for e in fired], ["loop-thrash"])
        self.assertEqual(reflex.fire("", session=sid), [])   # latched: once/episode
        record.record(self.ev(sid, tool="Bash", cmd="git status"))
        self.assertEqual(record.counters(sid).get("loop-streak"), 0)
        self.assertEqual(reflex.fire("", session=sid), [])   # inverse event…
        self.assertEqual(reflex._load_latch(sid), {})        # …drops the latch
        for _ in range(4):
            record.record(self.ev(sid, tool="Bash", cmd="pytest -x tests"))
        fired = reflex.fire("", session=sid)                 # re-armed episode
        self.assertEqual([e["id"] for e in fired], ["loop-thrash"])

    def test_stalled_fires_once_from_passive_streak(self):
        reflex.write({"id": "stalled-driver", "steer": "you may be circling",
                      "signal": "stalled", "threshold": "6", "latch": "true"})
        sid = "e2e-stalled"
        for _ in range(6):
            record.record(self.ev(sid, tool="Read"))
        self.assertEqual(record.counters(sid).get("passive-streak"), 6)
        self.assertEqual(len(reflex.fire("", session=sid)), 1)
        record.record(self.ev(sid, tool="Read"))             # streak deepens…
        self.assertEqual(reflex.fire("", session=sid), [])   # …no escalate: quiet

    def test_drift_fires_and_commit_clears(self):
        reflex.write({"id": "uncommitted-drift", "steer": "checkpoint the slice",
                      "signal": "drift", "threshold": "8", "latch": "true"})
        sid = "e2e-drift"
        with mock.patch.object(record, "_git_dirty", lambda wd: True):
            record.record(self.ev(sid, tool="Edit"))
            for _ in range(7):
                record.record(self.ev(sid, tool="Read"))
        self.assertEqual(record.counters(sid).get("dirty-streak"), 8)
        self.assertEqual(len(reflex.fire("", session=sid)), 1)
        record.record(self.ev(sid, tool="Bash", cmd="git commit -m checkpoint"))
        self.assertEqual(record.counters(sid).get("dirty-streak"), 0)
        self.assertEqual(reflex.fire("", session=sid), [])
        self.assertEqual(reflex._load_latch(sid), {})        # commit = the clear

    def test_stuck_escalates_as_the_streak_worsens(self):
        reflex.write({"id": "stuck-commonsense", "steer": "stop retrying",
                      "signal": "stuck", "threshold": "3", "escalate": "3",
                      "latch": "true"})
        sid = "e2e-stuck"
        for _ in range(3):
            record.record(self.ev(sid, tool="Bash", cmd="curl api",
                                  resp="401 unauthorized"))
        self.assertEqual(record.counters(sid).get("stuck-streak"), 3)
        self.assertEqual(len(reflex.fire("", session=sid)), 1)
        self.assertEqual(reflex.fire("", session=sid), [])   # latched at 3
        for _ in range(3):
            record.record(self.ev(sid, tool="Bash", cmd="curl api",
                                  resp="401 unauthorized", failed=True))
        self.assertEqual(record.counters(sid).get("stuck-streak"), 6)
        self.assertEqual(len(reflex.fire("", session=sid)), 1)  # escalation re-fire

    def test_generic_counter_is_level_triggered_without_latch(self):
        reflex.write({"id": "warm-seat", "steer": "still reading",
                      "signal": "counter", "counter": "passive-streak",
                      "threshold": "2"})
        sid = "e2e-generic"
        record.record(self.ev(sid, tool="Read"))
        record.record(self.ev(sid, tool="Read"))
        self.assertEqual(len(reflex.fire("", session=sid)), 1)
        self.assertEqual(len(reflex.fire("", session=sid)), 1)  # level, not edge


if __name__ == "__main__":
    unittest.main()
