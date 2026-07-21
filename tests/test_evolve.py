#!/usr/bin/env python3
"""evolve tests — the self-evolution composition layer. Pins the observer
return shapes proposals() composes, the propose-only law (no mutation beyond
the documented registry-sync write/scaffold — where the observers are mocked,
NO write at all is permitted), the drift snapshot=False contract (evolve must
never consume a pending drift report), the steady-state quiet line, and the
BEHAVIOR observers (fire-ledger wallpaper/dead-weight/silence, drift-feed
commands, reflex habituation + dead-reflex, recorder stuck/loop signatures)
against planted ledger and reflex-state fixtures.
Hermetic: tmp HELM_HOME throughout."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import drain, drift, evolve, home, inject, pk, reflex, registry, store, whoami  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR")

EMPTY_PROFILE = {"technical_level": "", "guidance": ""}

TS = "2026-07-01T00:00:00Z"


def snapshot(root):
    """{relpath: (size, mtime_ns)} for every file under root."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


class EvolveBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-evolve-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def with_observers(self, fn, sync_new=(), classify=(), drift_findings=(),
                       profile=None, notes=""):
        """Run fn with the store observers stubbed (the behavior observers run
        REAL against the hermetic empty home); returns (result, seen)."""
        seen = {}

        def fake_findings(project=None, snapshot=True):
            seen["drift_snapshot"] = snapshot
            return list(drift_findings), None

        with mock.patch.object(registry, "sync",
                               return_value=({"projects": {}},
                                             {"new": list(sync_new)})), \
                mock.patch.object(drain, "classify", return_value=list(classify)), \
                mock.patch.object(drift, "findings", fake_findings), \
                mock.patch.object(whoami, "load_profile",
                                  return_value=profile or dict(EMPTY_PROFILE)), \
                mock.patch.object(whoami, "load_notes", return_value=notes):
            return fn(), seen

    def run_cmd(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            (rc, _), seen = self.with_observers(lambda: (evolve.cmd_evolve([]), None), **kw)
        return rc, out.getvalue(), seen


class ProposalShapeTest(EvolveBase):
    def test_steady_state_is_one_quiet_line(self):
        rc, out, _ = self.run_cmd(profile={"technical_level": "expert",
                                           "guidance": "be terse"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "helm evolve: steady — nothing to propose.")

    def test_every_observer_yields_its_proposal(self):
        # a RISE finding: counted by the summary, mints no per-belief command
        props, _ = self.with_observers(
            evolve.proposals,
            sync_new=["alpha", "beta"],
            classify=[{"op": "retype"}, {"op": "route-project"},
                      {"op": "sweep-dup"}, {"op": "keep"}],
            drift_findings=[{"kind": "tier", "id": "prior-x", "tier": "auto-act",
                             "dir": "rose", "was": 0.5, "now": 0.9}])
        self.assertEqual([p[0] for p in props],
                         ["registry", "drain", "drain", "drift", "who"])
        for p in props:
            self.assertEqual(len(p), 3)  # (area, what, verb-or-None)
        by_area = {}
        for area, what, verb in props:
            by_area.setdefault(area, []).append((what, verb))
        self.assertIn("2 new projects discovered: alpha, beta",
                      by_area["registry"][0][0])
        self.assertIsNone(by_area["registry"][0][1])
        self.assertEqual(by_area["drain"][0],
                         ("2 raw entries routable to typed homes",
                          "helm drain --apply"))
        self.assertEqual(by_area["drain"][1][1], "helm drain --apply --sweep-dups")
        self.assertEqual(by_area["drift"][0],
                         ("1 belief drifting", "helm drift"))
        self.assertEqual(by_area["who"][0][1], "helm interview")

    def test_cmd_output_lists_verbs_and_count(self):
        rises = [{"kind": "tier", "id": i, "tier": "auto-act", "dir": "rose",
                  "was": 0.5, "now": 0.9} for i in ("a", "b")]
        rc, out, _ = self.run_cmd(sync_new=["alpha"], drift_findings=rises)
        self.assertEqual(rc, 0)
        self.assertIn("helm evolve — 3 proposals (no knowledge edited):", out)
        self.assertIn("[registry] 1 new project discovered: alpha", out)
        self.assertIn("[drift] 2 beliefs drifting  ->  helm drift", out)
        self.assertIn("[who]", out)
        self.assertIn("helm interview", out)

    def test_notes_alone_satisfy_the_know_your_user_leg(self):
        props, _ = self.with_observers(evolve.proposals, notes="knows the user")
        self.assertEqual(props, [])


class ProposeOnlyTest(EvolveBase):
    def test_propose_only_no_filesystem_mutation(self):
        # with the observers stubbed, the documented sync write is excluded —
        # evolve itself must write NOTHING
        os.makedirs(os.path.join(os.environ["HELM_HOME"], "_global"))
        marker = os.path.join(os.environ["HELM_HOME"], "_global", "keep.md")
        with open(marker, "w") as f:
            f.write("untouched")
        before = snapshot(self.tmp)
        rc, out, _ = self.run_cmd(
            sync_new=["alpha"], classify=[{"op": "retype"}],
            drift_findings=[{"kind": "contradicted", "id": "x", "n": 2,
                             "latest": "agents disagree"}])
        self.assertEqual(rc, 0)
        self.assertEqual(snapshot(self.tmp), before,
                         "evolve mutated the estate — propose-only broken")

    def test_drift_observer_never_consumes_the_snapshot(self):
        _, seen = self.with_observers(evolve.proposals)
        self.assertIs(seen["drift_snapshot"], False,
                      "evolve must call drift.findings(snapshot=False) — anything "
                      "else eats the operator's pending drift report")


class BehaviorBase(EvolveBase):
    """Behavior-observer tests: planted ledger fixtures, REAL store/reflex/
    drift in the hermetic home; the store-keeping observers stubbed quiet."""

    @contextlib.contextmanager
    def quiet_estate(self):
        with mock.patch.object(registry, "sync",
                               return_value=({"projects": {}}, {"new": []})), \
                mock.patch.object(drain, "classify", return_value=[]), \
                mock.patch.object(whoami, "load_profile",
                                  return_value={"technical_level": "expert",
                                                "guidance": "terse"}), \
                mock.patch.object(whoami, "load_notes", return_value=""):
            yield

    def props(self, project=None):
        with self.quiet_estate():
            return evolve.proposals(project=project)

    def seed(self, pid, conf=0.6, project=None, **kw):
        e = {"id": pid, "statement": "s-" + pid, "confidence": conf,
             "stated_ts": TS, "source": "human", "keywords": pid}
        e.update(kw)
        base = home.project_dir(project) if project else home.global_dir()
        return store.write_prior(e, root_dir=os.path.join(base, "premises"))

    def plant(self, rows, gen=""):
        path = inject._ledger_path() + gen
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write((json.dumps(r) if isinstance(r, dict) else r) + "\n")

    @staticmethod
    def turn(jit=(), reflexes=(), pinned=(), ts=TS):
        if not (jit or reflexes or pinned):
            return {"v": 1, "ts": ts, "project": None, "silent": True}
        return {"v": 1, "ts": ts, "project": None,
                "fired": {"pinned": list(pinned), "jit": list(jit),
                          "reflex": list(reflexes)}}


class WallpaperTest(BehaviorBase):
    def test_trigger_carries_the_numbers_and_the_evidence_verb(self):
        self.seed("wallp")
        self.plant([self.turn(jit=["wallp"])] * 18 + [self.turn(jit=["other"])] * 6)
        props = self.props()
        self.assertEqual(len(props), 1)
        area, what, verb = props[0]
        self.assertEqual(area, "fire")
        self.assertIn("'wallp' fired in 18/24 non-silent turns (75%)", what)
        self.assertIn("wallpaper", what)
        self.assertIn("helm store evidence ", verb)
        self.assertIn(" wallp -0.2 wallpaper: fired 75% of non-silent turns", verb)
        self.assertNotIn("--project", verb)

    def test_at_rate_or_thin_window_stays_quiet(self):
        self.seed("wallp")
        # exactly 50% is NOT wallpaper (> law), and 19 turns is under the floor
        self.plant([self.turn(jit=["wallp"])] * 12 + [self.turn(jit=["other"])] * 12)
        self.assertEqual(self.props(), [])
        os.remove(inject._ledger_path())
        self.plant([self.turn(jit=["wallp"])] * 19)
        self.assertEqual(self.props(), [])

    def test_non_live_id_never_proposed(self):
        self.plant([self.turn(jit=["ghost"])] * 24)
        self.assertEqual(self.props(), [])

    def test_premise_gets_the_review_verb_not_evidence(self):
        self.seed("cert-w", conf=1.0)  # certain: agent evidence cannot move it
        self.plant([self.turn(jit=["cert-w"])] * 24)
        props = self.props()
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0][2], "helm store get cert-w")


