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


class ReflexBase(unittest.TestCase):
    """An empty reflex store: setUp scaffolds the global home and removes
    every reflex in it.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def setUp(self):
        home.scaffold_global()
        for n in os.listdir(os.path.join(home.global_dir(), "reflexes")):
            os.remove(os.path.join(home.global_dir(), "reflexes", n))


class ReflexTest(ReflexBase):
    """The reflex store arms, on ReflexBase's empty store.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses ReflexBase."""

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

    def test_recorded_project_fences_the_global_reflex_lane(self):
        """task/2435: a reflex recorded to one project reaches that project's
        seats and no other's, while an unrecorded one still reaches both.

        BOTH ARMS RUN AGAINST THE SAME OBSERVABLE IN ONE CALL, because the
        absence half ("helm's reflex does not reach another project") is worthless
        without a positive control proving the probe can see a reflex arrive at
        all: `fleet-wide` is that control and it is asserted unconditionally.
        """
        reflex.write({"id": "helm-only", "steer": "helm lane steer",
                      "signal": "every-turn", "project": "helm"})
        reflex.write({"id": "fleet-wide", "steer": "everyone steer",
                      "signal": "every-turn"})
        helm_ids = {e["id"] for e in reflex.load_all(project="helm")}
        other_ids = {e["id"] for e in reflex.load_all(project="other-project")}
        lens_ids = {e["id"] for e in reflex.load_all()}
        # the control: the unrecorded reflex reaches every lane and the lens
        self.assertIn("fleet-wide", helm_ids)
        self.assertIn("fleet-wide", other_ids)
        self.assertIn("fleet-wide", lens_ids)
        # the fence: the recorded one reaches only its own project
        self.assertIn("helm-only", helm_ids)
        self.assertNotIn("helm-only", other_ids)
        # a project-less call is a global LENS, not a seat — it sees everything
        self.assertIn("helm-only", lens_ids)

    def test_recorded_fleet_reaches_every_project(self):
        """`fleet` is a recorded value, not a silence: it must survive the
        fence exactly as an unrecorded reflex does."""
        reflex.write({"id": "owner-policy", "steer": "owner policy steer",
                      "signal": "every-turn", "project": "fleet"})
        reflex.write({"id": "elsewhere", "steer": "other steer",
                      "signal": "every-turn", "project": "third-project"})
        ids = {e["id"] for e in reflex.load_all(project="other-project")}
        self.assertIn("owner-policy", ids)
        self.assertNotIn("elsewhere", ids)

    def test_project_round_trips_through_the_parser_allowlist(self):
        """The written field must come back out: a key absent from _DEFAULTS is
        dropped SILENTLY, which would fence nothing while looking correct."""
        reflex.write({"id": "scoped", "steer": "s", "signal": "every-turn",
                      "project": "helm"})
        e = {x["id"]: x for x in reflex.load_all()}["scoped"]
        self.assertEqual(e["project"], "helm")
        plain = reflex.write({"id": "bare", "steer": "s", "signal": "every-turn"})
        with open(plain) as f:
            self.assertNotIn("project:", f.read())
        self.assertEqual({x["id"]: x for x in reflex.load_all()}["bare"]["project"], "")