class DeadWeightTest(BehaviorBase):
    def test_never_fired_batched_once_pinned_excluded(self):
        self.seed("dw-a")
        self.seed("dw-b")
        self.seed("dw-c")
        self.seed("dw-pin", pin="true")  # always-lane: not a jit candidate
        self.plant([self.turn(jit=["dw-a"])] * 200)
        props = self.props()
        dead = [p for p in props if "never fired" in p[1]]
        self.assertEqual(len(dead), 1, "dead-weight must be ONE batched proposal")
        area, what, verb = dead[0]
        self.assertEqual(area, "fire")
        self.assertIn("2 live jit entries never fired over 200 turns: dw-b, dw-c", what)
        self.assertNotIn("dw-a", what)
        self.assertNotIn("dw-pin", what)
        self.assertIn("<id> -0.3 dead-weight: zero fires in 200 turns", verb)

    def test_thin_window_stays_quiet(self):
        self.seed("dw-b")
        self.plant([self.turn(jit=["dw-a"])] * 150)
        self.assertEqual([p for p in self.props() if "never fired" in p[1]], [])


class SilentAnomalyTest(BehaviorBase):
    def test_all_silent_over_populated_store_points_at_hooks(self):
        self.seed("alive")
        self.plant([self.turn()] * 30)
        props = self.props()
        self.assertEqual(len(props), 1)
        area, what, verb = props[0]
        self.assertEqual((area, verb), ("fire", "helm hooks status"))
        self.assertIn("100% of 30 turns silent with 1 live entries", what)

    def test_empty_store_all_silent_is_legit(self):
        self.plant([self.turn()] * 30)
        self.assertEqual(self.props(), [])

    def test_active_estate_never_flagged(self):
        self.seed("alive")
        self.plant([self.turn()] * 20 + [self.turn(jit=["other"])] * 10)
        self.assertEqual([p for p in self.props() if p[2] == "helm hooks status"], [])


class LedgerToleranceTest(BehaviorBase):
    def test_absent_ledger_no_claims_no_window(self):
        self.seed("alive")
        with self.quiet_estate():
            props, window = evolve.cycle()
        self.assertEqual(props, [])
        self.assertIsNone(window)

    def test_garbage_lines_skipped_valid_rows_counted(self):
        self.seed("wallp")
        self.plant(["not json", '{"v":1,"ts":', "[1,2,3]", '"just a string"'])
        with open(inject._ledger_path(), "ab") as f:
            f.write(b"\xff\xfe\x00 binary junk\n")
        self.plant([self.turn(jit=["wallp"])] * 18 + [self.turn(jit=["other"])] * 6)
        props = self.props()
        self.assertEqual(len(props), 1)
        self.assertIn("18/24 non-silent turns", props[0][1])

    def test_rotation_generation_read_first(self):
        self.seed("wallp")
        old = "2026-06-01T00:00:00Z"
        self.plant([self.turn(jit=["wallp"], ts=old)] * 9
                   + [self.turn(jit=["other"], ts=old)] * 3, gen=".1")
        self.plant([self.turn(jit=["wallp"])] * 9 + [self.turn(jit=["other"])] * 3)
        with self.quiet_estate():
            props, window = evolve.cycle()
        self.assertEqual(len(props), 1)
        self.assertIn("18/24 non-silent turns", props[0][1])
        self.assertEqual(window, {"turns": 24, "non_silent": 24, "since": old})


class ReflexOutcomeTest(BehaviorBase):
    def nag(self, rid="nag"):
        reflex.write({"id": rid, "steer": "steer-" + rid, "signal": "prompt",
                      "pattern": "x", "stated_ts": TS})

    def fires_at(self, idxs, n, rid="nag"):
        return [self.turn(reflexes=[rid]) if i in idxs else self.turn()
                for i in range(n)]

    def test_refire_habituation_proposes_honestly(self):
        self.nag()
        self.plant(self.fires_at({0, 2, 4, 6, 8}, 12))  # 4 episodes, gap 2
        props = self.props()
        self.assertEqual(len(props), 1)
        area, what, verb = props[0]
        self.assertEqual((area, verb), ("reflex", "helm reflex retire nag"))
        self.assertIn("reflex 'nag' re-fired within 3 turns of steering 4x", what)
        self.assertIn("the ledger logs fires, not heeds", what)

    def test_spaced_fires_are_healthy(self):
        self.nag()
        self.plant(self.fires_at({0, 5, 10, 15}, 18))
        self.assertEqual(self.props(), [])

    def test_below_episode_floor_stays_quiet(self):
        self.nag()
        self.plant(self.fires_at({0, 1, 2}, 6))  # 2 episodes < HABIT_MIN
        self.assertEqual(self.props(), [])

    def test_non_live_reflex_not_proposed(self):
        self.plant(self.fires_at({0, 1, 2, 3, 4, 5}, 8, rid="gone"))
        self.assertEqual(self.props(), [])