class RescopeVerbTest(ReflexBase):
    """The WRITE half of the scope door (task/2435, task/2447).

    load_all's own docstring says nothing changes for an unrecorded reflex
    "until someone records a scope", and until this verb there was no way to
    record one on an EXISTING reflex: `add --project` picks a directory for a
    NEW one, and everything already in the global dir could only be rescoped by
    hand-editing a file under the helm home."""

    def _rescope(self, *args):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = reflex.cmd_reflex(["rescope"] + list(args))
        return rc, buf.getvalue() + err.getvalue()

    def test_rescope_records_and_the_fence_then_holds(self):
        """The verb's whole point is the DELIVERY change, so the assertion is
        on the lane, not on the file. Positive control in the same call: an
        unrecorded sibling keeps reaching the other project throughout, so a
        lane that went empty for some unrelated reason cannot pass this."""
        reflex.write({"id": "helm-mechanics", "steer": "trains and lanes",
                      "signal": "every-turn"})
        reflex.write({"id": "portable", "steer": "everyone steer",
                      "signal": "every-turn"})
        before = {e["id"] for e in reflex.load_all(project="other-project")}
        self.assertIn("helm-mechanics", before)      # it reached before
        self.assertIn("portable", before)            # control
        rc, out = self._rescope("helm-mechanics", "helm")
        self.assertEqual(rc, 0, out)
        after = {e["id"] for e in reflex.load_all(project="other-project")}
        self.assertNotIn("helm-mechanics", after)    # the fence now holds
        self.assertIn("portable", after)             # control, unconditional
        self.assertIn("helm-mechanics",
                      {e["id"] for e in reflex.load_all(project="helm")})

    def test_rescope_clears_back_to_every_project(self):
        """`-` is the mirror: the row returns to reaching everyone. A door that
        only narrows is a trap, because a mis-scoped reflex is invisible."""
        reflex.write({"id": "r", "steer": "s", "signal": "every-turn",
                      "project": "helm"})
        self.assertNotIn("r", {e["id"] for e in reflex.load_all(project="o")})
        rc, out = self._rescope("r", "-")
        self.assertEqual(rc, 0, out)
        self.assertIn("r", {e["id"] for e in reflex.load_all(project="o")})

    def test_rescope_preserves_every_other_field(self):
        """It edits ONE line. A re-serializing implementation would silently
        drop whatever the frontmatter allowlist does not carry, on files an
        operator is invited to hand-edit."""
        reflex.write({"id": "counted", "steer": "s", "signal": "stuck",
                      "counter": "c", "threshold": "3", "latch": "true",
                      "notes": "keep me"})
        rc, out = self._rescope("counted", "helm")
        self.assertEqual(rc, 0, out)
        e = {x["id"]: x for x in reflex.load_all(project="helm")}["counted"]
        self.assertEqual(e["project"], "helm")
        for k, v in (("signal", "stuck"), ("counter", "c"),
                     ("threshold", "3"), ("notes", "keep me")):
            self.assertEqual(e[k], v)

    def test_rescope_refuses_a_name_the_record_cannot_carry(self):  # noqa: VACUOUS_ASSERTION — the trailing `fleet` rescope is the unconditional control on the same observable: the same verb writing the same field, asserted to land.
        """One validation door, not a second spelling of it: the operand meets
        the same two contracts `helm store rescope` applies, because the two
        recorded names are compared by ONE shared predicate."""
        reflex.write({"id": "r", "steer": "s", "signal": "every-turn"})
        for bad in ("bad/name", "two\nlines"):
            rc, _ = self._rescope("r", bad)
            self.assertEqual(rc, 2)
            self.assertEqual(
                {x["id"]: x for x in reflex.load_all()}["r"]["project"], "",
                "a refused operand must write nothing")
        rc, out = self._rescope("r", "fleet")     # control: a good name lands
        self.assertEqual(rc, 0, out)

    def test_rescope_reports_a_missing_reflex_rather_than_inventing_one(self):
        rc, out = self._rescope("no-such-reflex", "helm")
        self.assertEqual(rc, 1)
        self.assertIn("not found", out)

    def test_list_shows_the_scope_that_governs_delivery(self):
        """The listing is the read surface for this door; before this it showed
        no scope at all, so a fleet-wide row and a fenced one rendered
        identically and the operator could not see which still had none."""
        reflex.write({"id": "fenced", "steer": "s", "signal": "every-turn",
                      "project": "helm"})
        reflex.write({"id": "loose", "steer": "s", "signal": "every-turn"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            reflex.cmd_reflex(["list"])
        out = buf.getvalue()
        self.assertRegex(out, r"fenced.*\[helm\]")
        self.assertRegex(out, r"loose.*\[fleet \(derived\)\]")
        self.assertIn("reach EVERY project", out)


class SeedBase(unittest.TestCase):
    """Fresh HELM_HOME per test — the default pack must never land in (or read
    from) the module-level home the ReflexBase wipes."""

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
    def test_seed_installs_nine_marked_defaults(self):
        wrote = reflex.seed_defaults()
        self.assertEqual(len(wrote), 9)
        es = self.by_id()
        self.assertEqual(sorted(es), [
            "compaction-continuity", "correction-language", "guard-friction",
            "loop-thrash",
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
        for rid, sig, cn, th in (("stalled-driver", "stalled", "stalled-turns", 6),
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
        self.assertEqual(len(reflex.load_all()), 9)
        home.scaffold_global()
        self.assertEqual(len(reflex.load_all()), 9)


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
        chat.post("agents, status?", who="daria")
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
        self.assertEqual(self.fired(sid, {"stalled-turns": 5}), [])         # under the line
        self.assertEqual(self.fired(sid, {"stalled-turns": 6}), ["stalled-driver"])  # crosses
        self.assertEqual(self.fired(sid, {"stalled-turns": 9}), [])         # latched: once per episode
        self.assertEqual(self.fired(sid, {"stalled-turns": 0}), [])         # inverse event clears it
        self.assertEqual(self.fired(sid, {"stalled-turns": 6}), ["stalled-driver"])  # re-armed, re-fires

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
        reflex.fire("", session=sid, counters={"stalled-turns": 6})   # latches
        self.assertIn("stalled-driver", pk.read_json(reflex._latch_path(sid), {}))
        reflex.fire("", session=sid, counters={"stalled-turns": 0})   # inverse event
        self.assertNotIn("stalled-driver", pk.read_json(reflex._latch_path(sid), {}))  # pruned

    def test_persist_false_never_mutates_latch(self):
        sid = "ro-sess"
        reflex.fire("", session=sid, counters={"stalled-turns": 6}, persist=False)
        self.assertFalse(os.path.exists(reflex._latch_path(sid)))  # a dry look writes nothing

    def test_latch_state_is_session_scoped(self):
        # a fresh session re-arms latches (documented: correct behavior)
        self.assertEqual(self.fired("sA", {"stalled-turns": 6}), ["stalled-driver"])
        self.assertEqual(self.fired("sA", {"stalled-turns": 6}), [])            # latched in sA
        self.assertEqual(self.fired("sB", {"stalled-turns": 6}), ["stalled-driver"])  # sB re-arms


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
        self.assertEqual(reflex.counter_spec(e)[:2], ("stalled-turns", 6))  # default

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


class CounterEndToEndTest(ReflexBase):
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

    def test_stalled_fires_once_from_stalled_turns(self):
        """task/2970: the stalled signal reads TURNS, closed by turn_open (the
        per-turn hook's first act), not the call-level passive-streak."""
        reflex.write({"id": "stalled-driver", "steer": "you may be circling",
                      "signal": "stalled", "threshold": "6", "latch": "true"})
        sid = "e2e-stalled"
        for _ in range(6):
            record.record(self.ev(sid, tool="Read"))
        self.assertEqual(record.counters(sid).get("passive-streak"), 6)
        self.assertEqual(reflex.fire("", session=sid), [],
                         "six calls with no turn boundary are no stalled turn")
        for _ in range(6):
            record.turn_open(sid, "typed turn")
            record.record(self.ev(sid, tool="Read"))
        record.turn_open(sid, "typed turn")
        self.assertEqual(record.counters(sid).get("stalled-turns"), 6)
        self.assertEqual(len(reflex.fire("", session=sid)), 1)
        record.record(self.ev(sid, tool="Read"))
        record.turn_open(sid, "typed turn")                  # streak deepens…
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


class StalledTurnTest(SeedBase):
    """task/2970: stalled-driver says "6 turns with no forward op", so it
    counts TURNS a seat spent without one, driven here exactly as a live seat
    drives it: `inject.gather` is the UserPromptSubmit hook that opens each
    turn and `record.record` is the PostToolUse hook that sees each call.

    MEASURED: 191 fires in 41h, 156 of them on turns a chat wake or
    a task notice began, because the old counter bumped on every non-edit
    CALL — six reads in one reviewing turn read as six stalled turns."""

    STALL = "stalled-driver"

    def setUp(self):
        super().setUp()
        home.scaffold_global()                   # seeds stalled-driver
        from helm import inject
        self.inject = inject
        p = mock.patch.object(inject, "_greeted_today", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.steer = "REFLEX: " + next(d["steer"] for d in reflex.DEFAULT_PACK
                                       if d["id"] == self.STALL)
        self.sid = "sess-turns"
        # A SESSION THE RECORDER HAS SEEN. The first call of a session is the
        # recorder's first evidence of it; turns before that are not counted.
        self.tool("Edit", file_path="/tmp/fixture/prime.py")

    def prompt(self, text):
        """Open a turn; True when the stall steer rode it."""
        return self.steer in self.inject.gather(text, session=self.sid)["reflex"]

    def tool(self, name="Read", n=1, agent_id=None, failed=False, **tin):
        for _ in range(n):
            e = {"session_id": self.sid, "tool_name": name, "cwd": self.tmp,
                 "hook_event_name": record.FAIL_EVENT if failed
                 else record.HOOK_EVENT,
                 "tool_input": tin or {"file_path": "/tmp/fixture/a.py"}}
            if failed:
                e.update(error="Exit code 1", is_interrupt=False)
            if agent_id:
                e["agent_id"] = agent_id
            with mock.patch.object(record, "_git_dirty", return_value=False):
                record.record(e)

    def stalled_turns(self, n, reads=1):
        """n typed turns, each spending `reads` reads and no forward op; the
        stall steer must ride none of them."""
        for i in range(n):
            self.assertFalse(self.prompt("typed turn %d" % i), i)
            self.tool("Read", n=reads)

    def test_one_reviewing_turn_of_many_reads_is_one_turn(self):  # noqa: VACUOUS_ASSERTION — test_six_stalled_typed_turns_fire_once and test_a_read_verb_or_a_failed_write_earns_no_credit fire the same steer through the same gather observable on this fixture
        self.assertFalse(self.prompt("review the lane"))
        self.tool("Read", n=8)
        self.assertFalse(self.prompt("and the tests?"),
                         "eight reads in ONE turn are one turn, not eight")

    def test_six_stalled_typed_turns_fire_once(self):
        """THE POSITIVE CONTROL: the rule still fires where it applies."""
        self.stalled_turns(6)
        self.assertTrue(self.prompt("typed turn 6"))
        self.tool("Read")
        self.assertFalse(self.prompt("typed turn 7"), "latched: once per episode")

    def test_turns_a_notice_began_never_count(self):  # noqa: VACUOUS_ASSERTION — test_six_stalled_typed_turns_fire_once and test_a_read_verb_or_a_failed_write_earns_no_credit fire the same steer through the same gather observable on this fixture
        from tests import _notices as N
        for text in (N.wake("the review is in"), N.agent_done("all green"),
                     N.command_done("run the suite")) * 3:
            self.assertFalse(self.prompt(text))
            self.tool("Read", n=2)
        self.assertFalse(self.prompt("typed, after nine notice turns"))

    def test_a_turn_with_no_tool_call_is_conversation(self):  # noqa: VACUOUS_ASSERTION — test_six_stalled_typed_turns_fire_once and test_a_read_verb_or_a_failed_write_earns_no_credit fire the same steer through the same gather observable on this fixture
        for i in range(8):
            self.assertFalse(self.prompt("a question for you, %d" % i))

    def test_coordination_writes_are_forward_ops(self):  # noqa: VACUOUS_ASSERTION — test_six_stalled_typed_turns_fire_once and test_a_read_verb_or_a_failed_write_earns_no_credit fire the same steer through the same gather observable on this fixture
        self.stalled_turns(5)
        self.assertFalse(self.prompt("file what you found"))
        for cmd in ("helm task add 'cap the steer' --owner fixture-seat",
                    "helm chat post --room fixture-room <<'EOF'\nfiled\nEOF",
                    "helm store revise fixture-id corrected"):
            self.tool("Bash", command=cmd)
        self.tool("Read", n=3)
        self.stalled_turns(5)
        self.assertFalse(self.prompt("typed turn after"))

    def test_native_task_writes_and_a_lease_claim_are_forward_ops(self):  # noqa: VACUOUS_ASSERTION — test_six_stalled_typed_turns_fire_once fires the same steer through the same gather observable on this fixture
        for label, call in (("TaskCreate", dict(name="TaskCreate", subject="x")),
                            ("TaskUpdate", dict(name="TaskUpdate", taskId="1")),
                            ("claim", dict(name="Bash",
                                           command="helm chat claim lane/x"))):
            with self.subTest(op=label):
                self.stalled_turns(5)
                self.assertFalse(self.prompt("record the work"))
                self.tool(call.pop("name"), **call)
                self.assertFalse(self.prompt("typed turn after"),
                                 "durable work must not close as a stall")

    def test_a_git_grep_for_commit_earns_no_credit(self):
        self.stalled_turns(5)
        self.assertFalse(self.prompt("look for commit mentions"))
        self.tool("Bash", command="git grep -n commit -- docs")
        self.assertTrue(self.prompt("typed turn after"),
                        "a read that mentions commit is still a read")

    def test_a_read_verb_or_a_failed_write_earns_no_credit(self):
        """CONTROL for the arm above: the credit is the verb, not the word."""
        for i in range(6):
            self.assertFalse(self.prompt("typed turn %d" % i), i)
            if i % 2:
                self.tool("Bash", command="helm task show 12")
            else:
                self.tool("Bash", failed=True,
                          command="helm task add 'x' --owner fixture-seat")
        self.assertTrue(self.prompt("typed turn 6"))

    def test_a_forward_op_in_a_notice_turn_still_resets(self):  # noqa: VACUOUS_ASSERTION — test_six_stalled_typed_turns_fire_once and test_a_read_verb_or_a_failed_write_earns_no_credit fire the same steer through the same gather observable on this fixture
        from tests import _notices as N
        self.stalled_turns(5)
        self.assertFalse(self.prompt(N.wake("please fix the cap")))
        self.tool("Edit", file_path="/tmp/fixture/cap.py")
        self.stalled_turns(5)
        self.assertFalse(self.prompt("typed turn after"))

    def test_a_subagents_calls_are_not_the_seats_turn(self):  # noqa: VACUOUS_ASSERTION — test_six_stalled_typed_turns_fire_once and test_a_read_verb_or_a_failed_write_earns_no_credit fire the same steer through the same gather observable on this fixture
        for i in range(8):
            self.assertFalse(self.prompt("still waiting on the reviewer %d" % i), i)
            self.tool("Read", n=3, agent_id="afixture01")


class FanoutRekeyTest(SeedBase):
    """task/2971: subagent-no-fanout-must-be-enforced fired on the words
    constraint, delegated or carries in ANY prompt — 144 fires in 41h, 88% of
    the joinable ones on machine-authored prompts, none about a spawn. Its
    rule binds a subagent, so it is said to each one at SubagentStart,
    before its first step. The seed pass re-keys the authored
    entry to that signal, only while its trigger is still the exact legacy
    one, so an operator's own edit is never overwritten."""

    RID = "subagent-no-fanout-must-be-enforced"
    LEGACY = r"\b(constraint|delegated|carries)\b"
    STEER = "a fixture steer about nested spawns"

    def legacy(self, **extra):
        return reflex.write(dict({"id": self.RID, "steer": self.STEER,
                                  "signal": "prompt", "pattern": self.LEGACY,
                                  "stated_ts": "2026-08-27T10:33:06Z"}, **extra))

    def fired(self, text):
        return [e["id"] for e in reflex.fire(text)]

    def test_the_legacy_trigger_is_rekeyed_by_the_seed_pass(self):
        self.legacy()
        words = "the constraint carries a delegated brief"
        self.assertIn(self.RID, self.fired(words))            # control
        home.scaffold_global()
        e = self.by_id()[self.RID]
        self.assertEqual(e["signal"], "nested-spawn")
        self.assertEqual(e["pattern"], "")
        self.assertEqual(e["steer"], self.STEER)
        self.assertEqual(e["status"], "live")
        self.assertNotIn(self.RID, self.fired(words))

    def test_an_operator_edited_trigger_is_never_rekeyed(self):  # noqa: VACUOUS_ASSERTION — the file is compared EQUAL to its own non-empty prior bytes; test_the_legacy_trigger_is_rekeyed_by_the_seed_pass is the positive re-key on the same seed pass
        path = self.legacy(pattern=r"\bnested spawn\b")
        with open(path, encoding="utf-8") as f:
            before = f.read()
        home.scaffold_global()
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_the_rekey_touches_only_the_trigger_and_runs_once(self):
        path = self.legacy(project="helm", status="retired")
        with open(path, "a", encoding="utf-8") as f:
            f.write("hand-written prose below the steer\n")
        with open(path, encoding="utf-8") as f:
            before = f.read().splitlines()
        home.scaffold_global()
        with open(path, encoding="utf-8") as f:
            once = f.read()
        home.scaffold_global()
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), once, "a second seed pass is a no-op")
        after = once.splitlines()
        self.assertEqual(
            [l for l in before if not l.startswith(("  signal:", "  pattern:"))],
            [l for l in after if not l.startswith(("  signal:", "  pattern:",
                                                   "  notes:"))])
        self.assertIn("  signal: nested-spawn", after)
        self.assertFalse([l for l in after if l.startswith("  pattern:")])
        self.assertEqual(self.by_id(include_retired=True)[self.RID]["status"],
                         "retired")

    def test_a_nested_spawn_reflex_never_fires_on_prompt_text(self):
        reflex.write({"id": "fanout", "steer": self.STEER,
                      "signal": "nested-spawn"})
        reflex.write({"id": "control", "steer": "c", "signal": "prompt",
                      "pattern": r"\bspawn\b"})
        self.assertEqual(self.fired("the constraint carries a spawn"),
                         ["control"])

    def test_spawn_steers_honours_a_rekeyed_reflexs_recorded_project(self):  # noqa: VACUOUS_ASSERTION — the owning project's call is asserted EQUAL to a non-empty list before the two empty ones, on the same reader
        """The re-key keeps a legacy reflex's recorded project, so the
        reader must honour it rather than drop it."""
        self.legacy(project="fixture-project")
        home.scaffold_global()
        self.assertEqual(self.by_id()[self.RID]["signal"], "nested-spawn")
        want = [(self.RID, "REFLEX: " + self.STEER)]
        self.assertEqual(reflex.spawn_steers("fixture-project"), want)
        self.assertEqual(reflex.spawn_steers("other-project"), [])
        self.assertEqual(reflex.spawn_steers(None), [])

    def test_spawn_steers_lists_only_live_fleet_nested_spawn_reflexes(self):
        reflex.write({"id": "fanout", "steer": self.STEER,
                      "signal": "nested-spawn"})
        reflex.write({"id": "old-fanout", "steer": "x", "signal": "nested-spawn",
                      "status": "retired"})
        reflex.write({"id": "one-project", "steer": "y",
                      "signal": "nested-spawn", "project": "fixture-project"})
        reflex.write({"id": "prompt-one", "steer": "z", "signal": "prompt",
                      "pattern": r"\bspawn\b"})
        self.assertEqual(reflex.spawn_steers(),
                         [("fanout", "REFLEX: " + self.STEER)])


class AReflexDescriptionSaysWhenItIsShortTest(unittest.TestCase):
    """Same shape as the handoff and whoami writers, through the one helper:
    the steer lives whole in the metadata and the body, so the front-matter
    description is a glance — which must say when it is one."""

    def setUp(self):
        home.scaffold_global()
        for n in os.listdir(os.path.join(home.global_dir(), "reflexes")):
            os.remove(os.path.join(home.global_dir(), "reflexes", n))

    def desc(self, steer):
        path = reflex.write({"id": "long-one", "steer": steer})
        with open(path, encoding="utf-8") as fh:
            whole = fh.read()
        return pk.parse_simple_frontmatter(
            path, {"id": "", "steer": "", "description": ""}), whole

    def test_a_long_steer_is_cut_in_the_description_and_says_so(self):
        steer = ("hold the line on the measured thing " * 20).strip()
        got, whole = self.desc(steer)
        self.assertIn("[cut: 170 of ", got["description"])
        self.assertEqual(got["steer"], steer)    # whole, in the metadata
        self.assertIn("REFLEX: " + steer, whole)

    def test_a_short_steer_carries_no_mark(self):
        got, _whole = self.desc("checkpoint the green slice")
        self.assertEqual(got["description"],
                         "reflex: long-one - checkpoint the green slice")

    def test_a_multiline_steer_cannot_tear_the_front_matter(self):
        """A newline in the value would end the `description:` line and make
        `parse_simple_frontmatter` read back an entry nobody wrote."""
        got, _whole = self.desc("first\nsecond")
        self.assertEqual(got["description"], "reflex: long-one - first second")


if __name__ == "__main__":
    unittest.main()


class CorrectionAddresseeTest(SeedBase):
    """task/2978: correction-language fired on agents correcting THEIR OWN
    posts ("I said the cap was four. That was wrong") — 0 of 2 relevant in
    the E2 gold, and every one of the 13 fires in the 3,054-turn replay was
    an agent's first-person "I said <claim>". A correction the reflex exists
    for is addressed TO the agent: the owner's "i told you", "like i said",
    "i said use X". Measured over ~3,700 owner-typed prompts: 11 such
    phrases, all kept by the narrowed trigger."""

    RID = "correction-language"
    LEGACY = r"\b(no,? actually|that'?s (wrong|not what)|i (said|told you)|stop doing)\b"

    def fired(self, text):
        return self.RID in [e["id"] for e in reflex.fire(text)]

    def test_an_agent_correcting_its_own_post_is_not_a_correction(self):
        reflex.seed_defaults()
        self.assertTrue(self.fired("i told you to rebase first"))  # control
        for text in ("peer-seat: CORRECTION. I said the cap was four; it is six.",
                     "@all correcting my own number: I said PINNED is 5.4%",
                     "MY EARLIER RELAY WAS PARTLY WRONG. I said RebindT holds"):
            with self.subTest(text=text[:40]):
                self.assertFalse(self.fired(text))

    def test_a_correction_addressed_to_the_agent_still_fires(self):
        reflex.seed_defaults()
        self.assertTrue(self.fired("like i said, use the typed store"))
        for text in ("like i said, use the typed store",
                     "as i said this migration already happened once",
                     "i said do a council with whoever is awake",
                     "i thought i said to pull them into fab",
                     "i told you to self-compact",
                     "i said use the typed store",
                     "no, actually keep it HOME-anchored",
                     "that's not what i asked for",
                     "stop doing per-commit pushes"):
            with self.subTest(text=text[:40]):
                self.assertTrue(self.fired(text))

    def test_the_seed_pass_rekeys_the_exact_legacy_trigger(self):
        reflex.write({"id": self.RID, "steer": "a fixture steer",
                      "signal": "prompt", "pattern": self.LEGACY,
                      "source": "helm-default"})
        self.assertTrue(self.fired("I said the cap was four"))       # control
        home.scaffold_global()
        e = self.by_id()[self.RID]
        self.assertEqual(e["signal"], "prompt")
        self.assertNotEqual(e["pattern"], self.LEGACY)
        self.assertEqual(e["steer"], "a fixture steer")
        self.assertIn("task/2978", e["notes"])
        self.assertFalse(self.fired("I said the cap was four"))
        self.assertTrue(self.fired("like i said, use the typed store"))

    def test_an_operator_edited_trigger_is_never_rekeyed(self):  # noqa: VACUOUS_ASSERTION — the file is compared EQUAL to its own non-empty prior bytes; test_the_seed_pass_rekeys_the_exact_legacy_trigger is the positive re-key on the same seed pass
        path = reflex.write({"id": self.RID, "steer": "mine",
                             "signal": "prompt", "pattern": r"\bi said\b"})
        with open(path, encoding="utf-8") as f:
            before = f.read()
        home.scaffold_global()
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)


class SeatReflexFenceTest(SeedBase):
    """task/2978: a seat in no project receives fleet reflexes only, the same
    law the store's seat door keeps; the inventory still lists every one."""

    def test_no_project_fires_fleet_reflexes_only(self):
        reflex.write({"id": "helm-only", "steer": "a helm steer",
                      "signal": "prompt", "pattern": r"\bflumpet\b",
                      "project": "helm"})
        reflex.write({"id": "fleet-wide", "steer": "a fleet steer",
                      "signal": "prompt", "pattern": r"\bflumpet\b"})
        fired = [e["id"] for e in reflex.fire("the flumpet broke")]
        self.assertIn("fleet-wide", fired)
        self.assertNotIn("helm-only", fired)
        self.assertIn("helm-only",
                      [e["id"] for e in reflex.fire("the flumpet broke",
                                                    project="helm")])
        self.assertIn("helm-only", self.by_id())        # inventory lists it