class DriftFeedTest(BehaviorBase):
    def drift_props(self, project=None):
        return [p for p in self.props(project=project) if p[0] == "drift"]

    def test_contradicted_mints_supersede_command(self):
        self.seed("cert-x", conf=1.0, evidence_log=[
            {"ts": TS, "type": "contradict", "delta": -0.1,
             "reason": "agents disagree", "by": "agent"}])
        props = self.drift_props()
        self.assertEqual(props[0], ("drift", "1 belief drifting", "helm drift"))
        area, what, verb = props[1]
        self.assertIn("premise 'cert-x' holds 1.0 against 1 agent contradiction", what)
        self.assertIn("supersede or retire", what)
        self.assertTrue(verb.startswith("helm store supersede "))
        self.assertIn(" cert-x <new-id> agents disagree", verb)
        self.assertNotIn("--project", verb)

    def test_scoped_commands_carry_project(self):
        self.seed("cert-p", conf=1.0, project="myproj", evidence_log=[
            {"ts": TS, "type": "contradict", "delta": -0.1,
             "reason": "wrong here", "by": "agent"}])
        props = self.drift_props(project="myproj")
        self.assertEqual(props[0][2], "helm drift --project myproj")
        self.assertTrue(props[1][2].endswith(" --project myproj"))

    def test_tier_fall_mints_reconfirm_evidence_with_the_delta(self):
        self.seed("tierx", conf=0.9)
        drift.report()  # operator baseline snapshot
        self.seed("tierx", conf=0.5)
        props = self.drift_props()
        self.assertEqual(len(props), 2)
        self.assertIn("belief 'tierx' fell below auto-act (0.90 -> 0.50)", props[1][1])
        self.assertIn(" tierx +0.40 re-confirmed (auto-act fall review)", props[1][2])
        # snapshot=False law: a second cycle still sees the same drift
        self.assertEqual(self.drift_props(), props)

    def test_rise_rides_the_summary_only(self):
        self.seed("riser", conf=0.5)
        drift.report()
        self.seed("riser", conf=0.9)
        props = self.drift_props()
        self.assertEqual(props, [("drift", "1 belief drifting", "helm drift")])

    def test_decayed_mints_retire_command(self):
        self.seed("dorm", conf=0.3)
        props = self.drift_props()
        self.assertIn("'dorm' dormant at 0.30", props[1][1])
        self.assertIn("helm store retire ", props[1][2])
        self.assertTrue(props[1][2].endswith(" dorm decayed dormant"))

    def test_one_command_per_belief(self):
        self.seed("crash", conf=0.9)
        drift.report()
        self.seed("crash", conf=0.3)  # crosses BOTH tiers + decays: 3 findings
        props = self.drift_props()
        self.assertEqual(len(props), 2, "one belief -> one command, not three")
        self.assertIn("fell below auto-act", props[1][1])


class DeadReflexTest(BehaviorBase):
    def live(self, rid):
        reflex.write({"id": rid, "steer": "steer-" + rid, "signal": "prompt",
                      "pattern": "x", "stated_ts": TS})

    def dead_props(self):
        return [p for p in self.props() if "zero ledger fires" in p[1]]

    def test_zero_fire_reflexes_batched_once(self):
        self.live("ra")
        self.live("rb")
        self.live("rc")
        self.plant([self.turn(reflexes=["ra"])] + [self.turn()] * 199)
        dead = self.dead_props()
        self.assertEqual(len(dead), 1, "dead reflexes must be ONE batched proposal")
        area, what, verb = dead[0]
        self.assertEqual((area, verb), ("reflex", "helm reflex retire <id>"))
        self.assertIn("2 live reflexes with zero ledger fires over 200 turns: "
                      "rb, rc", what)
        self.assertNotIn("ra,", what)
        self.assertIn("dead signal or unprovoked", what)

    def test_thin_window_stays_quiet(self):
        self.live("rb")
        self.plant([self.turn()] * 199)
        self.assertEqual(self.dead_props(), [])

    def test_all_firing_estate_stays_quiet(self):
        self.live("ra")
        self.plant([self.turn(reflexes=["ra"])] + [self.turn()] * 199)
        joined = " ".join(p[1] for p in self.props())
        self.assertNotIn("zero ledger fires", joined)

    def test_retired_reflex_not_proposed(self):
        self.live("gone")
        p = reflex.reflex_path("gone")  # retire via the status flip, hermetic
        with open(p, encoding="utf-8") as f:
            raw = f.read()
        pk.atomic_write(p, raw.replace("status: live", "status: retired"))
        self.plant([self.turn()] * 200)
        self.assertEqual(self.dead_props(), [])


class RecordCounterTest(BehaviorBase):
    def counters(self, sid, **kw):
        from helm import record
        d = record.session_dir(sid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "counters.json"), "w", encoding="utf-8") as f:
            json.dump(dict({"v": 1}, **kw), f)

    def record_props(self):
        return [p for p in self.props() if p[0] == "record"]

    def test_stuck_signature_across_sessions_proposes_once(self):
        for i in range(3):
            self.counters("s%d" % i, **{"stuck-streak": 3})
        props = self.record_props()
        self.assertEqual(len(props), 1)
        _area, what, verb = props[0]
        self.assertIn("3 sessions' last counters show a stuck streak >= 3", what)
        self.assertIn("repeated infra/auth failures", what)
        self.assertIn("helm coach", what)
        self.assertEqual(verb, "helm record status")

    def test_loop_thrash_signature(self):
        for i in range(4):
            self.counters("s%d" % i, **{"loop-streak": 5})
        props = self.record_props()
        self.assertEqual(len(props), 1)
        self.assertIn("4 sessions' last counters show a loop-thrash streak >= 3",
                      props[0][1])
        self.assertIn("re-run unchanged", props[0][1])

    def test_below_session_floor_stays_quiet(self):
        for i in range(2):  # 2 sessions < RECORD_SESS_MIN, however deep the streak
            self.counters("s%d" % i, **{"stuck-streak": 9})
        self.assertEqual(self.record_props(), [])

    def test_below_streak_floor_not_counted(self):
        for i in range(4):
            self.counters("s%d" % i, **{"stuck-streak": 2})
        self.assertEqual(self.record_props(), [])

    def test_resolved_streak_reads_zero_never_counted(self):
        for i in range(3):
            self.counters("s%d" % i, **{"stuck-streak": 0, "loop-streak": 0})
        self.assertEqual(self.record_props(), [])

    def test_absent_state_torn_and_alien_counters_no_claims(self):
        self.assertEqual(self.record_props(), [])  # no reflex-state dir at all
        from helm import record
        d = record.session_dir("torn")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "counters.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        for i in range(2):  # alien-shaped values never count toward the floor
            self.counters("a%d" % i, **{"stuck-streak": "9"})
        self.counters("a2", **{"stuck-streak": True})  # bool is not a streak
        self.assertEqual(self.record_props(), [])


class CapRankWindowTest(BehaviorBase):
    def test_cap_ten_ranked_by_evidence_strength(self):
        ids = ["w%02d" % k for k in range(1, 13)]
        for pid in ids:
            self.seed(pid)
        reflex.write({"id": "nag", "steer": "s", "signal": "prompt",
                      "pattern": "x", "stated_ts": TS})
        # w_k fires in 20+k of 40 turns (52..80%); nag every turn (39 episodes)
        self.plant([self.turn(jit=[p for k, p in enumerate(ids, 1) if t < 20 + k],
                              reflexes=["nag"]) for t in range(40)])
        props = self.props()
        self.assertEqual(len(props), evolve.BEHAVIOR_CAP)
        for p in props:
            self.assertEqual(len(p), 3)
        self.assertIn("'nag'", props[0][1])          # strongest evidence first
        self.assertIn("'w12'", props[1][1])          # then rate-ranked wallpaper
        joined = " ".join(p[1] for p in props)
        for dropped in ("w01", "w02", "w03"):
            self.assertNotIn(dropped, joined)

    def test_cycle_output_names_the_data_window(self):
        self.seed("wallp")
        self.plant([self.turn(jit=["wallp"], ts="2026-07-02T00:00:00Z")] * 24)
        out = io.StringIO()
        with self.quiet_estate(), contextlib.redirect_stdout(out):
            rc = evolve.cmd_evolve([])
        self.assertEqual(rc, 0)
        self.assertIn("(over 24 turns since 2026-07-02T00:00:00Z; 24 non-silent)",
                      out.getvalue())

    def test_steady_line_still_notes_the_window(self):
        self.plant([self.turn()] * 5)
        out = io.StringIO()
        with self.quiet_estate(), contextlib.redirect_stdout(out):
            rc = evolve.cmd_evolve([])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(),
                         "helm evolve: steady — nothing to propose."
                         "  (over 5 turns since %s; 0 non-silent)" % TS)


if __name__ == "__main__":
    unittest.main()
